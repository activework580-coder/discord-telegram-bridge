#!/usr/bin/env python3
"""
Server Join Monitor – Using discord.py-self
Reliably detects GUILD_MEMBER_ADD with user tokens
"""

import discord
import asyncio
import aiohttp
import logging
import threading
from datetime import datetime
from flask import Flask, jsonify

# ===== CONFIGURATION =====
DISCORD_TOKEN = "MTI1MTkyODY3NTE2MzgzNjU2MA.GqpILO.tDRKP_6PIbtOxgr0DMglU_8o3IN2jDUi29gK1I"          # Replace with your user token
TELEGRAM_BOT_TOKEN = "8897870104:AAFc1JvCIam8lWbUhyJsyIZPe8wUwc5ObJw"
TELEGRAM_CHAT_ID = "8591595853"

# ===== LOGGING =====
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ===== FLASK WEB SERVER (for Render health checks) =====
app = Flask(__name__)

@app.route('/')
def home():
    return jsonify({"status": "running", "mode": "discord.py-self"})

@app.route('/health')
def health():
    return jsonify({"status": "healthy"})

def run_flask():
    app.run(host='0.0.0.0', port=int(os.getenv("PORT", 10000)))

# ===== TELEGRAM SENDER =====
async def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    logger.info("📨 Telegram notification sent")
                else:
                    logger.warning(f"Telegram error: {resp.status}")
        except Exception as e:
            logger.error(f"Telegram send failed: {e}")

# ===== DISCORD CLIENT =====
class JoinNotifier(discord.Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ready = False

    async def on_ready(self):
        self.ready = True
        logger.info(f"✅ Logged in as {self.user} (ID: {self.user.id})")
        logger.info(f"📊 Monitoring {len(self.guilds)} servers")
        await send_telegram(f"✅ Bot online, monitoring {len(self.guilds)} servers")

    async def on_member_join(self, member):
        """Called when a new member joins a server (user token works with discord.py-self)"""
        server_name = member.guild.name
        username = member.name
        user_id = member.id
        join_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        alert = (
            f"🚨 **New Discord Join!**\n\n"
            f"🏠 **Server:** {server_name}\n"
            f"👤 **User:** {username}\n"
            f"🆔 **ID:** {user_id}\n"
            f"🕐 **Time:** {join_time}"
        )
        logger.info(f"🎉 {username} joined {server_name}")
        await send_telegram(alert)

# ===== MAIN =====
async def main():
    # Start Flask in background thread
    threading.Thread(target=run_flask, daemon=True).start()

    # Create and run the Discord client
    client = JoinNotifier()

    try:
        await client.start(DISCORD_TOKEN)
    except discord.LoginFailure:
        logger.error("❌ Invalid Discord token")
        await send_telegram("❌ Invalid Discord token")
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        await client.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped")
