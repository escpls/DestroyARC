import html
import json
import logging
import os
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


def application_keyboard(app_id, status="pending"):
    rows = []

    if status == "pending":
        rows.append([
            InlineKeyboardButton(
                "✅ Принять",
                callback_data=f"app:accept:{app_id}",
            ),
            InlineKeyboardButton(
                "❌ Отклонить",
                callback_data=f"app:reject:{app_id}",
            ),
        ])

    rows.append([
        InlineKeyboardButton(
            "💬 Написать пользователю",
            callback_data=f"app:contact:{app_id}",
        )
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

    if user.id in {int(x) for x in data["blocked_users"]}:
        await message.reply_text(
            "❌ Вы не можете подать заявку через этого бота."
        )
        return

    context.user_data.pop("awaiting_nickname", None)
    context.user_data.pop("application_photo", None)
    context.user_data["awaiting_photo"] = True

    await send_template(chat.id, data, context)

    await message.reply_text(
        "📷 Отправь свою фотографию следующим сообщением. "
        "После этого я попрошу твой никнейм."
    )


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

    # /шаблон ответом на фотографию
    if message.reply_to_message and message.reply_to_message.photo:
        photo_message = message.reply_to_message

        data["template_photo"] = (
            photo_message.photo[-1].file_id
        )

        if photo_message.caption:
            data["template_text"] = photo_message.caption

        save_data(data)
        await message.reply_text("✅ Шаблон сохранён.")
        return

    # /шаблон Текст шаблона
    text = " ".join(context.args).strip()

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
        "Либо отправь фотографию с подписью, начинающейся "
        "с /шаблон.\n\n"
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

    # Фотография с подписью /шаблон Текст
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

    # Фотография после команды /шаблон Текст
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
    app_id, app = find_application(data, replied.message_id)

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
    save_data(data)

    # Пытаемся уведомить пользователя, но права сохраняются,
    # даже если он ещё не запускал бота.
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
    app_id, app = find_application(data, replied.message_id)

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
        # Ответ пользователя в активном диалоге с администрацией
        if await relay_user_message(update, context):
            return

        if not context.user_data.get("awaiting_nickname"):
            await message.reply_text(
                "Чтобы подать заявку, отправь /start."
            )
            return

        nickname = message.text.strip()

        if not nickname or nickname.startswith("/"):
            await message.reply_text(
                "Отправь никнейм обычным текстовым сообщением."
            )
            return

        photo_id = context.user_data.get("application_photo")

        if not photo_id:
            context.user_data.pop("awaiting_nickname", None)
            context.user_data["awaiting_photo"] = True

            await message.reply_text(
                "Сначала отправь фотографию."
            )
            return

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
        }

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
            f"🆔 ID пользователя: <code>{user.id}</code>"
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
            "✅ Твоя заявка отправлена администрации на рассмотрение."
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

    if message.text.startswith("/"):
        return

    try:
        await context.bot.send_message(
            chat_id=app["user_id"],
            text=f"💬 Сообщение от администрации:\n\n{message.text}",
        )
        await message.reply_text("✅ Сообщение отправлено.")
    except TelegramError:
        await message.reply_text(
            "❌ Не удалось написать пользователю. "
            "Возможно, он не запускал бота или заблокировал его."
        )


async def relay_user_message(update, context):
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return False

    data = get_data(context)

    # Находим активный диалог по Telegram ID пользователя.
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

    # Копируем оригинал: сохраняются фотографии, документы,
    # стикеры, голосовые сообщения и другие поддерживаемые типы.
    await context.bot.copy_message(
        chat_id=ADMIN_CHAT_ID,
        from_chat_id=user.id,
        message_id=message.message_id,
    )

    await message.reply_text("✅ Сообщение передано администрации.")
    return True


async def other_private_message(
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
        await query.answer(
            "❌ Нет доступа к управлению заявками.",
            show_alert=True,
        )
        return

    await query.answer()

    try:
        _, action, app_id_text = query.data.split(":")
        app_id = int(app_id_text)
    except (ValueError, AttributeError):
        return

    app = data["applications"].get(str(app_id))

    if not app:
        await query.message.reply_text("Заявка не найдена.")
        return

    if action in ("accept", "reject"):
        if app["status"] != "pending":
            await query.message.reply_text(
                "Эта заявка уже обработана."
            )
            return

        if action == "accept":
            try:
                invite = await context.bot.create_chat_invite_link(
    chat_id=MAIN_CHAT_ID,
    member_limit=1,
    expire_date=datetime.now(timezone.utc) + timedelta(days=1),
)

                    name=f"application-{app_id}",
                )

                await context.bot.send_message(
                    chat_id=app["user_id"],
                    text=(
                        "✅ Твоя заявка одобрена!\n\n"
                        "Вот одноразовая ссылка для вступления:\n"
                        f"{invite.invite_link}\n\n"
                        "Не передавай ссылку другим пользователям!"
                    ),
                )

                app["status"] = "accepted"
                app["invite_link"] = invite.invite_link

            except TelegramError:
                logger.exception("Не удалось принять заявку")

                await query.message.reply_text(
                    "❌ Не удалось создать ссылку или отправить её. "
                    "Проверь права бота в беседе и доступность пользователя."
                )
                return

        else:
            app["status"] = "rejected"

            try:
                await context.bot.send_message(
                    chat_id=app["user_id"],
                    text=(
                        "❌ К сожалению, твоя заявка "
                        "на вступление отклонена Администрацией."
                    ),
                )
            except TelegramError:
                pass

        app["processed_by"] = query.from_user.id
        save_data(data)

        await query.message.edit_reply_markup(
            reply_markup=application_keyboard(
                app_id,
                status=app["status"],
            )
        )

        await query.message.reply_text(
            "✅ Заявка принята."
            if action == "accept"
            else "❌ Заявка отклонена."
        )
        return

    if action == "contact":
        app["contact_mode"] = True
        app["contact_started_by"] = query.from_user.id
        save_data(data)

        context.user_data["admin_contact_app_id"] = app_id

        try:
            await context.bot.send_message(
                chat_id=app["user_id"],
                text=(
                    "💬 Администрация хочет связаться с тобой "
                    "по поводу заявки. Напиши сюда своё сообщение."
                ),
            )
        except TelegramError:
            await query.message.reply_text(
                "❌ Не удалось связаться с пользователем. "
                "Он должен открыть бота и нажать /start."
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
        save_data(data)

        try:
            await context.bot.send_message(
                chat_id=app["user_id"],
                text="ℹ️ Диалог с администрацией завершён.",
            )
        except TelegramError:
            pass

    await message.reply_text("✅ Диалог завершён.")


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

    # Команды
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("шаблон", set_template))
    application.add_handler(
        CommandHandler("шаблон_текущий", show_template)
    )
    application.add_handler(
        CommandHandler("безфото", remove_template_photo)
    )
    application.add_handler(CommandHandler("add", add_admin))
    application.add_handler(CommandHandler("del", remove_admin))
    application.add_handler(CommandHandler("конец", end_contact))

    application.add_handler(
        CallbackQueryHandler(
            application_callback,
            pattern=r"^app:",
        )
    )

    # Фотографии: личные сообщения и настройка шаблона
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.PHOTO,
            private_photo,
        )
    )

    application.add_handler(
        MessageHandler(
            filters.Chat(ADMIN_CHAT_ID) & filters.PHOTO,
            template_photo_handler,
        )
    )

    # Текстовые сообщения в личном чате с ботом
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE
            & filters.TEXT
            & ~filters.COMMAND,
            private_text,
        )
    )

    # Текстовые сообщения администраторов
    application.add_handler(
        MessageHandler(
            filters.Chat(ADMIN_CHAT_ID)
            & filters.TEXT
            & ~filters.COMMAND,
            private_text,
        )
    )

    # Остальные типы сообщений пользователя
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
