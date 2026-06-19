import asyncio
import os
import aiohttp
from dotenv import load_dotenv

load_dotenv()

BALE_BOT_TOKEN = os.getenv("BALE_BOT_TOKEN")
BALE_CHANNEL_ID = os.getenv("BALE_CHANNEL_ID")
BALE_API_URL = f"https://tapi.bale.ai/bot{BALE_BOT_TOKEN}"


async def main():
    async with aiohttp.ClientSession() as session:

        # 1. Get bot's own info
        async with session.get(f"{BALE_API_URL}/getMe") as resp:
            print("getMe:", await resp.json())

        # 2. Get chat info for the channel
        async with session.get(f"{BALE_API_URL}/getChat", params={"chat_id": BALE_CHANNEL_ID}) as resp:
            print("\ngetChat:", resp.status, await resp.text())

        # 3. Get list of admins in the channel
        async with session.get(f"{BALE_API_URL}/getChatAdministrators", params={"chat_id": BALE_CHANNEL_ID}) as resp:
            print("\ngetChatAdministrators:", resp.status, await resp.text())


if __name__ == "__main__":
    asyncio.run(main())