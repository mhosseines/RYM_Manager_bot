import asyncio
import os
import aiohttp
from dotenv import load_dotenv

load_dotenv()

BALE_BOT_TOKEN = os.getenv("BALE_BOT_TOKEN")
BALE_API_URL = f"https://tapi.bale.ai/bot{BALE_BOT_TOKEN}"


async def main():
    offset = 0
    print("Listening for updates... Now go send a message in your Bale channel.")
    print("Press Ctrl+C to stop.\n")

    async with aiohttp.ClientSession() as session:
        while True:
            params = {"offset": offset, "timeout": 30}
            async with session.get(
                f"{BALE_API_URL}/getUpdates",
                params=params,
                timeout=aiohttp.ClientTimeout(total=40)
            ) as resp:
                data = await resp.json()

            if not data.get("ok"):
                print("Error:", data)
                await asyncio.sleep(2)
                continue

            results = data.get("result", [])
            if results:
                for update in results:
                    print("NEW UPDATE:", update)
                    offset = update["update_id"] + 1
            else:
                print("No updates this round, polling again...")


if __name__ == "__main__":
    asyncio.run(main())