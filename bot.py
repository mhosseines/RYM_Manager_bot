import asyncio
import os
import logging

import aiohttp
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile

import database as db

# -------------------- SETUP --------------------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS").split(",")]
CHANNEL_ID = os.getenv("CHANNEL_ID")
BALE_BOT_TOKEN = os.getenv("BALE_BOT_TOKEN")
BALE_CHANNEL_ID = os.getenv("BALE_CHANNEL_ID")
BALE_CHANNEL_USERNAME = os.getenv("BALE_CHANNEL_USERNAME")
TELEGRAM_CHANNEL_USERNAME = os.getenv("TELEGRAM_CHANNEL_USERNAME")
BALE_API_URL = f"https://tapi.bale.ai/bot{BALE_BOT_TOKEN}"

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# -------------------- USER: RECEIVE SUBMISSIONS --------------------

@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "👋 Welcome! Send me a news post (text, photo with caption, or a link) "
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

    await message.answer("✅ Your post (with photo) was received and is pending admin review.")
    await send_to_admin(post_id)


@dp.message(F.text)
async def handle_text(message: Message):
    text = message.text

    # Treat anything starting with http as a "link" type, otherwise "text"
    content_type = "link" if text.strip().lower().startswith("http") else "text"

    post_id = await db.add_post(
        user_id=message.from_user.id,
        content_type=content_type,
        text=text,
        file_id=None
    )

    await message.answer("✅ Your post was received and is pending admin review.")
    await send_to_admin(post_id)


# -------------------- SEND POST TO ADMIN FOR REVIEW --------------------

async def send_to_admin(post_id: int):
    post = await db.get_post(post_id)
    # post columns: id, user_id, content_type, text, file_id, status
    _, user_id, content_type, text, file_id, status = post

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Approve", callback_data=f"approve_{post_id}"),
            InlineKeyboardButton(text="❌ Reject", callback_data=f"reject_{post_id}")
        ]
    ])

    caption_text = f"📝 New post #{post_id} from user {user_id}\n\n{text}"

    for admin_id in ADMIN_IDS:
        try:
            if content_type == "photo":
                await bot.send_photo(
                    chat_id=admin_id,
                    photo=file_id,
                    caption=caption_text,
                    reply_markup=keyboard
                )
            else:
                await bot.send_message(
                    chat_id=admin_id,
                    text=caption_text,
                    reply_markup=keyboard
                )
        except Exception as e:
            logging.warning(f"Could not send to admin {admin_id}: {e}")


# -------------------- ADMIN: APPROVE / REJECT --------------------

@dp.callback_query(F.data.startswith("approve_"))
async def handle_approve(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔ You are not authorized.", show_alert=True)
        return

    post_id = int(callback.data.split("_")[1])
    post = await db.get_post(post_id)

    if post is None:
        await callback.answer("Post not found.", show_alert=True)
        return

    _, user_id, content_type, text, file_id, status = post

    if status != "pending":
        await callback.answer(f"This post is already '{status}'.", show_alert=True)
        return

    # Mark as approved first
    await db.update_status(post_id, "approved")

    # Publish to Telegram channel
    await publish_to_telegram_channel(content_type, text, file_id)

    # # Publish to Bale channel
    # await publish_to_bale_channel(content_type, text, file_id)

    # Mark as published
    await db.update_status(post_id, "published")

    # Update the admin's message
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.reply(f"✅ Post #{post_id} approved and published to both channels.")
    await callback.answer("Published!")

    # Notify the original user
    try:
        await bot.send_message(user_id, f"🎉 Your post #{post_id} was approved and published!")
    except Exception as e:
        logging.warning(f"Could not notify user {user_id}: {e}")


@dp.callback_query(F.data.startswith("reject_"))
async def handle_reject(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("⛔ You are not authorized.", show_alert=True)
        return

    post_id = int(callback.data.split("_")[1])
    post = await db.get_post(post_id)

    if post is None:
        await callback.answer("Post not found.", show_alert=True)
        return

    _, user_id, content_type, text, file_id, status = post

    if status != "pending":
        await callback.answer(f"This post is already '{status}'.", show_alert=True)
        return

    await db.update_status(post_id, "rejected")

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.reply(f"❌ Post #{post_id} rejected.")
    await callback.answer("Rejected.")

    try:
        await bot.send_message(user_id, f"😔 Your post #{post_id} was rejected by the admin.")
    except Exception as e:
        logging.warning(f"Could not notify user {user_id}: {e}")


# -------------------- PUBLISHING FUNCTIONS --------------------

async def publish_to_telegram_channel(content_type: str, text: str, file_id: str):
    """Publish the approved post to the Telegram channel."""
    try:
        if content_type == "photo":
            await bot.send_photo(chat_id=CHANNEL_ID, photo=file_id, caption=text)
        else:
            await bot.send_message(chat_id=CHANNEL_ID, text=text)
    except Exception as e:
        logging.error(f"Failed to publish to Telegram channel: {e}")


# async def publish_to_bale_channel(content_type: str, text: str, file_id: str):
#     """
#     Publish the approved post to the Bale channel.
#     Bale's Bot API is similar to Telegram's, base URL: https://tapi.bale.ai/bot<TOKEN>/
#     NOTE: file_id from Telegram CANNOT be reused on Bale (different servers).
#     So for photos, we download from Telegram and re-upload to Bale.
#     """
#     base_url = f"https://tapi.bale.ai/bot{BALE_BOT_TOKEN}"

#     try:
#         async with aiohttp.ClientSession() as session:
#             if content_type == "photo":
#                 # Download the photo bytes from Telegram
#                 file = await bot.get_file(file_id)
#                 file_bytes_io = await bot.download_file(file.file_path)
#                 photo_bytes = file_bytes_io.read()

#                 form = aiohttp.FormData()
#                 form.add_field("chat_id", BALE_CHANNEL_ID)
#                 form.add_field("caption", text)
#                 form.add_field("photo", photo_bytes, filename="photo.jpg", content_type="image/jpeg")

#                 async with session.post(f"{base_url}/sendPhoto", data=form) as resp:
#                     if resp.status != 200:
#                         body = await resp.text()
#                         logging.error(f"Bale sendPhoto failed: {resp.status} {body}")
#             else:
#                 payload = {"chat_id": BALE_CHANNEL_ID, "text": text}
#                 async with session.post(f"{base_url}/sendMessage", json=payload) as resp:
#                     if resp.status != 200:
#                         body = await resp.text()
#                         logging.error(f"Bale sendMessage failed: {resp.status} {body}")
#     except Exception as e:
#         logging.error(f"Failed to publish to Bale channel: {e}")


# -------------------- BALE CHANNEL LISTENER --------------------

async def handle_bale_channel_post(post: dict, session: aiohttp.ClientSession):
    """Process a new post from the Bale channel and forward it to Telegram."""
    text = post.get("text") or post.get("caption") or ""
    new_text = text.replace(BALE_CHANNEL_USERNAME, TELEGRAM_CHANNEL_USERNAME)

    if "photo" in post:
        # Get largest photo
        file_id = post["photo"][-1]["file_id"]

        # Ask Bale for the file path
        async with session.get(f"{BALE_API_URL}/getFile", params={"file_id": file_id}) as resp:
            file_data = await resp.json()

        if not file_data.get("ok"):
            logging.error(f"Bale getFile failed: {file_data}")
            return

        file_path = file_data["result"]["file_path"]
        file_url = f"https://tapi.bale.ai/file/bot{BALE_BOT_TOKEN}/{file_path}"

        async with session.get(file_url) as resp:
            photo_bytes = await resp.read()

        photo_file = BufferedInputFile(photo_bytes, filename="photo.jpg")
        await bot.send_photo(chat_id=CHANNEL_ID, photo=photo_file, caption=new_text)
    else:
        if new_text.strip():
            await bot.send_message(chat_id=CHANNEL_ID, text=new_text)


async def bale_listener():
    """Continuously poll Bale for new channel posts and forward them to Telegram."""
    offset = 0
    async with aiohttp.ClientSession() as session:
        logging.info("Bale listener started, polling for updates...")
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
                    offset = update["update_id"] + 1
                    message = update.get("message")
                    if message and message.get("chat", {}).get("type") == "channel":
                        try:
                            await handle_bale_channel_post(message, session)
                        except Exception as e:
                            logging.error(f"Error forwarding Bale post: {e}")

            except Exception as e:
                logging.error(f"Bale listener error: {e}")
                await asyncio.sleep(5)
# -------------------- MAIN --------------------

async def main():
    await db.init_db()
    logging.info("Database initialized. Starting bot and Bale listener...")
    await asyncio.gather(
        dp.start_polling(bot),
        bale_listener()
    )

if __name__ == "__main__":
    asyncio.run(main())
