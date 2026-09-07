#!/usr/bin/env python3
"""
Discord Join Monitor using discord.py-self
Reads tokens from tokens.txt file
Receives GUILD_MEMBER_ADD events natively
"""

import asyncio
import logging
import os
import threading
import random
from datetime import datetime
from flask import Flask, jsonify
import requests

# Try importing discord.py-self
try:
    import discord
    from discord import Client
    logger = logging.getLogger(__name__)
    logger.info("✅ discord.py-self loaded successfully")
    print(f"✅ discord.py-self version: {discord.__version__}")
except ImportError as e:
    print(f"❌ Error importing discord.py-self: {e}")
    print("Please install: pip install discord.py-self")
    exit(1)

# ===== LOGGING SETUP =====
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# ===== CREDENTIALS =====
TELEGRAM_BOT_TOKEN = "8897870104:AAFc1JvCIam8lWbUhyJsyIZPe8wUwc5ObJw"
TELEGRAM_CHAT_ID = "8591595853"

# ===== FLASK KEEPALIVE =====
app = Flask(__name__)

@app.route('/')
def home():
    return jsonify({"status": "active", "framework": "discord.py-self"})

@app.route('/health')
def health():
    return jsonify({"status": "healthy"}), 200

def run_flask():
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    app.run(host='0.0.0.0', port=int(os.getenv("PORT", 10000)))

# ===== TELEGRAM SERVICE =====
class TelegramNotifier:
    def send_alert(self, text: str):
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
        try:
            response = requests.post(url, json=payload, timeout=10)
            if response.status_code == 200:
                logger.info("📨 Alert sent to Telegram")
            else:
                logger.error(f"Telegram error: {response.status_code}")
        except Exception as e:
            logger.error(f"Telegram error: {e}")

notifier = TelegramNotifier()

# ===== DISCORD CLIENT =====
class JoinMonitor(discord.Client):
    def __init__(self, account_name: str):
        # Use discord.Client directly without Intents (discord.py-self handles this differently)
        super().__init__()
        self.account_name = account_name
        self.ready_sent = False

    async def on_ready(self):
        logger.info(f"✅ {self.account_name} Connected as: {self.user.name} (ID: {self.user.id})")
        
        guild_count = len(self.guilds)
        guild_list = "\n".join([f"  🏠 {g.name}" for g in list(self.guilds)[:10]])
        if guild_count > 10:
            guild_list += f"\n  ... and {guild_count - 10} more"

        alert = (
            f"✅ <b>Monitor Online</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>Account:</b> {self.user.name}\n"
            f"🆔 <b>ID:</b> <code>{self.user.id}</code>\n"
            f"📊 <b>Servers:</b> {guild_count}\n"
            f"\n<b>Monitoring:</b>\n{guild_list}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⏰ <b>Time:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"🔄 <b>Status:</b> Monitoring for new joins..."
        )
        notifier.send_alert(alert)
        self.ready_sent = True

    async def on_member_join(self, member):
        try:
            server = member.guild.name
            username = member.name
            user_id = member.id
            avatar = member.display_avatar.url if member.avatar else None

            alert = (
                f"🆕 <b>New Discord Join!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🏠 <b>Server:</b> {server}\n"
                f"👤 <b>User:</b> {username}\n"
                f"🆔 <b>User ID:</b> <code>{user_id}</code>\n"
            )
            if avatar:
                alert += f"🖼️ <b>Avatar:</b> <a href='{avatar}'>View</a>\n"
            alert += f"━━━━━━━━━━━━━━━━━━━━\n"
            alert += f"⏰ <b>Time:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

            logger.info(f"🚨 {self.account_name}: {username} joined {server}")
            notifier.send_alert(alert)
        except Exception as e:
            logger.error(f"on_member_join error: {e}")

    async def on_guild_join(self, guild):
        alert = (
            f"✅ <b>Added to new server!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🏠 <b>Server:</b> {guild.name}\n"
            f"🆔 <b>ID:</b> <code>{guild.id}</code>\n"
            f"👤 <b>Members:</b> {guild.member_count}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⏰ <b>Time:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        logger.info(f"✅ {self.account_name} added to: {guild.name}")
        notifier.send_alert(alert)

    async def on_error(self, event, *args, **kwargs):
        logger.error(f"Error in {event}: {args}")

# ===== ACCOUNT MANAGER =====
class AccountManager:
    def __init__(self):
        self.accounts = []
        self.clients = []
        self._running = True

    def load_accounts(self):
        if os.path.exists("tokens.txt"):
            with open("tokens.txt", "r") as f:
                for line in f:
                    line = line.strip()
                    if line and ":" in line:
                        name, token = line.split(":", 1)
                        self.accounts.append({"name": name.strip(), "token": token.strip()})
                        logger.info(f"📁 Loaded: {name.strip()}")
        else:
            logger.warning("tokens.txt not found! Creating sample...")
            with open("tokens.txt", "w") as f:
                f.write("# Add your tokens here\n# Format: Name:TOKEN\nAccount1:YOUR_TOKEN_HERE\n")
        return self.accounts

    async def start_all(self):
        self.accounts = self.load_accounts()
        if not self.accounts:
            logger.error("No accounts loaded")
            notifier.send_alert("❌ No Discord tokens found. Create tokens.txt")
            return

        logger.info(f"🚀 Starting {len(self.accounts)} monitors")
        
        # Send startup notification
        account_list = "\n".join([f"  👤 {a['name']}" for a in self.accounts])
        notifier.send_alert(
            f"🚀 <b>Discord Join Monitor Starting</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 <b>Accounts:</b> {len(self.accounts)}\n"
            f"{account_list}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⏰ <b>Started:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )

        # Start each client
        for idx, acc in enumerate(self.accounts):
            if idx > 0:
                stagger = random.uniform(2, 5)
                logger.info(f"⏳ Waiting {stagger:.1f}s before starting {acc['name']}")
                await asyncio.sleep(stagger)
            
            client = JoinMonitor(acc["name"])
            self.clients.append(client)
            asyncio.create_task(self._run_client(client, acc["token"], acc["name"]))

        # Keep running
        while self._running:
            await asyncio.sleep(60)
            connected = sum(1 for c in self.clients if c.is_ready())
            total = len(self.clients)
            logger.info(f"📊 Status: {connected}/{total} connected")

    async def _run_client(self, client, token, name):
        try:
            await client.start(token)
        except discord.LoginFailure:
            logger.error(f"❌ {name}: Invalid token")
            notifier.send_alert(f"❌ <b>{name}</b>: Invalid Discord token!")
        except Exception as e:
            logger.error(f"❌ {name}: {e}")
            await asyncio.sleep(5)
            asyncio.create_task(self._run_client(client, token, name))

    async def cleanup(self):
        self._running = False
        for c in self.clients:
            try:
                await c.close()
            except:
                pass

# ===== MAIN =====
async def main():
    print("=" * 60)
    print("■ Discord Join Monitor - discord.py-self")
    print("=" * 60)
    
    # Start Flask
    threading.Thread(target=run_flask, daemon=True).start()
    
    # Start manager
    manager = AccountManager()
    try:
        await manager.start_all()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        await manager.cleanup()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nClosed.")
