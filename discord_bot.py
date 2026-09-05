import discord
import requests
import aiohttp
import asyncio
from datetime import datetime
from flask import Flask
from threading import Thread

# CONFIGURATION - PASTE YOUR TOKENS HERE
DISCORD_BOT_TOKEN = "MTI1MTkyODY3NTE2MzgzNjU2MA.GbrP9J.njN2BWsfxMFfvd9W8_zosI1x9HHqfEVWoCJjKY"
TELEGRAM_BOT_TOKEN = "8897870104:AAFc1JvCIam8lWbUhyJsyIZPe8wUwc5ObJw"
TELEGRAM_CHAT_ID = "8591595853"

client = discord.Client()

# Flask Web Server setup to satisfy Render's port checks completely
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive and running 24/7!"

def run_flask():
    # Flask runs on port 10000 which Render scans automatically
    app.run(host='0.0.0.0', port=10000)
    
async def send_telegram_notification(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        #"parse_mode": None
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
        f"🚨 New Discord Join Detected!\n\n"
        f"🏠 Server Name:** {server_name}\n"
        f"👤 User: {username}\n"
        f"⏰ Time Joined: {join_time} (Local Time)"
    )

    print(f"[Event Caught] User {username} joined {server_name}. Sending alert...")
    await send_telegram_notification(alert_text)


if __name__ == "__main__":
    # Start the web server in a separate thread so it doesn't block Discord
    Thread(target=run_flask).start()
    
    # Run the Discord bot
    client.run(DISCORD_BOT_TOKEN)
