import asyncio
import os
import logging

import aiohttp
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    BufferedInputFile,
    BotCommand, BotCommandScopeDefault, BotCommandScopeChat,
    ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.filters import CommandStart, Command

import database as db
from rss_fetcher import run_rss_fetcher

# ────────────────────────────────────────────
# SETUP
# ────────────────────────────────────────────

load_dotenv()

BOT_TOKEN                 = os.getenv("BOT_TOKEN")
ADMIN_IDS                 = [int(x.strip()) for x in os.getenv("ADMIN_IDS").split(",")]
CHANNEL_ID                = os.getenv("CHANNEL_ID")
BALE_BOT_TOKEN            = os.getenv("BALE_BOT_TOKEN")
BALE_CHANNEL_ID           = os.getenv("BALE_CHANNEL_ID")
BALE_CHANNEL_USERNAME     = os.getenv("BALE_CHANNEL_USERNAME")
TELEGRAM_CHANNEL_USERNAME = os.getenv("TELEGRAM_CHANNEL_USERNAME")
BALE_API_URL              = f"https://tapi.bale.ai/bot{BALE_BOT_TOKEN}"

channel_slug = (TELEGRAM_CHANNEL_USERNAME or "").lstrip("@")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()

BTN_HELP = "ℹ️ راهنما"
BTN_CHANNEL = "📣 کانال"
BTN_PENDING = "📥 صف بررسی"
BTN_STATUS = "📊 وضعیت"
BTN_BLOCK = "🚫 بلاک کاربر"
BTN_UNBLOCK = "✅ رفع بلاک"
BTN_ADMIN = "🛠 پنل ادمین"


# ────────────────────────────────────────────
# HELPERS
# ────────────────────────────────────────────

def make_channel_link(channel_msg_id: int) -> str | None:
    if not channel_slug or not channel_msg_id:
        return None
    return f"https://t.me/{channel_slug}/{channel_msg_id}"


def is_admin(user_id: int | None) -> bool:
    return user_id in ADMIN_IDS


def command_args(message: Message) -> list[str]:
    return (message.text or "").strip().split()[1:]


def parse_positive_int(value: str | None, default: int, minimum: int = 1, maximum: int = 50) -> int:
    if not value or not value.isdigit():
        return default
    return max(minimum, min(int(value), maximum))


def get_user_id_arg(message: Message) -> int | None:
    args = command_args(message)
    if args and args[0].isdigit():
        return int(args[0])
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user.id
    return None


def trim_text(text: str | None, limit: int = 220) -> str:
    text = (text or "").strip()
    if not text:
        return "(بدون متن)"
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def separator(width: int = 28) -> str:
    return "─" * width


def build_user_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_HELP), KeyboardButton(text=BTN_CHANNEL)],
        ],
        resize_keyboard=True,
        input_field_placeholder="خبر، عکس، ویدئو یا لینک را ارسال کنید",
    )


def build_admin_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_PENDING), KeyboardButton(text=BTN_STATUS)],
            [KeyboardButton(text=BTN_BLOCK), KeyboardButton(text=BTN_UNBLOCK)],
            [KeyboardButton(text=BTN_ADMIN), KeyboardButton(text=BTN_HELP)],
        ],
        resize_keyboard=True,
        input_field_placeholder="یک عملیات مدیریتی انتخاب کنید",
    )


def get_main_keyboard(user_id: int | None) -> ReplyKeyboardMarkup:
    return build_admin_reply_keyboard() if is_admin(user_id) else build_user_keyboard()


def build_admin_home_message(stats: dict) -> str:
    return "\n".join([
        "🛠 پنل ادمین",
        separator(),
        f"⏳ در انتظار بررسی: {stats['pending']}",
        f"✅ منتشر شده امروز: {stats['published_today']}",
        f"❌ رد شده امروز: {stats['rejected_today']}",
        f"📡 RSS امروز: {stats['rss_today']}",
        separator(),
        "دستورهای سریع:",
        "/pending — نمایش ۱۰ پست قدیمی در صف",
        "/pending 20 — نمایش تعداد بیشتر، تا سقف ۵۰",
        "/status — گزارش کامل سیستم",
        "/block <user_id> [reason] — بلاک کاربر",
        "/unblock <user_id> — رفع بلاک",
        "",
        "برای بلاک/آنبلاک، دکمه را بزنید تا قالب آماده بگیرید.",
        "نکته: برای بلاک/آنبلاک می‌توانید روی پیام کاربر reply کنید و دستور را بدون user_id بفرستید.",
    ])


def build_user_help_message() -> str:
    channel_line = (
        f"کانال: @{channel_slug}"
        if channel_slug else
        "کانال پس از تأیید ادمین به‌روزرسانی می‌شود."
    )
    return "\n".join([
        "راهنمای ارسال خبر",
        separator(),
        "خبر، عکس، ویدئو یا لینک را همین‌جا برای بات بفرستید.",
        "بعد از بررسی ادمین، در صورت تأیید در کانال منتشر می‌شود.",
        "",
        channel_line,
        f"فاصله مجاز بین ارسال‌ها: {db.RATE_LIMIT_SECONDS} ثانیه",
    ])


async def setup_bot_commands():
    user_commands = [
        BotCommand(command="start", description="شروع و معرفی بات"),
        BotCommand(command="help", description="راهنمای ارسال خبر"),
    ]
    admin_commands = [
        BotCommand(command="admin", description="پنل سریع ادمین"),
        BotCommand(command="pending", description="بررسی پست‌های در انتظار"),
        BotCommand(command="status", description="گزارش وضعیت سیستم"),
        BotCommand(command="block", description="بلاک کاربر"),
        BotCommand(command="unblock", description="رفع بلاک کاربر"),
        BotCommand(command="help", description="راهنمای بات"),
    ]

    await bot.set_my_commands(user_commands, scope=BotCommandScopeDefault())
    for admin_id in ADMIN_IDS:
        await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=admin_id))


def build_admin_info_message(post, channel_link: str | None) -> str:
    post_id      = post["id"]
    content_type = post["content_type"]
    sim_flag     = post["similarity_flag"]
    sim_post_id  = post["similar_post_id"]
    source_type  = post["source_type"] or "user"
    source_name  = post["source_name"]
    source_url   = post["source_url"]

    if source_type == "rss":
        origin = f"📡 RSS  •  {source_name or 'بدون نام'}"
        if source_url:
            origin += f"\n🔗 {source_url}"
    else:
        origin = f"👤 User  •  {post['user_id']}"

    lines = [
        f"📬 Post #{post_id}  |  📄 {content_type}",
        origin,
        "─" * 32,
    ]

    if sim_flag == "DUPLICATE":
        lines.append(f"🚨 DUPLICATE of post #{sim_post_id}")
        if channel_link:
            lines.append(f"🔗 {channel_link}")
        else:
            lines.append("⚠️ Original was not published to channel yet.")
    elif sim_flag and sim_flag.startswith("SIMILAR:"):
        percent = sim_flag.split(":")[1]
        lines.append(f"⚠️ Similar to post #{sim_post_id}  ({percent}% word overlap)")
        if channel_link:
            lines.append(f"🔗 {channel_link}")
    else:
        lines.append("✅ No issues found.")

    return "\n".join(lines)


def build_admin_keyboard(post_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Approve", callback_data=f"approve_{post_id}"),
            InlineKeyboardButton(text="❌ Reject",  callback_data=f"reject_{post_id}"),
        ]
    ])


# ────────────────────────────────────────────
# ADMIN COMMANDS
# ────────────────────────────────────────────

@dp.message(Command("admin"))
async def cmd_admin_home(message: Message):
    """Show the admin command center."""
    if not is_admin(message.from_user.id):
        await message.answer(
            build_user_help_message(),
            reply_markup=build_user_keyboard(),
        )
        return

    stats = await db.get_stats()
    await message.answer(
        build_admin_home_message(stats),
        reply_markup=build_admin_reply_keyboard(),
    )


@dp.message(Command("help"))
async def cmd_help(message: Message):
    if is_admin(message.from_user.id):
        stats = await db.get_stats()
        await message.answer(
            build_admin_home_message(stats),
            reply_markup=build_admin_reply_keyboard(),
        )
        return

    await message.answer(
        build_user_help_message(),
        reply_markup=build_user_keyboard(),
    )


@dp.message(Command("status"))
async def cmd_status(message: Message):
    """Show a live system summary to the admin."""
    if not is_admin(message.from_user.id):
        return

    stats = await db.get_stats()

    text = "\n".join([
        "📊 وضعیت سیستم",
        separator(),
        f"⏳ در انتظار تأیید: {stats['pending']}",
        "",
        "امروز:",
        f"👤 ارسال کاربران: {stats['user_submitted_today']}",
        f"📡 دریافت از RSS: {stats['rss_today']}",
        f"✅ منتشر شده: {stats['published_today']}",
        f"❌ رد شده: {stats['rejected_today']}",
        "",
        f"📡 منابع RSS فعال: {stats['active_sources']}",
        f"🚫 کاربران بلاک: {stats['blocked_users']}",
        separator(),
        "برای عملیات سریع: /admin",
    ])
    await message.answer(text, reply_markup=build_admin_reply_keyboard())

@dp.message(Command("pending"))
async def cmd_pending(message: Message):
    """Show the oldest pending posts with direct action buttons."""
    if not is_admin(message.from_user.id):
        return

    limit = parse_positive_int(command_args(message)[0] if command_args(message) else None, default=10)
    posts = await db.get_pending_posts(limit=limit)

    if not posts:
        await message.answer(
            "✅ صف بررسی خالی است. فعلاً کاری برای تأیید یا رد وجود ندارد.",
            reply_markup=build_admin_reply_keyboard(),
        )
        return

    stats = await db.get_stats()
    queue_note = f"{len(posts)} از {stats['pending']}" if stats["pending"] > len(posts) else str(len(posts))
    await message.answer(
        f"⏳ {queue_note} پست در صف بررسی، قدیمی‌ترین‌ها اول:",
        reply_markup=build_admin_reply_keyboard(),
    )

    for post in posts:
        post_id      = post["id"]
        source_type  = post["source_type"] or "user"
        source_name  = post["source_name"] or ""
        content_type = post["content_type"]

        if source_type == "rss":
            origin = f"📡 RSS — {source_name or 'بدون نام'}"
        else:
            origin = f"👤 User — {post['user_id'] or '?'}"

        summary = "\n".join([
            f"📬 پست #{post_id}",
            origin,
            f"نوع: {content_type}",
            separator(24),
            trim_text(post["text"]),
        ])

        await message.answer(
            summary,
            reply_markup=build_admin_keyboard(post_id)
        )


@dp.message(Command("block"))
async def cmd_block(message: Message):
    """
    Block a user: /block 123456789
    Blocked users can't submit posts.
    """
    if not is_admin(message.from_user.id):
        return

    user_id = get_user_id_arg(message)
    if user_id is None:
        await message.answer(
            "استفاده:\n/block <user_id> [reason]\n\n"
            "یا روی پیام کاربر reply کنید و بنویسید:\n/block [reason]",
            reply_markup=build_admin_reply_keyboard(),
        )
        return

    args = command_args(message)
    reason_parts = args[1:] if args and args[0].isdigit() else args
    reason = " ".join(reason_parts).strip()

    await db.block_user(user_id, reason)
    response = f"🚫 کاربر {user_id} بلاک شد."
    if reason:
        response += f"\nدلیل ثبت‌شده: {reason}"
    await message.answer(response, reply_markup=build_admin_reply_keyboard())

    # Try to notify the blocked user
    try:
        await bot.send_message(
            user_id,
            "⛔ دسترسی شما به ارسال خبر محدود شده است."
        )
    except Exception as e:
        logger.info("Could not notify blocked user %s: %s", user_id, e)
        await message.answer(
            "⚠️ کاربر بلاک شد، اما پیام اطلاع‌رسانی به خودش ارسال نشد.",
            reply_markup=build_admin_reply_keyboard(),
        )


@dp.message(Command("unblock"))
async def cmd_unblock(message: Message):
    """Unblock a user: /unblock 123456789"""
    if not is_admin(message.from_user.id):
        return

    user_id = get_user_id_arg(message)
    if user_id is None:
        await message.answer(
            "استفاده:\n/unblock <user_id>\n\n"
            "یا روی پیام کاربر reply کنید و بنویسید:\n/unblock",
            reply_markup=build_admin_reply_keyboard(),
        )
        return

    await db.unblock_user(user_id)
    await message.answer(f"✅ کاربر {user_id} آنبلاک شد.", reply_markup=build_admin_reply_keyboard())

    try:
        await bot.send_message(
            user_id,
            "✅ محدودیت ارسال شما برداشته شد. می‌توانید دوباره خبر ارسال کنید."
        )
    except Exception as e:
        logger.info("Could not notify unblocked user %s: %s", user_id, e)
        await message.answer(
            "⚠️ کاربر آنبلاک شد، اما پیام اطلاع‌رسانی به خودش ارسال نشد.",
            reply_markup=build_admin_reply_keyboard(),
        )


# ────────────────────────────────────────────
# GUARD: runs before every user submission
# Returns True if allowed, False if blocked or rate-limited
# Admins are always exempt from rate limiting
# ────────────────────────────────────────────

async def check_user_allowed(message: Message) -> bool:
    user_id = message.from_user.id

    # Admins bypass all restrictions
    if user_id in ADMIN_IDS:
        return True

    # 1. Blocked?
    if await db.is_user_blocked(user_id):
        await message.answer(
            "⛔ دسترسی شما به ارسال خبر محدود شده است.",
            reply_markup=build_user_keyboard(),
        )
        return False

    # 2. Rate limit: must wait 1 minute between posts
    wait_seconds = db.check_rate_limit(user_id)
    if wait_seconds > 0:
        await message.answer(
            f"⏳ لطفاً {wait_seconds} ثانیه صبر کنید و دوباره ارسال کنید.",
            reply_markup=build_user_keyboard(),
        )
        return False

    return True


# ────────────────────────────────────────────
# REPLY KEYBOARD ACTIONS
# ────────────────────────────────────────────

@dp.message(F.text == BTN_ADMIN)
async def handle_admin_button(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer(build_user_help_message(), reply_markup=build_user_keyboard())
        return

    stats = await db.get_stats()
    await message.answer(build_admin_home_message(stats), reply_markup=build_admin_reply_keyboard())


@dp.message(F.text == BTN_HELP)
async def handle_help_button(message: Message):
    if is_admin(message.from_user.id):
        stats = await db.get_stats()
        await message.answer(build_admin_home_message(stats), reply_markup=build_admin_reply_keyboard())
        return

    await message.answer(build_user_help_message(), reply_markup=build_user_keyboard())


@dp.message(F.text == BTN_CHANNEL)
async def handle_channel_button(message: Message):
    if channel_slug:
        await message.answer(f"📣 کانال:\nhttps://t.me/{channel_slug}", reply_markup=get_main_keyboard(message.from_user.id))
        return

    await message.answer(
        "هنوز آدرس کانال در تنظیمات بات ثبت نشده است.",
        reply_markup=get_main_keyboard(message.from_user.id),
    )


@dp.message(F.text == BTN_PENDING)
async def handle_pending_button(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer(build_user_help_message(), reply_markup=build_user_keyboard())
        return

    await cmd_pending(message)


@dp.message(F.text == BTN_STATUS)
async def handle_status_button(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer(build_user_help_message(), reply_markup=build_user_keyboard())
        return

    await cmd_status(message)


@dp.message(F.text == BTN_BLOCK)
async def handle_block_button(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer(build_user_help_message(), reply_markup=build_user_keyboard())
        return

    await message.answer(
        "برای بلاک کردن یکی از این دو روش را بزنید:\n\n"
        "/block <user_id> [reason]\n"
        "یا روی پیام کاربر reply کنید و بنویسید:\n"
        "/block [reason]",
        reply_markup=build_admin_reply_keyboard(),
    )


@dp.message(F.text == BTN_UNBLOCK)
async def handle_unblock_button(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer(build_user_help_message(), reply_markup=build_user_keyboard())
        return

    await message.answer(
        "برای رفع بلاک یکی از این دو روش را بزنید:\n\n"
        "/unblock <user_id>\n"
        "یا روی پیام کاربر reply کنید و بنویسید:\n"
        "/unblock",
        reply_markup=build_admin_reply_keyboard(),
    )


# ────────────────────────────────────────────
# USER: RECEIVE SUBMISSIONS
# ────────────────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "👋 خوش آمدید!\n\n"
        "خبر خود را ارسال کنید — متن، عکس، ویدئو یا لینک —\n"
        "و پس از بررسی ادمین در کانال منتشر خواهد شد.\n\n"
        f"⚠️ بین هر ارسال حداقل {db.RATE_LIMIT_SECONDS} ثانیه فاصله لازم است.",
        reply_markup=get_main_keyboard(message.from_user.id),
    )


@dp.message(F.photo)
async def handle_photo(message: Message):
    if not await check_user_allowed(message):
        return

    file_id = message.photo[-1].file_id
    caption = message.caption or ""

    post_id = await db.add_post(
        content_type="photo",
        text=caption,
        user_id=message.from_user.id,
        file_id=file_id,
        source_type="user",
    )
    db.record_submission(message.from_user.id)
    await db.log_action(post_id, "submitted")
    post = await db.get_post(post_id)

    await notify_user_if_flagged(message, post)
    await message.answer(
        "✅ پست شما (عکس) دریافت شد و در انتظار بررسی ادمین است.",
        reply_markup=get_main_keyboard(message.from_user.id),
    )
    await send_to_admin(post_id)


@dp.message(F.video)
async def handle_video(message: Message):
    if not await check_user_allowed(message):
        return

    file_id = message.video.file_id
    caption = message.caption or ""

    post_id = await db.add_post(
        content_type="video",
        text=caption,
        user_id=message.from_user.id,
        file_id=file_id,
        source_type="user",
    )
    db.record_submission(message.from_user.id)
    await db.log_action(post_id, "submitted")
    post = await db.get_post(post_id)

    await notify_user_if_flagged(message, post)
    await message.answer(
        "✅ پست شما (ویدئو) دریافت شد و در انتظار بررسی ادمین است.",
        reply_markup=get_main_keyboard(message.from_user.id),
    )
    await send_to_admin(post_id)


@dp.message(F.text & ~F.text.regexp(r"^/"))
async def handle_text(message: Message):
    if not await check_user_allowed(message):
        return

    text = message.text
    content_type = "link" if text.strip().lower().startswith("http") else "text"

    post_id = await db.add_post(
        content_type=content_type,
        text=text,
        user_id=message.from_user.id,
        source_type="user",
    )
    db.record_submission(message.from_user.id)
    await db.log_action(post_id, "submitted")
    post = await db.get_post(post_id)

    await notify_user_if_flagged(message, post)
    await message.answer(
        "✅ پست شما دریافت شد و در انتظار بررسی ادمین است.",
        reply_markup=get_main_keyboard(message.from_user.id),
    )
    await send_to_admin(post_id)


async def notify_user_if_flagged(message: Message, post):
    sim_flag    = post["similarity_flag"]
    sim_post_id = post["similar_post_id"]

    if not sim_flag:
        return

    if sim_flag == "DUPLICATE":
        await message.answer(
            f"ℹ️ این خبر مشابه پست قبلاً منتشرشده‌ای است (#{sim_post_id}).\n"
            f"با این حال برای بررسی به ادمین ارسال شد."
        )
    elif sim_flag.startswith("SIMILAR:"):
        percent = sim_flag.split(":")[1]
        await message.answer(
            f"ℹ️ این خبر {percent}٪ شبیه پست قبلاً منتشرشده‌ای است (#{sim_post_id}).\n"
            f"با این حال برای بررسی به ادمین ارسال شد."
        )


# ────────────────────────────────────────────
# SEND TO ADMIN — TWO MESSAGES
# ────────────────────────────────────────────

async def send_to_admin(post_id: int):
    post = await db.get_post(post_id)
    if not post:
        return

    content_type = post["content_type"]
    text         = post["text"] or ""
    file_id      = post["file_id"]
    sim_post_id  = post["similar_post_id"]

    channel_link = None
    if sim_post_id:
        original = await db.get_post(sim_post_id)
        if original and original["channel_msg_id"]:
            channel_link = make_channel_link(original["channel_msg_id"])

    info_text = build_admin_info_message(post, channel_link)
    keyboard  = build_admin_keyboard(post_id)

    for admin_id in ADMIN_IDS:
        try:
            # Message 1: raw content
            if content_type == "photo" and file_id:
                await bot.send_photo(chat_id=admin_id, photo=file_id, caption=text or None)
            elif content_type == "video" and file_id:
                await bot.send_video(chat_id=admin_id, video=file_id, caption=text or None)
            else:
                await bot.send_message(chat_id=admin_id, text=text)

            # Message 2: info card + buttons
            await bot.send_message(chat_id=admin_id, text=info_text, reply_markup=keyboard)

        except Exception as e:
            logging.warning(f"Could not message admin {admin_id}: {e}")


# ────────────────────────────────────────────
# ADMIN: APPROVE
# ────────────────────────────────────────────

@dp.callback_query(F.data.startswith("approve_"))
async def handle_approve(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ شما مجاز نیستید.", show_alert=True)
        return

    post_id = int(callback.data.split("_")[1])
    post    = await db.get_post(post_id)

    if post is None:
        await callback.answer("پست پیدا نشد.", show_alert=True)
        return

    if post["status"] != "pending":
        await callback.answer(f"این پست قبلاً '{post['status']}' شده.", show_alert=True)
        return

    await callback.answer("در حال انتشار…")

    await db.update_status(post_id, "approved")
    await db.log_action(post_id, "approved")

    channel_msg_id = await publish_to_telegram_channel(
        post["content_type"], post["text"], post["file_id"]
    )
    if not channel_msg_id:
        await db.update_status(post_id, "pending")
        await db.log_action(post_id, "publish_failed")
        await callback.message.reply(
            f"⚠️ پست #{post_id} تأیید شد، اما انتشار در کانال شکست خورد.\n"
            "وضعیت دوباره به pending برگشت تا بعداً بتوانید دوباره تأیید کنید."
        )
        return

    await db.save_channel_msg_id(post_id, channel_msg_id)

    await db.update_status(post_id, "published")
    await db.log_action(post_id, "published")

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.reply(f"✅ پست #{post_id} تأیید و منتشر شد.")

    if post["user_id"]:
        try:
            await bot.send_message(
                post["user_id"],
                f"🎉 پست شما (#{post_id}) تأیید و در کانال منتشر شد!"
            )
        except Exception as e:
            logging.warning(f"Could not notify user {post['user_id']}: {e}")


# ────────────────────────────────────────────
# ADMIN: REJECT
# ────────────────────────────────────────────

@dp.callback_query(F.data.startswith("reject_"))
async def handle_reject(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ شما مجاز نیستید.", show_alert=True)
        return

    post_id = int(callback.data.split("_")[1])
    post    = await db.get_post(post_id)

    if post is None:
        await callback.answer("پست پیدا نشد.", show_alert=True)
        return

    if post["status"] != "pending":
        await callback.answer(f"این پست قبلاً '{post['status']}' شده.", show_alert=True)
        return

    await db.update_status(post_id, "rejected")
    await db.log_action(post_id, "rejected")

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.reply(f"❌ پست #{post_id} رد شد.")
    await callback.answer("رد شد.")

    if post["user_id"]:
        try:
            await bot.send_message(
                post["user_id"],
                f"😔 پست شما (#{post_id}) بررسی شد اما برای انتشار انتخاب نشد."
            )
        except Exception as e:
            logging.warning(f"Could not notify user {post['user_id']}: {e}")


# ────────────────────────────────────────────
# PUBLISHING
# ────────────────────────────────────────────

async def publish_to_telegram_channel(content_type: str, text: str, file_id: str) -> int | None:
    try:
        if content_type == "photo" and file_id:
            sent = await bot.send_photo(chat_id=CHANNEL_ID, photo=file_id, caption=text or None)
        elif content_type == "video" and file_id:
            sent = await bot.send_video(chat_id=CHANNEL_ID, video=file_id, caption=text or None)
        else:
            sent = await bot.send_message(chat_id=CHANNEL_ID, text=text)
        return sent.message_id
    except Exception as e:
        logging.error(f"Failed to publish to Telegram channel: {e}")
        return None


# ────────────────────────────────────────────
# BALE LISTENER
# ────────────────────────────────────────────

async def handle_bale_channel_post(post: dict, session: aiohttp.ClientSession):
    text     = post.get("text") or post.get("caption") or ""
    new_text = text.replace(BALE_CHANNEL_USERNAME, TELEGRAM_CHANNEL_USERNAME)

    if "photo" in post:
        file_id = post["photo"][-1]["file_id"]
        async with session.get(f"{BALE_API_URL}/getFile", params={"file_id": file_id}) as resp:
            file_data = await resp.json()
        if not file_data.get("ok"):
            return
        file_path = file_data["result"]["file_path"]
        async with session.get(f"https://tapi.bale.ai/file/bot{BALE_BOT_TOKEN}/{file_path}") as resp:
            photo_bytes = await resp.read()
        await bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=BufferedInputFile(photo_bytes, filename="photo.jpg"),
            caption=new_text
        )
    elif "video" in post:
        file_id = post["video"]["file_id"]
        async with session.get(f"{BALE_API_URL}/getFile", params={"file_id": file_id}) as resp:
            file_data = await resp.json()
        if not file_data.get("ok"):
            return
        file_path = file_data["result"]["file_path"]
        async with session.get(f"https://tapi.bale.ai/file/bot{BALE_BOT_TOKEN}/{file_path}") as resp:
            video_bytes = await resp.read()
        await bot.send_video(
            chat_id=CHANNEL_ID,
            video=BufferedInputFile(video_bytes, filename="video.mp4"),
            caption=new_text
        )
    else:
        if new_text.strip():
            await bot.send_message(chat_id=CHANNEL_ID, text=new_text)


async def bale_listener():
    offset = 0
    async with aiohttp.ClientSession() as session:
        logging.info("Bale listener started.")
        while True:
            try:
                params = {"offset": offset, "timeout": 30}
                async with session.get(
                    f"{BALE_API_URL}/getUpdates", params=params,
                    timeout=aiohttp.ClientTimeout(total=40)
                ) as resp:
                    data = await resp.json()

                if not data.get("ok"):
                    await asyncio.sleep(5)
                    continue

                for update in data.get("result", []):
                    offset  = update["update_id"] + 1
                    msg     = update.get("message")
                    if msg and msg.get("chat", {}).get("type") == "channel":
                        try:
                            await handle_bale_channel_post(msg, session)
                        except Exception as e:
                            logging.error(f"Bale forward error: {e}")

            except Exception as e:
                logging.error(f"Bale listener error: {e}")
                await asyncio.sleep(5)


# ────────────────────────────────────────────
# MAIN
# ────────────────────────────────────────────

async def main():
    await db.init_db()
    try:
        await setup_bot_commands()
        logging.info("Bot command menu configured.")
    except Exception as e:
        logging.warning(f"Could not configure bot command menu: {e}")
    logging.info("Database ready. Bot is starting…")
    await asyncio.gather(
        dp.start_polling(bot),
        bale_listener(),
        run_rss_fetcher(notify_callback=send_to_admin),
    )


if __name__ == "__main__":
    asyncio.run(main())
