import asyncio
import html
import logging
import os
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ContentType, ParseMode
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    CallbackQuery,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "anonimbot.db")
REVEAL_PRICE_STARS = int(os.getenv("REVEAL_PRICE_STARS", "15"))
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = Router()
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

DIVIDER = "━━━━━━━━━━━━━━"

BTN_LINK = "🔗 Моя ссылка"
BTN_BLOCKED = "🚫 Заблокированные"
BTN_HELP = "ℹ️ Как это работает"
BTN_CANCEL = "❌ Отмена"

RESTRICTED_CONTENT_TYPES = {
    ContentType.PHOTO,
    ContentType.VIDEO,
    ContentType.ANIMATION,
    ContentType.VOICE,
    ContentType.DOCUMENT,
    ContentType.STICKER,
}

RU_MONTHS = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]

PUBLIC_COMMANDS = [
    BotCommand(command="start", description="Запустить бота"),
    BotCommand(command="link", description="Моя персональная ссылка"),
    BotCommand(command="blocked", description="Заблокированные пользователи"),
    BotCommand(command="help", description="Как это работает"),
    BotCommand(command="cancel", description="Отменить текущее действие"),
]

SEND_COOLDOWN_SECONDS = 2.0
_last_send_at: dict[int, float] = {}
_bot_username: str | None = None


def is_rate_limited(user_id: int) -> bool:
    now = time.monotonic()
    if now - _last_send_at.get(user_id, 0.0) < SEND_COOLDOWN_SECONDS:
        return True
    _last_send_at[user_id] = now
    return False


def parse_callback_id(data: str | None) -> int | None:
    if not data:
        return None
    try:
        return int(data.split(":", 1)[1])
    except (IndexError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def db_connect() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


def init_db() -> None:
    with closing(db_connect()) as conn, conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                joined_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS blocks (
                owner_id INTEGER NOT NULL,
                blocked_id INTEGER NOT NULL,
                PRIMARY KEY (owner_id, blocked_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                added_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


def register_user(user_id: int) -> None:
    with closing(db_connect()) as conn, conn:
        conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))


def user_exists(user_id: int) -> bool:
    with closing(db_connect()) as conn:
        row = conn.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return row is not None


def is_blocked(owner_id: int, other_id: int) -> bool:
    with closing(db_connect()) as conn:
        row = conn.execute(
            "SELECT 1 FROM blocks WHERE owner_id = ? AND blocked_id = ?",
            (owner_id, other_id),
        ).fetchone()
        return row is not None


def block_user(owner_id: int, blocked_id: int) -> None:
    with closing(db_connect()) as conn, conn:
        conn.execute(
            "INSERT OR IGNORE INTO blocks (owner_id, blocked_id) VALUES (?, ?)",
            (owner_id, blocked_id),
        )


def unblock_user(owner_id: int, blocked_id: int) -> None:
    with closing(db_connect()) as conn, conn:
        conn.execute(
            "DELETE FROM blocks WHERE owner_id = ? AND blocked_id = ?",
            (owner_id, blocked_id),
        )


def list_blocked(owner_id: int) -> list[int]:
    with closing(db_connect()) as conn:
        rows = conn.execute(
            "SELECT blocked_id FROM blocks WHERE owner_id = ?", (owner_id,)
        ).fetchall()
        return [r[0] for r in rows]


def count_users() -> int:
    with closing(db_connect()) as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def add_admin(user_id: int) -> None:
    with closing(db_connect()) as conn, conn:
        conn.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (user_id,))


def remove_admin(user_id: int) -> None:
    with closing(db_connect()) as conn, conn:
        conn.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))


def list_admins() -> list[int]:
    with closing(db_connect()) as conn:
        rows = conn.execute("SELECT user_id FROM admins").fetchall()
        return [r[0] for r in rows]


def count_admins() -> int:
    with closing(db_connect()) as conn:
        return conn.execute("SELECT COUNT(*) FROM admins").fetchone()[0]


def is_admin(user_id: int) -> bool:
    if user_id == OWNER_ID:
        return True
    with closing(db_connect()) as conn:
        row = conn.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,)).fetchone()
        return row is not None


# ---------------------------------------------------------------------------
# FSM states
# ---------------------------------------------------------------------------

class SendMessageStates(StatesGroup):
    waiting_for_message = State()


class ReplyStates(StatesGroup):
    waiting_for_reply = State()


class AdminStates(StatesGroup):
    waiting_for_new_admin_id = State()


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

def main_menu_keyboard() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text=BTN_LINK)
    kb.button(text=BTN_BLOCKED)
    kb.button(text=BTN_HELP)
    kb.adjust(2, 1)
    return kb.as_markup(resize_keyboard=True)


def cancel_keyboard() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text=BTN_CANCEL)
    return kb.as_markup(resize_keyboard=True)


def relay_keyboard(other_user_id: int, revealed: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✍️ Ответить", callback_data=f"reply:{other_user_id}")
    kb.button(text="🚫 Заблокировать", callback_data=f"block:{other_user_id}")
    if revealed:
        kb.adjust(2)
    else:
        kb.button(text=f"🔍 Кто написал? — {REVEAL_PRICE_STARS} ⭐", callback_data=f"reveal:{other_user_id}")
        kb.adjust(2, 1)
    return kb.as_markup()


async def get_bot_link(bot: Bot, user_id: int) -> str:
    global _bot_username
    if _bot_username is None:
        me = await bot.get_me()
        _bot_username = me.username
    return f"https://t.me/{_bot_username}?start={user_id}"


def month_label(dt: datetime) -> str:
    return f"{RU_MONTHS[dt.month - 1]} {dt.year}"


async def get_monthly_star_revenue(bot: Bot) -> int | None:
    month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    total = 0
    offset = 0
    limit = 100

    try:
        for _ in range(20):
            result = await bot.get_star_transactions(offset=offset, limit=limit)
            transactions = result.transactions
            if not transactions:
                break

            for tx in transactions:
                tx_date = tx.date if tx.date.tzinfo else tx.date.replace(tzinfo=timezone.utc)
                if tx.source is not None and tx_date >= month_start:
                    total += tx.amount

            if len(transactions) < limit:
                break
            offset += limit
    except Exception:
        logger.exception("Не удалось получить данные о доходе Stars")
        return None

    return total


async def set_admin_commands_for(bot: Bot, user_id: int) -> None:
    try:
        await bot.set_my_commands(
            [*PUBLIC_COMMANDS, BotCommand(command="admin", description="Панель администратора")],
            scope=BotCommandScopeChat(chat_id=user_id),
        )
    except Exception:
        logger.exception("Не удалось установить команды администратора для %s", user_id)


async def clear_admin_commands_for(bot: Bot, user_id: int) -> None:
    try:
        await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=user_id))
    except Exception:
        logger.exception("Не удалось сбросить команды администратора для %s", user_id)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    user_id = message.from_user.id
    register_user(user_id)

    payload = command.args
    if payload and payload.isdigit():
        target_id = int(payload)

        if target_id == user_id:
            await message.answer(
                "🙂 Это ваша собственная ссылка.\n"
                "Поделитесь ей с друзьями — тогда они смогут написать вам анонимно.",
                reply_markup=main_menu_keyboard(),
            )
            return

        if not user_exists(target_id):
            await message.answer(
                "⚠️ Пользователь не найден. Возможно, ссылка неверна.",
                reply_markup=main_menu_keyboard(),
            )
            return

        if is_blocked(target_id, user_id):
            await message.answer(
                "🚫 К сожалению, вы не можете написать этому пользователю.",
                reply_markup=main_menu_keyboard(),
            )
            return

        await state.set_state(SendMessageStates.waiting_for_message)
        await state.update_data(target_id=target_id)
        await message.answer(
            "✍️ Напишите сообщение — оно будет отправлено <b>полностью анонимно</b>.",
            reply_markup=cancel_keyboard(),
        )
        return

    link = await get_bot_link(bot, user_id)
    await message.answer(
        "👋 <b>Добро пожаловать в Anonim Chat!</b>\n\n"
        "Здесь друзья могут написать вам <b>анонимно</b> — отправитель никогда не раскрывается.\n\n"
        "🔗 Вот ваша личная ссылка:\n\n"
        f"{link}\n\n"
        "Поделитесь ей и получайте анонимные сообщения 💬",
        reply_markup=main_menu_keyboard(),
    )


@router.message(Command("link"))
@router.message(F.text == BTN_LINK)
async def cmd_link(message: Message, bot: Bot) -> None:
    register_user(message.from_user.id)
    link = await get_bot_link(bot, message.from_user.id)
    await message.answer(f"🔗 Вот ваша личная ссылка:\n\n{link}\n\nПоделитесь ей и получайте анонимные сообщения 💬")


@router.message(Command("help"))
@router.message(F.text == BTN_HELP)
async def cmd_help(message: Message) -> None:
    await message.answer(
        "ℹ️ <b>Как это работает</b>\n\n"
        "1️⃣ Поделитесь своей ссылкой (🔗 Моя ссылка) с друзьями\n"
        "2️⃣ Они напишут вам — вы получите сообщение, не узнав, кто его отправил\n"
        "3️⃣ Вы можете ответить — ответ придёт отправителю анонимно\n"
        f"4️⃣ Хотите узнать, кто написал? Нажмите «🔍 Кто написал?» под сообщением — {REVEAL_PRICE_STARS} ⭐\n"
        "5️⃣ Если кто-то мешает — нажмите «🚫 Заблокировать» под его сообщением\n\n"
        "📝 Можно отправлять только текстовые сообщения — фото, видео, GIF, файлы и стикеры отключены.\n"
        "🔒 Сами сообщения нигде не сохраняются."
    )


@router.message(Command("cancel"))
@router.message(F.text == BTN_CANCEL)
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    if current is None:
        await message.answer("Нечего отменять.", reply_markup=main_menu_keyboard())
        return
    await state.clear()
    await message.answer("❌ Действие отменено.", reply_markup=main_menu_keyboard())


@router.message(Command("blocked"))
@router.message(F.text == BTN_BLOCKED)
async def cmd_blocked(message: Message) -> None:
    blocked_ids = list_blocked(message.from_user.id)
    if not blocked_ids:
        await message.answer("У вас нет заблокированных пользователей.")
        return

    kb = InlineKeyboardBuilder()
    for blocked_id in blocked_ids:
        kb.button(text=f"🔓 Разблокировать ID {blocked_id}", callback_data=f"unblock:{blocked_id}")
    kb.adjust(1)
    await message.answer("🚫 <b>Заблокированные пользователи:</b>", reply_markup=kb.as_markup())


# ---------------------------------------------------------------------------
# Admin panel
# ---------------------------------------------------------------------------

async def show_owner_panel(message: Message, bot: Bot) -> None:
    revenue = await get_monthly_star_revenue(bot)
    revenue_line = f"{revenue} ⭐" if revenue is not None else "н/д (ошибка запроса)"
    text = (
        "👑 <b>Панель владельца</b>\n\n"
        f"👥 Пользователей: {count_users()}\n"
        f"🛡 Администраторов: {count_admins()}\n"
        f"⭐ Доход за {month_label(datetime.now(timezone.utc))}: {revenue_line}"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить админа", callback_data="admin_add")
    kb.button(text="➖ Удалить админа", callback_data="admin_remove_list")
    kb.adjust(1)
    await message.answer(text, reply_markup=kb.as_markup())


async def show_admin_panel(message: Message, bot: Bot) -> None:
    revenue = await get_monthly_star_revenue(bot)
    revenue_line = f"{revenue} ⭐" if revenue is not None else "н/д (ошибка запроса)"
    text = (
        "🛡 <b>Панель администратора</b>\n\n"
        f"👥 Пользователей: {count_users()}\n"
        f"⭐ Доход за {month_label(datetime.now(timezone.utc))}: {revenue_line}"
    )
    await message.answer(text)


@router.message(Command("admin"))
async def cmd_admin(message: Message, bot: Bot) -> None:
    user_id = message.from_user.id
    if user_id == OWNER_ID:
        await show_owner_panel(message, bot)
        return
    if is_admin(user_id):
        await show_admin_panel(message, bot)
        return
    await message.answer("⛔ Эта команда только для администраторов.")


@router.callback_query(F.data == "admin_add")
async def cb_admin_add(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Недоступно.", show_alert=True)
        return

    await state.set_state(AdminStates.waiting_for_new_admin_id)
    await callback.message.answer(
        "Отправьте Telegram ID пользователя, которого нужно сделать администратором:",
        reply_markup=cancel_keyboard(),
    )
    await callback.answer()


@router.message(AdminStates.waiting_for_new_admin_id)
async def process_new_admin_id(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    if message.from_user.id != OWNER_ID:
        return
    text = (message.text or "").strip()

    if not text.isdigit():
        await message.answer("⚠️ Нужно отправить числовой Telegram ID.", reply_markup=main_menu_keyboard())
        return

    new_admin_id = int(text)
    if new_admin_id == OWNER_ID:
        await message.answer("Владелец уже обладает полным доступом.", reply_markup=main_menu_keyboard())
        return

    add_admin(new_admin_id)
    await set_admin_commands_for(bot, new_admin_id)

    try:
        await bot.send_message(new_admin_id, "🛡 Вам выдан доступ администратора.\nОткройте панель: /admin")
    except Exception:
        pass

    await message.answer(f"✅ Пользователь <code>{new_admin_id}</code> добавлен в администраторы.", reply_markup=main_menu_keyboard())


@router.callback_query(F.data == "admin_remove_list")
async def cb_admin_remove_list(callback: CallbackQuery) -> None:
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Недоступно.", show_alert=True)
        return

    admin_ids = list_admins()
    if not admin_ids:
        await callback.answer("Список администраторов пуст.", show_alert=True)
        return

    kb = InlineKeyboardBuilder()
    for admin_id in admin_ids:
        kb.button(text=f"➖ ID {admin_id}", callback_data=f"admin_remove:{admin_id}")
    kb.adjust(1)
    await callback.message.answer("Выберите администратора для удаления:", reply_markup=kb.as_markup())
    await callback.answer()


@router.callback_query(F.data.startswith("admin_remove:"))
async def cb_admin_remove(callback: CallbackQuery, bot: Bot) -> None:
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Недоступно.", show_alert=True)
        return

    admin_id = parse_callback_id(callback.data)
    if admin_id is None:
        await callback.answer("⚠️ Некорректные данные.", show_alert=True)
        return
    remove_admin(admin_id)
    await clear_admin_commands_for(bot, admin_id)

    try:
        await bot.send_message(admin_id, "⛔ Ваш доступ администратора отозван.")
    except Exception:
        pass

    try:
        await callback.message.edit_text(f"✅ Администратор {admin_id} удалён.")
    except Exception:
        pass
    await callback.answer()


# ---------------------------------------------------------------------------
# Paid reveal (Telegram Stars)
# ---------------------------------------------------------------------------

@router.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery, bot: Bot) -> None:
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)


@router.message(F.successful_payment)
async def process_successful_payment(message: Message, bot: Bot) -> None:
    parts = message.successful_payment.invoice_payload.split(":")
    if len(parts) != 4 or parts[0] != "reveal":
        return

    _, target_id_raw, orig_chat_id_raw, orig_message_id_raw = parts
    target_user_id = int(target_id_raw)
    orig_chat_id = int(orig_chat_id_raw)
    orig_message_id = int(orig_message_id_raw)

    try:
        chat = await bot.get_chat(target_user_id)
        safe_name = html.escape(chat.full_name or "")
        username_line = f"@{chat.username}" if chat.username else "не указан"
        text = (
            "🔓 <b>Отправитель раскрыт!</b>\n\n"
            f"👤 Имя: {safe_name}\n"
            f"🔗 Username: {username_line}\n"
            f"🆔 ID: <code>{target_user_id}</code>\n\n"
            f'<a href="tg://user?id={target_user_id}">Открыть профиль</a>'
        )
    except Exception:
        logger.exception("Не удалось получить данные пользователя для раскрытия")
        text = (
            "🔓 <b>Отправитель раскрыт!</b>\n\n"
            f"🆔 ID: <code>{target_user_id}</code>\n\n"
            "Дополнительные данные профиля недоступны."
        )

    await message.answer(text, reply_markup=main_menu_keyboard())

    try:
        await bot.edit_message_reply_markup(
            chat_id=orig_chat_id,
            message_id=orig_message_id,
            reply_markup=relay_keyboard(target_user_id, revealed=True),
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Anonymous message flow
# ---------------------------------------------------------------------------

@router.message(SendMessageStates.waiting_for_message)
async def process_anonymous_message(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    target_id = data.get("target_id")
    sender_id = message.from_user.id
    await state.clear()

    if not target_id:
        await message.answer("⚠️ Что-то пошло не так. Попробуйте снова: /start", reply_markup=main_menu_keyboard())
        return

    if is_blocked(target_id, sender_id):
        await message.answer("🚫 К сожалению, вы не можете написать этому пользователю.", reply_markup=main_menu_keyboard())
        return

    if message.content_type in RESTRICTED_CONTENT_TYPES:
        await message.answer(
            "🔒 Можно отправлять только текстовые сообщения.\nФото, видео, GIF, файлы и стикеры отключены.",
            reply_markup=main_menu_keyboard(),
        )
        return

    if is_rate_limited(sender_id):
        await message.answer("⏳ Слишком быстро. Подождите пару секунд и попробуйте снова.", reply_markup=main_menu_keyboard())
        return

    try:
        await bot.send_message(target_id, f"📨 <b>Новое анонимное сообщение</b>\n{DIVIDER}")
        await bot.copy_message(
            chat_id=target_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
            reply_markup=relay_keyboard(sender_id),
        )
    except Exception:
        logger.exception("Не удалось отправить анонимное сообщение")
        await message.answer(
            "❌ Не удалось отправить сообщение. Возможно, пользователь заблокировал бота.",
            reply_markup=main_menu_keyboard(),
        )
        return

    await message.answer("✅ Готово! Сообщение отправлено анонимно.", reply_markup=main_menu_keyboard())


@router.message(ReplyStates.waiting_for_reply)
async def process_reply(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    reply_to = data.get("reply_to")
    sender_id = message.from_user.id
    await state.clear()

    if not reply_to:
        await message.answer("⚠️ Что-то пошло не так. Попробуйте снова.", reply_markup=main_menu_keyboard())
        return

    if is_blocked(reply_to, sender_id):
        await message.answer("🚫 Вы не можете ответить этому пользователю.", reply_markup=main_menu_keyboard())
        return

    if message.content_type in RESTRICTED_CONTENT_TYPES:
        await message.answer(
            "🔒 Можно отправлять только текстовые сообщения.\nФото, видео, GIF, файлы и стикеры отключены.",
            reply_markup=main_menu_keyboard(),
        )
        return

    if is_rate_limited(sender_id):
        await message.answer("⏳ Слишком быстро. Подождите пару секунд и попробуйте снова.", reply_markup=main_menu_keyboard())
        return

    try:
        await bot.send_message(reply_to, f"📩 <b>Вам ответили</b>\n{DIVIDER}")
        await bot.copy_message(
            chat_id=reply_to,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
            reply_markup=relay_keyboard(sender_id),
        )
    except Exception:
        logger.exception("Не удалось отправить ответ")
        await message.answer("❌ Не удалось отправить ответ.", reply_markup=main_menu_keyboard())
        return

    await message.answer("✅ Ответ отправлен!", reply_markup=main_menu_keyboard())


# ---------------------------------------------------------------------------
# Callback buttons: reply / block / unblock
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("reply:"))
async def cb_reply(callback: CallbackQuery, state: FSMContext) -> None:
    other_id = parse_callback_id(callback.data)
    if other_id is None:
        await callback.answer("⚠️ Некорректные данные.", show_alert=True)
        return

    if is_blocked(callback.from_user.id, other_id):
        await callback.answer("Этот пользователь заблокирован. Сначала разблокируйте его в «🚫 Заблокированные».", show_alert=True)
        return

    await state.set_state(ReplyStates.waiting_for_reply)
    await state.update_data(reply_to=other_id)
    await callback.message.answer("✍️ Напишите ответ:", reply_markup=cancel_keyboard())
    await callback.answer()


@router.callback_query(F.data.startswith("reveal:"))
async def cb_reveal(callback: CallbackQuery, bot: Bot) -> None:
    target_user_id = parse_callback_id(callback.data)
    if target_user_id is None:
        await callback.answer("⚠️ Некорректные данные.", show_alert=True)
        return
    payload = f"reveal:{target_user_id}:{callback.message.chat.id}:{callback.message.message_id}"

    try:
        await bot.send_invoice(
            chat_id=callback.from_user.id,
            title="Кто это написал?",
            description="Узнайте, кто отправил вам это анонимное сообщение.",
            payload=payload,
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label="Раскрыть отправителя", amount=REVEAL_PRICE_STARS)],
        )
    except Exception:
        logger.exception("Не удалось выставить счёт за раскрытие отправителя")
        await callback.answer("❌ Не удалось выставить счёт. Попробуйте позже.", show_alert=True)
        return

    await callback.answer()


@router.callback_query(F.data.startswith("block:"))
async def cb_block(callback: CallbackQuery, state: FSMContext) -> None:
    blocked_id = parse_callback_id(callback.data)
    if blocked_id is None:
        await callback.answer("⚠️ Некорректные данные.", show_alert=True)
        return
    owner_id = callback.from_user.id

    if blocked_id == owner_id:
        await callback.answer("Это действие недоступно.", show_alert=True)
        return

    block_user(owner_id, blocked_id)

    data = await state.get_data()
    if data.get("reply_to") == blocked_id:
        await state.clear()

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.answer("🚫 Пользователь заблокирован.", show_alert=True)


@router.callback_query(F.data.startswith("unblock:"))
async def cb_unblock(callback: CallbackQuery) -> None:
    blocked_id = parse_callback_id(callback.data)
    if blocked_id is None:
        await callback.answer("⚠️ Некорректные данные.", show_alert=True)
        return
    unblock_user(callback.from_user.id, blocked_id)
    await callback.answer("🔓 Пользователь разблокирован.", show_alert=True)

    blocked_ids = list_blocked(callback.from_user.id)
    if not blocked_ids:
        await callback.message.edit_text("У вас нет заблокированных пользователей.")
        return

    kb = InlineKeyboardBuilder()
    for bid in blocked_ids:
        kb.button(text=f"🔓 Разблокировать ID {bid}", callback_data=f"unblock:{bid}")
    kb.adjust(1)
    await callback.message.edit_reply_markup(reply_markup=kb.as_markup())


# ---------------------------------------------------------------------------
# Fallback (must stay last)
# ---------------------------------------------------------------------------

@router.message()
async def fallback(message: Message) -> None:
    await message.answer(
        "🤔 Не понял вас. Нажмите /start, чтобы начать заново.",
        reply_markup=main_menu_keyboard(),
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def setup_bot_profile(bot: Bot) -> None:
    await bot.set_my_commands(PUBLIC_COMMANDS)
    await bot.set_my_description(
        "Отправляйте и получайте анонимные сообщения. Отправитель никогда не раскрывается, "
        "а сами сообщения нигде не сохраняются."
    )
    await bot.set_my_short_description("💬 Анонимные сообщения без границ")

    if OWNER_ID:
        await set_admin_commands_for(bot, OWNER_ID)
    for admin_id in list_admins():
        await set_admin_commands_for(bot, admin_id)


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN topilmadi. .env faylida BOT_TOKEN qiymatini kiriting (.env.example ga qarang).")

    init_db()

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    await bot.delete_webhook(drop_pending_updates=True)
    await setup_bot_profile(bot)

    retry_delay = 5
    while True:
        try:
            await dp.start_polling(bot)
            break
        except TelegramUnauthorizedError:
            logger.error("Noto'g'ri BOT_TOKEN. Botni to'xtatish.")
            raise
        except (TelegramNetworkError, asyncio.TimeoutError):
            logger.warning("Tarmoq bilan bog'lanishda xatolik, %s soniyadan keyin qayta urinish.", retry_delay)
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)
        except Exception:
            logger.exception("Kutilmagan xatolik, %s soniyadan keyin qayta urinish.", retry_delay)
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot to'xtatildi.")
