import html
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ChatType
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)


def env_required(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Не задана переменная окружения {name}"
        )
    return value


BOT_TOKEN = env_required("BOT_TOKEN")
OWNER_ID = int(env_required("OWNER_ID"))
ADMIN_CHAT_ID = int(env_required("ADMIN_CHAT_ID"))
MAIN_CHAT_ID = int(env_required("MAIN_CHAT_ID"))
DATA_FILE = Path(os.getenv("DATA_FILE", "data.json"))

DEFAULT_TEMPLATE = (
    "Привет! Чтобы подать заявку на вступление в беседу, "
    "отправь свою фотографию, а следующим сообщением "
    "напиши свой никнейм."
)

DEFAULT_DATA = {
    "template_text": DEFAULT_TEMPLATE,
    "template_photo": None,
    "admins": [],
    "applications": {},
    "admin_message_map": {},
    "next_application_id": 1,
    "blocked_users": [],
    "user_blocks": {},
    "admin_log": [],
    "last_submission_at": {},
}


def save_data(data):
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_file = DATA_FILE.with_suffix(".json.tmp")

    with temp_file.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)

    temp_file.replace(DATA_FILE)


def load_data():
    if not DATA_FILE.exists():
        data = json.loads(json.dumps(DEFAULT_DATA))
        save_data(data)
        return data

    with DATA_FILE.open("r", encoding="utf-8") as file:
        saved = json.load(file)

    data = json.loads(json.dumps(DEFAULT_DATA))

    if isinstance(saved, dict):
        data.update(saved)

    for key, value in DEFAULT_DATA.items():
        data.setdefault(key, value)

    return data



def log_action(data, actor_id, action, target_id=None, app_id=None, details=None):
    """Сохраняет историю действий Администрации в data.json."""
    entry = {
        "at": datetime.now(timezone.utc).isoformat(),
        "actor_id": int(actor_id) if actor_id is not None else None,
        "action": str(action),
        "target_id": int(target_id) if target_id is not None else None,
        "app_id": int(app_id) if app_id is not None else None,
        "details": str(details) if details else None,
    }
    log = data.setdefault("admin_log", [])
    log.append(entry)
    # Ограничиваем размер файла, оставляя последние 500 событий.
    del log[:-500]
    save_data(data)


def user_application_history(data, user_id, current_app_id=None):
    apps = [
        app for app in data.get("applications", {}).values()
        if int(app.get("user_id", 0)) == int(user_id)
        and str(app.get("id")) != str(current_app_id)
    ]
    total = len(apps)
    accepted = sum(app.get("status") == "accepted" for app in apps)
    rejected = sum(app.get("status") == "rejected" for app in apps)
    pending = sum(app.get("status") == "pending" for app in apps)
    last = max(apps, key=lambda a: a.get("created_at", ""), default=None)
    last_text = "нет"
    if last:
        last_status = {"accepted": "одобрена", "rejected": "отклонена", "pending": "на рассмотрении"}.get(last.get("status"), last.get("status", "неизвестно"))
        last_text = f"№{last.get('id', '?')} — {last_status}"
    return f"📚 История: {total} предыдущих заявок (одобрено: {accepted}, отклонено: {rejected}, ожидают: {pending})\n🕘 Последняя: {last_text}"


def pending_application_for_user(data, user_id):
    for key, app in data.get("applications", {}).items():
        if int(app.get("user_id", 0)) == int(user_id) and app.get("status") == "pending":
            return int(key), app
    return None, None


def get_user_block(data, user_id):
    """Возвращает действующую блокировку или None."""
    user_id = int(user_id)

    # Старый список blocked_users сохраняем как бессрочные блокировки.
    if user_id in {int(x) for x in data.get("blocked_users", [])}:
        return {"until": None, "reason": "Бессрочная блокировка"}

    blocks = data.setdefault("user_blocks", {})
    block = blocks.get(str(user_id))
    if not block:
        return None

    until_text = block.get("until")
    if not until_text:
        return block

    try:
        until = datetime.fromisoformat(until_text)
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        # Повреждённая дата не должна случайно снять блокировку.
        return block

    if datetime.now(timezone.utc) >= until:
        blocks.pop(str(user_id), None)
        save_data(data)
        return None

    return block


def apply_user_block(data, user_id, hours=24, reason="Отказ в заявке", blocked_by=None):
    """Создаёт временную или бессрочную блокировку подачи заявок."""
    user_id = int(user_id)
    data.setdefault("user_blocks", {})

    if hours is None:
        until = None
        until_text = None
    else:
        until = datetime.now(timezone.utc) + timedelta(hours=hours)
        until_text = until.isoformat()

    data["user_blocks"][str(user_id)] = {
        "until": until_text,
        "reason": reason,
        "blocked_by": int(blocked_by) if blocked_by is not None else None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    save_data(data)
    return data["user_blocks"][str(user_id)]


def format_block_expiry(block):
    until_text = block.get("until")
    if not until_text:
        return "навсегда"
    try:
        until = datetime.fromisoformat(until_text)
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        return until.astimezone().strftime("%d.%m.%Y в %H:%M")
    except (TypeError, ValueError):
        return "срок неизвестен"


def get_replied_target(data, message):
    """Определяет пользователя по ответу на заявку или сообщение."""
    replied = message.reply_to_message if message else None
    if not replied:
        return None

    _, app = find_application(data, replied.message_id)
    if app:
        return int(app.get("user_id"))

    if replied.from_user:
        return int(replied.from_user.id)

    return None


def get_data(context):
    return context.application.bot_data["data"]


def is_owner(user_id):
    return user_id == OWNER_ID


def has_access(user_id, data):
    if user_id is None:
        return False

    admins = {int(item) for item in data.get("admins", [])}
    return user_id == OWNER_ID or user_id in admins


def in_admin_chat(update):
    chat = update.effective_chat
    return chat is not None and chat.id == ADMIN_CHAT_ID


def esc(value):
    return html.escape(str(value), quote=True)


def application_keyboard(app_id, status="pending", has_invite=False):
    rows = []

    if status == "pending":
        rows.append([
            InlineKeyboardButton("✅ Принять", callback_data=f"app:accept:{app_id}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"app:reject:{app_id}"),
        ])
    elif status == "accepted" and has_invite:
        rows.append([
            InlineKeyboardButton("🚫 Отозвать ссылку", callback_data=f"app:revoke:{app_id}"),
        ])

    rows.append([
        InlineKeyboardButton("💬 Написать пользователю", callback_data=f"app:contact:{app_id}")
    ])
    return InlineKeyboardMarkup(rows)


def find_application(data, message_id):
    app_id = data["admin_message_map"].get(str(message_id))

    if app_id is not None:
        app = data["applications"].get(str(app_id))
        if app:
            return int(app_id), app

    for key, app in data["applications"].items():
        if app.get("admin_message_id") == message_id:
            return int(key), app

    return None, None


async def send_template(chat_id, data, context):
    text = data.get("template_text") or DEFAULT_TEMPLATE
    photo = data.get("template_photo")

    if photo:
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=photo,
            caption=text[:1024],
        )

        if len(text) > 1024:
            await context.bot.send_message(
                chat_id=chat_id,
                text=text[1024:],
            )
    else:
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
        )


# ============================================================
# START
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    if not message or not user or not chat:
        return

    if chat.type != ChatType.PRIVATE:
        return

    data = get_data(context)

    block = get_user_block(data, user.id)
    if block:
        await message.reply_text(
            "❌ Подача заявок для тебя временно ограничена.\n"
            f"Причина: {esc(block.get('reason', 'не указана'))}\n"
            f"Блокировка действует до: {format_block_expiry(block)}."
        )
        return

    context.user_data.pop("awaiting_nickname", None)
    context.user_data.pop("application_photo", None)
    context.user_data["awaiting_photo"] = True

    await send_template(chat.id, data, context)


# ============================================================
# TEMPLATE MANAGEMENT
# ============================================================

async def set_template(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    user = update.effective_user

    if not message or not user or not in_admin_chat(update):
        return

    if not is_owner(user.id):
        await message.reply_text("❌ Доступ запрещён.")
        return

    data = get_data(context)

    if message.reply_to_message and message.reply_to_message.photo:
        photo_message = message.reply_to_message
        data["template_photo"] = photo_message.photo[-1].file_id

        if photo_message.caption:
            data["template_text"] = photo_message.caption

        save_data(data)
        await message.reply_text("✅ Шаблон сохранён.")
        return

    raw_text = message.text or ""
    text = raw_text.partition(" ")[2].strip()

    if text.startswith("@"):
        text = text.partition(" ")[2].strip()

    if text:
        data["template_text"] = text
        save_data(data)
        context.chat_data["awaiting_template_photo"] = True

        await message.reply_text(
            "📝 Текст сохранён. Теперь отправь фотографию "
            "отдельным сообщением.\n\n"
            "Если фотография не нужна, отправь /безфото."
        )
        return

    await message.reply_text(
        "Как изменить шаблон:\n\n"
        "1. Отправь /шаблон Текст сообщения.\n"
        "2. Следующим сообщением отправь фотографию.\n\n"
        "Либо ответь командой /шаблон на фотографию.\n\n"
        "Текущий шаблон: /шаблон_текущий"
    )


async def template_photo_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    user = update.effective_user

    if not message or not user or not in_admin_chat(update):
        return

    if not is_owner(user.id):
        return

    data = get_data(context)
    caption = message.caption or ""

    if caption.startswith("/шаблон"):
        text = caption[len("/шаблон"):].strip()
        data["template_photo"] = message.photo[-1].file_id

        if text:
            data["template_text"] = text

        save_data(data)
        await message.reply_text(
            "✅ Шаблон с фотографией сохранён."
        )
        return

    if context.chat_data.get("awaiting_template_photo"):
        data["template_photo"] = message.photo[-1].file_id
        save_data(data)
        context.chat_data.pop("awaiting_template_photo", None)

        await message.reply_text(
            "✅ Фотография добавлена в шаблон."
        )


async def show_template(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    user = update.effective_user

    if not message or not user or not in_admin_chat(update):
        return

    data = get_data(context)

    if not has_access(user.id, data):
        await message.reply_text("❌ Доступ запрещён.")
        return

    await send_template(ADMIN_CHAT_ID, data, context)


async def remove_template_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    user = update.effective_user

    if not message or not user or not in_admin_chat(update):
        return

    if not is_owner(user.id):
        await message.reply_text("❌ Доступ запрещён.")
        return

    data = get_data(context)
    data["template_photo"] = None
    save_data(data)
    context.chat_data.pop("awaiting_template_photo", None)

    await message.reply_text(
        "✅ Фотография убрана из шаблона."
    )


# ============================================================
# ADMIN ACCESS
# ============================================================

async def add_admin(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    user = update.effective_user

    if not message or not user or not in_admin_chat(update):
        return

    if not is_owner(user.id):
        await message.reply_text("❌ Доступ запрещён.")
        return

    if not message.reply_to_message:
        await message.reply_text(
            "Ответь командой /add на сообщение пользователя "
            "или на карточку его заявки."
        )
        return

    data = get_data(context)
    replied = message.reply_to_message
    _, app = find_application(data, replied.message_id)

    target_id = app.get("user_id") if app else None

    if target_id is None and replied.from_user:
        target_id = replied.from_user.id

    if target_id is None:
        await message.reply_text(
            "❌ Не удалось определить пользователя."
        )
        return

    target_id = int(target_id)

    if target_id == OWNER_ID:
        await message.reply_text(
            "Владелец уже имеет полный доступ."
        )
        return

    admins = {int(x) for x in data["admins"]}
    admins.add(target_id)
    data["admins"] = sorted(admins)
    log_action(data, user.id, "admin_access_granted", target_id)
    save_data(data)

    try:
        await context.bot.send_message(
            chat_id=target_id,
            text="✅ Вам выдан доступ к управлению заявками.",
        )
    except TelegramError:
        pass

    await message.reply_text("✅ Доступ выдан.")


async def remove_admin(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    user = update.effective_user

    if not message or not user or not in_admin_chat(update):
        return

    if not is_owner(user.id):
        await message.reply_text("❌ Доступ запрещён.")
        return

    if not message.reply_to_message:
        await message.reply_text(
            "Ответь командой /del на сообщение пользователя "
            "или на карточку его заявки."
        )
        return

    data = get_data(context)
    replied = message.reply_to_message
    _, app = find_application(data, replied.message_id)

    target_id = app.get("user_id") if app else None

    if target_id is None and replied.from_user:
        target_id = replied.from_user.id

    if target_id is None:
        await message.reply_text(
            "❌ Не удалось определить пользователя."
        )
        return

    target_id = int(target_id)

    if target_id == OWNER_ID:
        await message.reply_text(
            "Нельзя снять права владельца."
        )
        return

    data["admins"] = [
        int(x)
        for x in data["admins"]
        if int(x) != target_id
    ]
    log_action(data, user.id, "admin_access_revoked", target_id)
    save_data(data)

    try:
        await context.bot.send_message(
            chat_id=target_id,
            text="❌ Ваш доступ к управлению заявками отозван.",
        )
    except TelegramError:
        pass

    await message.reply_text("❌ Доступ отозван.")


# ============================================================
# USER APPLICATIONS
# ============================================================

async def private_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    if not message or not user or not chat:
        return

    if chat.type != ChatType.PRIVATE:
        return

    data = get_data(context)
    block = get_user_block(data, user.id)
    if block:
        context.user_data.pop("awaiting_photo", None)
        context.user_data.pop("awaiting_nickname", None)
        context.user_data.pop("application_photo", None)
        await message.reply_text(
            "❌ Ты не можешь подать заявку. "
            f"Блокировка действует до: {format_block_expiry(block)}."
        )
        return

    if context.user_data.get("awaiting_photo"):
        context.user_data.pop("awaiting_photo", None)
        context.user_data["application_photo"] = (
            message.photo[-1].file_id
        )
        context.user_data["awaiting_nickname"] = True

        await message.reply_text(
            "✅ Фотография получена!\n\n"
            "Теперь отправь свой никнейм текстовым сообщением."
        )
        return

    await relay_user_message(update, context)


async def private_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    if not message or not user or not chat:
        return

    data = get_data(context)

    if chat.type == ChatType.PRIVATE:
        block = get_user_block(data, user.id)
        if block:
            context.user_data.pop("awaiting_photo", None)
            context.user_data.pop("awaiting_nickname", None)
            context.user_data.pop("application_photo", None)
            await message.reply_text(
                "❌ Ты не можешь подать заявку. "
                f"Блокировка действует до: {format_block_expiry(block)}."
            )
            return
        if await relay_user_message(update, context):
            return

        if not context.user_data.get("awaiting_nickname"):
            await message.reply_text(
                "Чтобы подать заявку, отправь /start."
            )
            return

        nickname = (message.text or "").strip()

        if not nickname or nickname.startswith("/"):
            await message.reply_text(
                "Отправь никнейм обычным текстовым сообщением."
            )
            return

        photo_id = context.user_data.get("application_photo")

        if not photo_id:
            context.user_data.pop("awaiting_nickname", None)
            context.user_data["awaiting_photo"] = True
            await message.reply_text("Сначала отправь фотографию.")
            return

        existing_id, existing_app = pending_application_for_user(data, user.id)
        if existing_app:
            context.user_data.pop("awaiting_nickname", None)
            context.user_data.pop("application_photo", None)
            await message.reply_text(
                f"⏳ У тебя уже есть заявка №{existing_id} на рассмотрении. "
                "Дождись решения Администрации."
            )
            return

        # Защита от повторной отправки заявок: не чаще одной заявки за 60 секунд.
        now = datetime.now(timezone.utc)
        previous_time = data.setdefault("last_submission_at", {}).get(str(user.id))
        if previous_time:
            try:
                previous_dt = datetime.fromisoformat(previous_time)
                if previous_dt.tzinfo is None:
                    previous_dt = previous_dt.replace(tzinfo=timezone.utc)
                seconds_left = 60 - (now - previous_dt).total_seconds()
                if seconds_left > 0:
                    await message.reply_text(
                        f"⏳ Подожди ещё {int(seconds_left) + 1} сек. перед повторной подачей."
                    )
                    return
            except (TypeError, ValueError):
                pass

        app_id = int(data["next_application_id"])
        data["next_application_id"] = app_id + 1

        app = {
            "id": app_id,
            "user_id": user.id,
            "user_name": user.full_name,
            "username": user.username,
            "nickname": nickname,
            "photo_id": photo_id,
            "status": "pending",
            "admin_message_id": None,
            "contact_mode": False,
            "created_at": now.isoformat(),
        }
        data["last_submission_at"][str(user.id)] = now.isoformat()

        data["applications"][str(app_id)] = app
        save_data(data)

        username = (
            f"@{user.username}"
            if user.username
            else "не указан"
        )

        caption = (
            f"📨 <b>Заявка №{app_id}</b>\n\n"
            f"🎮 Никнейм: <b>{esc(nickname)}</b>\n"
            f"👤 Пользователь: "
            f'<a href="tg://user?id={user.id}">'
            f"{esc(user.full_name)}</a>\n"
            f"🔗 Username: {esc(username)}\n"
            f"🆔 ID пользователя: <code>{user.id}</code>\n\n"
            f"{esc(user_application_history(data, user.id, app_id))}"
        )

        sent = await context.bot.send_photo(
            chat_id=ADMIN_CHAT_ID,
            photo=photo_id,
            caption=caption,
            parse_mode="HTML",
            reply_markup=application_keyboard(app_id),
        )

        app["admin_message_id"] = sent.message_id
        data["admin_message_map"][str(sent.message_id)] = app_id
        save_data(data)

        context.user_data.pop("awaiting_nickname", None)
        context.user_data.pop("application_photo", None)

        await message.reply_text(
            "✅ Твоя заявка отправлена Администрации на рассмотрение."
        )
        return

    # Переписка администратора с заявителем
    if chat.id != ADMIN_CHAT_ID or not has_access(user.id, data):
        return

    app_id = context.user_data.get("admin_contact_app_id")

    if not app_id:
        return

    app = data["applications"].get(str(app_id))

    if not app or not app.get("contact_mode"):
        context.user_data.pop("admin_contact_app_id", None)
        await message.reply_text("Активный диалог не найден.")
        return

    if (message.text or "").startswith("/"):
        return

    try:
        await context.bot.send_message(
            chat_id=app["user_id"],
            text=f"💬 Сообщение от Администрации:\n\n{message.text}",
        )
        await message.reply_text("✅ Сообщение отправлено.")
    except TelegramError:
        await message.reply_text(
            "❌ Не удалось написать пользователю. "
            "Возможно, он заблокировал бота."
        )


async def relay_user_message(update, context):
    message = update.effective_message
    user = update.effective_user

    if not message or not user:
        return False

    data = get_data(context)
    active = None

    for key, app in data["applications"].items():
        if (
            int(app["user_id"]) == user.id
            and app.get("contact_mode")
        ):
            active = (int(key), app)
            break

    if not active:
        return False

    app_id, app = active

    caption = (
        f"💬 <b>Сообщение по заявке №{app_id}</b>\n"
        f"👤 <a href=\"tg://user?id={user.id}\">"
        f"{esc(user.full_name)}</a>\n"
        f"🎮 Никнейм: <b>{esc(app['nickname'])}</b>"
    )

    await context.bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=caption,
        parse_mode="HTML",
    )

    await context.bot.copy_message(
        chat_id=ADMIN_CHAT_ID,
        from_chat_id=user.id,
        message_id=message.message_id,
    )

    await message.reply_text(
        "✅ Сообщение передано Администрации."
    )
    return True


async def other_private_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    chat = update.effective_chat

    if not message or not chat:
        return

    if chat.type != ChatType.PRIVATE:
        return

    if await relay_user_message(update, context):
        return

    if context.user_data.get("awaiting_photo"):
        await message.reply_text(
            "Пожалуйста, отправь фотографию изображением."
        )
    else:
        await message.reply_text(
            "Чтобы подать заявку на вступление, отправь /start."
        )


# ============================================================
# APPLICATION BUTTONS
# ============================================================

async def application_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    if not query or not query.message:
        return

    data = get_data(context)
    if query.message.chat.id != ADMIN_CHAT_ID:
        await query.answer()
        return
    if not has_access(query.from_user.id, data):
        await query.answer("❌ Нет доступа к управлению заявками.", show_alert=True)
        return

    try:
        _, action, app_id_text = query.data.split(":")
        app_id = int(app_id_text)
    except (ValueError, AttributeError):
        await query.answer()
        return

    app = data["applications"].get(str(app_id))
    if not app:
        await query.answer("Заявка не найдена.", show_alert=True)
        return

    if action == "reject":
        await query.answer()
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⏱ 24 часа", callback_data=f"app:reject24:{app_id}"),
             InlineKeyboardButton("📅 7 дней", callback_data=f"app:reject168:{app_id}")],
            [InlineKeyboardButton("♾ Навсегда", callback_data=f"app:rejectperm:{app_id}"),
             InlineKeyboardButton("↩️ Отмена", callback_data=f"app:rejectcancel:{app_id}")],
        ])
        await query.message.reply_text(
            f"Выбери срок блокировки подачи заявок для пользователя по заявке №{app_id}:",
            reply_markup=keyboard,
        )
        return

    if action == "rejectcancel":
        await query.answer("Отменено.")
        try:
            await query.message.delete()
        except TelegramError:
            pass
        return

    if action == "revoke":
        await query.answer()
        invite_link = app.get("invite_link")
        if app.get("status") != "accepted" or not invite_link:
            await query.message.reply_text("❌ Для этой заявки нет активной ссылки для отзыва.")
            return
        try:
            await context.bot.revoke_chat_invite_link(
                chat_id=MAIN_CHAT_ID,
                invite_link=invite_link,
            )
        except TelegramError:
            logger.exception("Не удалось отозвать ссылку приглашения")
            await query.message.reply_text(
                "❌ Не удалось отозвать ссылку. Проверь права бота и не была ли ссылка уже использована/отозвана."
            )
            return
        app["invite_revoked"] = True
        app["invite_revoked_at"] = datetime.now(timezone.utc).isoformat()
        app["invite_revoked_by"] = query.from_user.id
        log_action(data, query.from_user.id, "invite_revoked", app.get("user_id"), app_id)
        save_data(data)
        await query.message.edit_reply_markup(
            reply_markup=application_keyboard(app_id, status="accepted", has_invite=False)
        )
        await query.message.reply_text(f"🚫 Ссылка для заявки №{app_id} отозвана.")
        try:
            await context.bot.send_message(
                chat_id=app["user_id"],
                text="ℹ️ Ранее выданная ссылка для вступления отозвана Администрацией. Если это ошибка, свяжись с Администрацией.",
            )
        except TelegramError:
            pass
        return

    if action in ("accept", "reject24", "reject168", "rejectperm"):
        await query.answer()
        if app.get("status") != "pending":
            await query.message.reply_text("Эта заявка уже обработана.")
            return

        if action == "accept":
            try:
                invite = await context.bot.create_chat_invite_link(
                    chat_id=MAIN_CHAT_ID,
                    name=f"application-{app_id}",
                    member_limit=1,
                    expire_date=datetime.now(timezone.utc) + timedelta(days=1),
                )
            except TelegramError:
                logger.exception("Не удалось создать ссылку")
                await query.message.reply_text(
                    "❌ Не удалось создать ссылку. Проверь права бота в главной беседе."
                )
                return

            app["invite_link"] = invite.invite_link
            app["invite_revoked"] = False
            app["status"] = "accepted"
            app["processed_by"] = query.from_user.id
            app["processed_at"] = datetime.now(timezone.utc).isoformat()
            try:
                await context.bot.send_message(
                    chat_id=app["user_id"],
                    text=(
                        "✅ Твоя заявка одобрена!\n\n"
                        "Вот одноразовая ссылка для вступления (действует 24 часа):\n"
                        f"{invite.invite_link}\n\n"
                        "❗️ Не передавай ссылку другим пользователям!"
                    ),
                )
            except TelegramError:
                app["delivery_failed"] = True
                log_action(data, query.from_user.id, "application_accepted_delivery_failed", app["user_id"], app_id)
                save_data(data)
                await query.message.reply_text(
                    "⚠️ Заявка одобрена, но бот не смог отправить ссылку. Она сохранена в карточке заявки/данных."
                )
            else:
                app["delivery_failed"] = False
            log_action(data, query.from_user.id, "application_accepted", app["user_id"], app_id)

        else:
            app["status"] = "rejected"
            app["processed_by"] = query.from_user.id
            app["processed_at"] = datetime.now(timezone.utc).isoformat()
            hours = {"reject24": 24, "reject168": 168, "rejectperm": None}[action]
            duration_label = {"reject24": "24 часа", "reject168": "7 дней", "rejectperm": "навсегда"}[action]
            existing_block = get_user_block(data, app["user_id"])
            should_apply = existing_block is None
            if existing_block and existing_block.get("until") and hours is None:
                # Бессрочная блокировка заменяет временную.
                should_apply = True
            elif existing_block and existing_block.get("until") and hours is not None:
                try:
                    current_until = datetime.fromisoformat(existing_block["until"])
                    if current_until.tzinfo is None:
                        current_until = current_until.replace(tzinfo=timezone.utc)
                    requested_until = datetime.now(timezone.utc) + timedelta(hours=hours)
                    should_apply = current_until < requested_until
                except (TypeError, ValueError):
                    should_apply = False
            elif existing_block and not existing_block.get("until"):
                # Уже есть бессрочная блокировка — не сокращаем её срок.
                should_apply = False
            if should_apply:
                apply_user_block(
                    data,
                    app["user_id"],
                    hours=hours,
                    reason=f"Заявка №{app_id} отклонена",
                    blocked_by=query.from_user.id,
                )
            app["rejection_block"] = duration_label
            log_action(data, query.from_user.id, "application_rejected", app["user_id"], app_id, f"Блокировка: {duration_label}")
            try:
                await context.bot.send_message(
                    chat_id=app["user_id"],
                    text=(
                        "❌ К сожалению, твоя заявка на вступление отклонена Администрацией.\n\n"
                        f"Повторная подача заявок ограничена: {duration_label}."
                    ),
                )
            except TelegramError:
                pass

        save_data(data)
        keyboard = application_keyboard(
            app_id,
            status=app["status"],
            has_invite=bool(app.get("invite_link") and not app.get("invite_revoked")),
        )
        # При выборе срока отказа callback приходит из отдельного сообщения с вариантами,
        # поэтому обновляем именно исходную карточку заявки.
        if app.get("admin_message_id"):
            try:
                await context.bot.edit_message_reply_markup(
                    chat_id=ADMIN_CHAT_ID,
                    message_id=app["admin_message_id"],
                    reply_markup=keyboard,
                )
            except TelegramError:
                logger.exception("Не удалось обновить карточку заявки")
        if query.message.message_id != app.get("admin_message_id"):
            try:
                await query.message.edit_reply_markup(reply_markup=None)
            except TelegramError:
                pass
        await query.message.reply_text(
            "✅ Заявка одобрена." if action == "accept"
            else f"❌ Заявка отклонена. Блокировка: {duration_label}."
        )
        return

    if action == "contact":
        await query.answer()
        app["contact_mode"] = True
        app["contact_started_by"] = query.from_user.id
        log_action(data, query.from_user.id, "contact_started", app.get("user_id"), app_id)
        save_data(data)
        context.user_data["admin_contact_app_id"] = app_id
        try:
            await context.bot.send_message(
                chat_id=app["user_id"],
                text="💬 Администрация хочет связаться с тобой по поводу заявки. Напиши сюда своё сообщение.",
            )
        except TelegramError:
            await query.message.reply_text(
                "❌ Не удалось связаться с пользователем. Он должен открыть бота и нажать /start."
            )
            return
        await query.message.reply_text(
            f"💬 Диалог по заявке №{app_id} активирован.\n"
            "Отправляй сообщения в админ-чат — бот передаст их пользователю.\n"
            "Для завершения отправь /конец."
        )


async def end_contact(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    user = update.effective_user

    if not message or not user or not in_admin_chat(update):
        return

    data = get_data(context)

    if not has_access(user.id, data):
        await message.reply_text("❌ Доступ запрещён.")
        return

    app_id = context.user_data.pop("admin_contact_app_id", None)

    if not app_id:
        await message.reply_text("Нет активного диалога.")
        return

    app = data["applications"].get(str(app_id))

    if app:
        app["contact_mode"] = False
        app["contact_started_by"] = None
        log_action(data, user.id, "contact_ended", app.get("user_id"), app_id)
        save_data(data)

        try:
            await context.bot.send_message(
                chat_id=app["user_id"],
                text="ℹ️ Диалог с Администрацией завершён.",
            )
        except TelegramError:
            pass

    await message.reply_text("✅ Диалог завершён.")



async def block_applications_command(update, context):
    message = update.effective_message
    user = update.effective_user
    if not message or not user or not in_admin_chat(update):
        return

    data = get_data(context)
    if not has_access(user.id, data):
        await message.reply_text("❌ Доступ запрещён.")
        return

    target_id = get_replied_target(data, message)
    if target_id is None:
        await message.reply_text(
            "Ответь командой /банзаявки на сообщение пользователя "
            "или на карточку его заявки.\n"
            "Можно указать срок в часах: /банзаявки 48\n"
            "Для бессрочной блокировки: /банзаявки навсегда"
        )
        return

    if target_id == OWNER_ID:
        await message.reply_text("❌ Нельзя заблокировать владельца бота.")
        return

    args = message.text.split(maxsplit=1)
    duration = args[1].strip().lower() if len(args) > 1 else "24"
    if duration in ("навсегда", "вечная", "бессрочно"):
        hours = None
    else:
        try:
            hours = int(duration)
            if hours < 1 or hours > 8760:
                raise ValueError
        except ValueError:
            await message.reply_text(
                "❌ Укажи срок от 1 до 8760 часов или слово «навсегда»."
            )
            return

    block = apply_user_block(
        data,
        target_id,
        hours=hours,
        reason="Блокировка Администрацией",
        blocked_by=user.id,
    )
    log_action(data, user.id, "applications_blocked", target_id, details=format_block_expiry(block))
    await message.reply_text(
        f"🔒 Подача заявок для пользователя <code>{target_id}</code> "
        f"заблокирована.\n"
        f"Срок: {format_block_expiry(block)}.",
        parse_mode="HTML",
    )


async def unblock_applications_command(update, context):
    message = update.effective_message
    user = update.effective_user
    if not message or not user or not in_admin_chat(update):
        return

    data = get_data(context)
    if not has_access(user.id, data):
        await message.reply_text("❌ Доступ запрещён.")
        return

    target_id = get_replied_target(data, message)
    if target_id is None:
        await message.reply_text(
            "Ответь командой /разбанзаявки на сообщение пользователя "
            "или на карточку его заявки."
        )
        return

    data.setdefault("user_blocks", {}).pop(str(target_id), None)
    data["blocked_users"] = [
        int(x) for x in data.get("blocked_users", [])
        if int(x) != target_id
    ]
    log_action(data, user.id, "applications_unblocked", target_id)
    save_data(data)
    await message.reply_text(
        f"🔓 Блокировка подачи заявок для пользователя "
        f"<code>{target_id}</code> снята.",
        parse_mode="HTML",
    )


async def list_application_bans(update, context):
    message = update.effective_message
    user = update.effective_user
    if not message or not user or not in_admin_chat(update):
        return

    data = get_data(context)
    if not has_access(user.id, data):
        await message.reply_text("❌ Доступ запрещён.")
        return

    active = []
    all_ids = set(str(int(x)) for x in data.get("blocked_users", []))
    all_ids.update(data.get("user_blocks", {}).keys())

    for user_id_text in sorted(all_ids, key=lambda x: int(x)):
        user_id = int(user_id_text)
        block = get_user_block(data, user_id)
        if block:
            active.append(
                f"• <code>{user_id}</code> — "
                f"{esc(format_block_expiry(block))}; "
                f"{esc(block.get('reason', 'причина не указана'))}"
            )

    if not active:
        await message.reply_text("✅ Сейчас нет заблокированных пользователей.")
        return

    await message.reply_text(
        "🔒 <b>Заблокированные пользователи:</b>\n\n"
        + "\n".join(active),
        parse_mode="HTML",
    )


async def admin_help(update, context):
    message = update.effective_message
    user = update.effective_user
    if not message or not user or not in_admin_chat(update):
        return
    data = get_data(context)
    if not has_access(user.id, data):
        await message.reply_text("❌ Доступ запрещён.")
        return
    await message.reply_text(
        "🛠 <b>**ПАМЯТКА ПО ПРИНЯТИЮ ЗАЯВОК</b>\n\n"
        "<b>📝 Заявки и шаблон</b>\n"
        "/пмщ — показать эту памятку.\n"
        "/шаблон Текст — изменить текст шаблона (команда владельца).\n"
        "/шаблон_текущий — посмотреть текущий шаблон.\n"
        "/безфото — убрать фотографию из шаблона (команда владельца).\n"
        "/конец — завершить активный диалог с участником.\n\n"
        "<b>🔒 Блокировки заявок</b>\n"
        "Ответь на сообщение пользователя или карточку заявки командой /банзаявки 48 — блокировка на 48 часов.\n"
        "/банзаявки навсегда — бессрочная блокировка.\n"
        "/разбанзаявки — снять блокировку (ответом на пользователя/заявку).\n"
        "/списокбанов — показать активные блокировки.\n\n"
        "<b>📋 История</b>\n"
        "/журнал — последние действия Администрации.\n\n"
        "При отклонении заявки выбери срок: 24 часа, 7 дней или навсегда.\n"
        "Команды /add и /del доступны только владельцу и не включены в эту справку.",
        parse_mode="HTML",
    )


async def show_admin_log(update, context):
    message = update.effective_message
    user = update.effective_user
    if not message or not user or not in_admin_chat(update):
        return
    data = get_data(context)
    if not has_access(user.id, data):
        await message.reply_text("❌ Доступ запрещён.")
        return
    entries = data.get("admin_log", [])[-20:]
    if not entries:
        await message.reply_text("📘 Журнал пока пуст.")
        return
    lines = []
    for item in reversed(entries):
        at = item.get("at", "")
        try:
            dt = datetime.fromisoformat(at).astimezone()
            at = dt.strftime("%d.%m.%Y %H:%M")
        except (TypeError, ValueError):
            pass
        actor = item.get("actor_id") or "?"
        target = item.get("target_id")
        app_id = item.get("app_id")
        details = f" — {esc(item['details'])}" if item.get("details") else ""
        suffix = f"; участник <code>{target}</code>" if target else ""
        suffix += f"; заявка №{app_id}" if app_id else ""
        lines.append(f"• <code>{esc(at)}</code> — <b>{esc(item.get('action', 'действие'))}</b>; Администратор <code>{actor}</code>{suffix}{details}")
    await message.reply_text("📘 <b>Последние действия Администрации</b>\n\n" + "\n".join(lines), parse_mode="HTML")


# ============================================================
# RUSSIAN COMMAND ROUTER
# Русские команды обрабатываются вручную, поскольку Telegram
# поддерживает в именах команд только латинские буквы, цифры и _.
# ============================================================

async def russian_command_router(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message

    if not message or not message.text:
        return

    text = message.text.strip()
    command = text.split(maxsplit=1)[0].split("@")[0].lower()

    if command == "/пмщ":
        await admin_help(update, context)
    elif command == "/журнал":
        await show_admin_log(update, context)
    elif command == "/шаблон":
        await set_template(update, context)
    elif command == "/шаблон_текущий":
        await show_template(update, context)
    elif command == "/безфото":
        await remove_template_photo(update, context)
    elif command == "/конец":
        await end_contact(update, context)
    elif command == "/банзаявки":
        await block_applications_command(update, context)
    elif command == "/разбанзаявки":
        await unblock_applications_command(update, context)
    elif command == "/списокбанов":
        await list_application_bans(update, context)


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(update, context):
    logger.error(
        "Ошибка при обработке обновления",
        exc_info=context.error,
    )


def main():
    data = load_data()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.bot_data["data"] = data

    # Английские команды Telegram
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("add", add_admin))
    application.add_handler(CommandHandler("del", remove_admin))

    # Русские команды, включая команды с аргументами
    application.add_handler(
        MessageHandler(
            filters.Chat(ADMIN_CHAT_ID)
            & filters.TEXT
            & filters.Regex(
                r"^/(?:пмщ|журнал|шаблон(?:_текущий)?|безфото|конец|"
                r"банзаявки|разбанзаявки|списокбанов)"
                r"(?:@\w+)?(?:\s|$)"
            ),
            russian_command_router,
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            application_callback,
            pattern=r"^app:",
        )
    )

    # Фотографии пользователей
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.PHOTO,
            private_photo,
        )
    )

    # Фотографии для шаблона в админ-чате
    application.add_handler(
        MessageHandler(
            filters.Chat(ADMIN_CHAT_ID) & filters.PHOTO,
            template_photo_handler,
        )
    )

    # Текст пользователей и администраторов
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE
            & filters.TEXT
            & ~filters.COMMAND,
            private_text,
        )
    )

    application.add_handler(
        MessageHandler(
            filters.Chat(ADMIN_CHAT_ID)
            & filters.TEXT
            & ~filters.COMMAND,
            private_text,
        )
    )

    # Другие типы личных сообщений
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE
            & ~filters.COMMAND
            & ~filters.TEXT
            & ~filters.PHOTO,
            other_private_message,
        )
    )

    application.add_error_handler(error_handler)

    logger.info("Бот заявок запускается.")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
