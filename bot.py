import asyncio
import os
import logging

import aiohttp
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    BufferedInputFile
)
from aiogram.filters import CommandStart

import database as db

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

# e.g. "@botetesterym" → "botetesterym"
channel_slug = (TELEGRAM_CHANNEL_USERNAME or "").lstrip("@")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()


# ────────────────────────────────────────────
# HELPERS
# ────────────────────────────────────────────

def make_channel_link(channel_msg_id: int) -> str | None:
    if not channel_slug or not channel_msg_id:
        return None
    return f"https://t.me/{channel_slug}/{channel_msg_id}"


def build_admin_info_message(post, channel_link: str | None) -> str:
    """
    Compact, clear second message for admins.
    One block of info — no redundant sentences.
    """
    post_id      = post["id"]
    user_id      = post["user_id"]
    content_type = post["content_type"]
    sim_flag     = post["similarity_flag"]
    sim_post_id  = post["similar_post_id"]

    # Header line
    lines = [
        f"📬 Post #{post_id}  |  👤 {user_id}  |  📄 {content_type}",
        "─" * 32,
    ]

    # Quality section
    if sim_flag == "DUPLICATE":
        lines.append(f"🚨 DUPLICATE of post #{sim_post_id}")
        if channel_link:
            lines.append(f"🔗 {channel_link}")
        else:
            lines.append(f"⚠️ Original post was not published to channel yet.")

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
# USER: RECEIVE SUBMISSIONS
# ────────────────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "👋 Welcome!\n\n"
        "Send me a news post — text, photo, video, or a link — "
        "and it will be reviewed by an admin before publishing."
    )


@dp.message(F.photo)
async def handle_photo(message: Message):
    file_id = message.photo[-1].file_id
    caption = message.caption or ""

    post_id = await db.add_post(
        user_id=message.from_user.id,
        content_type="photo",
        text=caption,
        file_id=file_id
    )
    await db.log_action(post_id, "submitted")
    post = await db.get_post(post_id)

    await notify_user_if_flagged(message, post)
    await message.answer("✅ Your post (photo) was received and is pending admin review.")
    await send_to_admin(post_id)


@dp.message(F.video)
async def handle_video(message: Message):
    file_id = message.video.file_id
    caption = message.caption or ""

    post_id = await db.add_post(
        user_id=message.from_user.id,
        content_type="video",
        text=caption,
        file_id=file_id
    )
    await db.log_action(post_id, "submitted")
    post = await db.get_post(post_id)

    await notify_user_if_flagged(message, post)
    await message.answer("✅ Your post (video) was received and is pending admin review.")
    await send_to_admin(post_id)


@dp.message(F.text)
async def handle_text(message: Message):
    text = message.text
    content_type = "link" if text.strip().lower().startswith("http") else "text"

    post_id = await db.add_post(
        user_id=message.from_user.id,
        content_type=content_type,
        text=text,
        file_id=None
    )
    await db.log_action(post_id, "submitted")
    post = await db.get_post(post_id)

    await notify_user_if_flagged(message, post)
    await message.answer("✅ Your post was received and is pending admin review.")
    await send_to_admin(post_id)


async def notify_user_if_flagged(message: Message, post):
    """
    Privately inform the submitting user if their post is flagged.
    The post still goes to the admin regardless.
    """
    sim_flag    = post["similarity_flag"]
    sim_post_id = post["similar_post_id"]

    if not sim_flag:
        return

    if sim_flag == "DUPLICATE":
        await message.answer(
            f"ℹ️ Your post is identical to a previously published post (#{sim_post_id}).\n"
            f"It has still been sent to the admin for review."
        )
    elif sim_flag.startswith("SIMILAR:"):
        percent = sim_flag.split(":")[1]
        await message.answer(
            f"ℹ️ Your post is {percent}% similar to a previously published post (#{sim_post_id}).\n"
            f"It has still been sent to the admin for review."
        )


# ────────────────────────────────────────────
# SEND TO ADMIN — TWO MESSAGES
# ────────────────────────────────────────────

async def send_to_admin(post_id: int):
    """
    Per submission, each admin gets exactly two messages:
      1. The raw content exactly as the user sent it (no extra text)
      2. A compact info card with warnings and action buttons
    """
    post = await db.get_post(post_id)
    if not post:
        return

    content_type = post["content_type"]
    text         = post["text"] or ""
    file_id      = post["file_id"]
    sim_post_id  = post["similar_post_id"]

    # Resolve channel link for the original post (if it was published)
    channel_link = None
    if sim_post_id:
        original = await db.get_post(sim_post_id)
        if original and original["channel_msg_id"]:
            channel_link = make_channel_link(original["channel_msg_id"])

    info_text = build_admin_info_message(post, channel_link)
    keyboard  = build_admin_keyboard(post_id)

    for admin_id in ADMIN_IDS:
        try:
            # ── Message 1: raw content ───────────────────────────
            if content_type == "photo" and file_id:
                await bot.send_photo(
                    chat_id=admin_id,
                    photo=file_id,
                    caption=text or None
                )
            elif content_type == "video" and file_id:
                await bot.send_video(
                    chat_id=admin_id,
                    video=file_id,
                    caption=text or None
                )
            else:
                await bot.send_message(
                    chat_id=admin_id,
                    text=text
                )

            # ── Message 2: info card + buttons ───────────────────
            await bot.send_message(
                chat_id=admin_id,
                text=info_text,
                reply_markup=keyboard
            )

        except Exception as e:
            logging.warning(f"Could not message admin {admin_id}: {e}")


# ────────────────────────────────────────────
# ADMIN: APPROVE
# ────────────────────────────────────────────

@dp.callback_query(F.data.startswith("approve_"))
async def handle_approve(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔ You are not authorized.", show_alert=True)
        return

    post_id = int(callback.data.split("_")[1])
    post    = await db.get_post(post_id)

    if post is None:
        await callback.answer("Post not found.", show_alert=True)
        return

    if post["status"] != "pending":
        await callback.answer(f"This post is already '{post['status']}'.", show_alert=True)
        return

    await db.update_status(post_id, "approved")
    await db.log_action(post_id, "approved")

    channel_msg_id = await publish_to_telegram_channel(
        post["content_type"], post["text"], post["file_id"]
    )
    if channel_msg_id:
        await db.save_channel_msg_id(post_id, channel_msg_id)

    await db.update_status(post_id, "published")
    await db.log_action(post_id, "published")

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.reply(f"✅ Post #{post_id} approved and published.")
    await callback.answer("Published!")

    try:
        await bot.send_message(
            post["user_id"],
            f"🎉 Your post #{post_id} was approved and published!"
        )
    except Exception as e:
        logging.warning(f"Could not notify user {post['user_id']}: {e}")


# ────────────────────────────────────────────
# ADMIN: REJECT
# ────────────────────────────────────────────

@dp.callback_query(F.data.startswith("reject_"))
async def handle_reject(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔ You are not authorized.", show_alert=True)
        return

    post_id = int(callback.data.split("_")[1])
    post    = await db.get_post(post_id)

    if post is None:
        await callback.answer("Post not found.", show_alert=True)
        return

    if post["status"] != "pending":
        await callback.answer(f"This post is already '{post['status']}'.", show_alert=True)
        return

    await db.update_status(post_id, "rejected")
    await db.log_action(post_id, "rejected")

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.reply(f"❌ Post #{post_id} rejected.")
    await callback.answer("Rejected.")

    try:
        await bot.send_message(
            post["user_id"],
            f"😔 Your post #{post_id} was reviewed but not selected for publication."
        )
    except Exception as e:
        logging.warning(f"Could not notify user {post['user_id']}: {e}")


# ────────────────────────────────────────────
# PUBLISHING
# ────────────────────────────────────────────

async def publish_to_telegram_channel(content_type: str, text: str, file_id: str) -> int | None:
    """Publish to the Telegram channel. Returns the message ID."""
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
# BALE CHANNEL LISTENER
# ────────────────────────────────────────────

async def handle_bale_channel_post(post: dict, session: aiohttp.ClientSession):
    text     = post.get("text") or post.get("caption") or ""
    new_text = text.replace(BALE_CHANNEL_USERNAME, TELEGRAM_CHANNEL_USERNAME)

    if "photo" in post:
        file_id = post["photo"][-1]["file_id"]
        async with session.get(f"{BALE_API_URL}/getFile", params={"file_id": file_id}) as resp:
            file_data = await resp.json()
        if not file_data.get("ok"):
            logging.error(f"Bale getFile failed: {file_data}")
            return
        file_path = file_data["result"]["file_path"]
        file_url  = f"https://tapi.bale.ai/file/bot{BALE_BOT_TOKEN}/{file_path}"
        async with session.get(file_url) as resp:
            photo_bytes = await resp.read()
        photo_file = BufferedInputFile(photo_bytes, filename="photo.jpg")
        await bot.send_photo(chat_id=CHANNEL_ID, photo=photo_file, caption=new_text)

    elif "video" in post:
        file_id = post["video"]["file_id"]
        async with session.get(f"{BALE_API_URL}/getFile", params={"file_id": file_id}) as resp:
            file_data = await resp.json()
        if not file_data.get("ok"):
            logging.error(f"Bale getFile (video) failed: {file_data}")
            return
        file_path = file_data["result"]["file_path"]
        file_url  = f"https://tapi.bale.ai/file/bot{BALE_BOT_TOKEN}/{file_path}"
        async with session.get(file_url) as resp:
            video_bytes = await resp.read()
        video_file = BufferedInputFile(video_bytes, filename="video.mp4")
        await bot.send_video(chat_id=CHANNEL_ID, video=video_file, caption=new_text)

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
                    f"{BALE_API_URL}/getUpdates",
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=40)
                ) as resp:
                    data = await resp.json()

                if not data.get("ok"):
                    logging.error(f"Bale getUpdates error: {data}")
                    await asyncio.sleep(5)
                    continue

                for update in data.get("result", []):
                    offset  = update["update_id"] + 1
                    message = update.get("message")
                    if message and message.get("chat", {}).get("type") == "channel":
                        try:
                            await handle_bale_channel_post(message, session)
                        except Exception as e:
                            logging.error(f"Error forwarding Bale post: {e}")

            except Exception as e:
                logging.error(f"Bale listener error: {e}")
                await asyncio.sleep(5)


# ────────────────────────────────────────────
# MAIN
# ────────────────────────────────────────────

async def main():
    await db.init_db()
    logging.info("Database ready. Bot is starting…")
    await asyncio.gather(
        dp.start_polling(bot),
        bale_listener()
    )


if __name__ == "__main__":
    asyncio.run(main())