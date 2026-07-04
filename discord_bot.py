import discord
import requests
import aiohttp
import asyncio
from datetime import datetime

# CONFIGURATION - PASTE YOUR TOKENS HERE
DISCORD_BOT_TOKEN = "ODU0NjgwOTE2MTQ4MzU1MTMy.Gx-HHD.52w4B-SvYBlAXDEkTgl3jCUGYO6KfjiteCHMxU"
TELEGRAM_BOT_TOKEN = "8897870104:AAFc1JvCIam8lWbUhyJsyIZPe8wUwc5ObJw"
TELEGRAM_CHAT_ID = "8591595853"

client = discord.Client()

async def send_telegram_notification(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }

    for attempt in range(3):
        try:
            response = requests.post(url, json=payload, timeout=15)

            print(f"Telegram Status: {response.status_code}")
            print(f"Telegram Response: {response.text}")

            if response.status_code == 200:
                return True

        except Exception as e:
            print(f"[Network Warning] Attempt {attempt + 1}/3 failed: {e}")

        await asyncio.sleep(2)

    return False


@client.event
async def on_ready():
    print(f"Logged into Discord successfully as: {client.user.name}")
    print(f"Monitoring your servers for incoming join events...")


@client.event
async def on_member_join(member):
    join_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    server_name = member.guild.name
    username = member.name

    alert_text = (
        f"🚨 **New Discord Join Detected!**\n\n"
        f"🏠 **Server Name:** {server_name}\n"
        f"👤 **User:** {username}\n"
        f"⏰ **Time Joined:** {join_time} (Local Time)"
    )

    print(f"[Event Caught] User {username} joined {server_name}. Sending alert...")
    await send_telegram_notification(alert_text)


# 1-LINE MODIFICATION: Lightweight port listener to satisfy Render's web traffic checks
async def dummy_web_server():
    server = await asyncio.start_server(lambda r, w: w.close(), '0.0.0.0', 10000)
    async with server: 
        await server.serve_forever()

async def main():
    # Run the web server and the Discord client side-by-side concurrently
    await asyncio.gather(dummy_web_server(), client.start(DISCORD_BOT_TOKEN))

if __name__ == "__main__":
    asyncio.run(main())
