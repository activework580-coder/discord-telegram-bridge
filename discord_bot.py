#!/usr/bin/env python3
"""
Clean Tier 3 Server Join Monitor
Monitors server joins and sends Telegram notifications
"""

import asyncio
import json
import os
import random
import time
import base64
import hashlib
import logging
import threading
from datetime import datetime
from flask import Flask, jsonify
from curl_cffi import requests as curl_requests
from curl_cffi.requests import WebSocket

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = "8897870104:AAFc1JvCIam8lWbUhyJsyIZPe8wUwc5ObJw"
TELEGRAM_CHAT_ID = "8591595853"
MAX_ACCOUNTS = 999
HEARTBEAT_JITTER = 0.15
PROXY_URL = os.getenv("PROXY_URL", None)

app = Flask(__name__)

@app.route('/')
def home():
    return jsonify({"status": "running", "mode": "Server Join Monitor"})

@app.route('/health')
def health():
    return jsonify({"status": "healthy"})

def run_flask():
    app.run(host='0.0.0.0', port=int(os.getenv("PORT", 10000)))

class TelegramService:
    def __init__(self):
        self._session = None
        self._last_sent = 0
        self._min_interval = 0.5

    async def _get_session(self):
        if self._session is None:
            self._session = curl_requests.AsyncSession(impersonate="chrome")
        return self._session

    async def send(self, text: str, account_label: str = None):
        if account_label:
            text = f"[{account_label}] {text}"
        now = time.time()
        if now - self._last_sent < self._min_interval:
            await asyncio.sleep(self._min_interval - (now - self._last_sent))
        self._last_sent = time.time()
        try:
            session = await self._get_session()
            await session.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
            )
            logger.info(f"📨 Telegram sent")
            return True
        except Exception as e:
            logger.error(f"Telegram error: {e}")
            return False

    async def close(self):
        if self._session:
            await self._session.close()

def generate_fingerprint(account_index: int):
    random.seed(account_index * 777 + 13)
    fingerprint = {
        "os": "Windows",
        "browser": "Chrome",
        "device": "",
        "system_locale": "en-US",
        "browser_user_agent": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "browser_version": "126.0.0.0",
        "os_version": "10.0.22621",
        "referrer": "",
        "referring_domain": "",
        "referrer_current": "",
        "referring_domain_current": "",
        "release_channel": "stable",
        "client_build_number": 287275,
        "client_event_source": None,
        "architecture": "x64",
        "launch_signature": base64.b64encode(random.randbytes(8)).decode('utf-8'),
        "has_client_mods": False
    }
    return fingerprint

class DiscordGateway:
    def __init__(self, token: str, label: str, account_index: int, telegram: TelegramService):
        self.token = token
        self.label = label
        self.account_index = account_index
        self.telegram = telegram
        self.ws = None
        self._session = None
        self._running = True
        self._seq = 0
        self._heartbeat_interval = 41.25
        self._connected = False
        self._heartbeat_task = None
        self._reconnect_attempt = 0
        self.is_connected = False
        self._guilds = {}
        self._invalid_token = False

    async def connect(self):
        if not self.token or len(self.token) < 20:
            logger.error(f"❌ {self.label}: Invalid token")
            await self.telegram.send(f"❌ {self.label} token is invalid", self.label)
            self._invalid_token = True
            self._running = False
            return

        fingerprint = generate_fingerprint(self.account_index)
        encoded_props = base64.b64encode(json.dumps(fingerprint).encode()).decode('utf-8')

        headers = {
            "User-Agent": fingerprint["browser_user_agent"],
            "Origin": "https://discord.com",
            "X-Super-Properties": encoded_props,
            "X-Discord-Device-Id": hashlib.sha256(f"{self.token}_{self.account_index}".encode()).hexdigest()[:32]
        }

        try:
            self._session = curl_requests.AsyncSession(
                impersonate="chrome",
                proxies={"https": PROXY_URL, "http": PROXY_URL} if PROXY_URL else None
            )
            self.ws = await self._session.ws_connect(
                url="wss://gateway.discord.gg/?v=9&encoding=json",
                headers=headers
            )
            logger.info(f"🔌 {self.label}: Connected")
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            await self._receive_loop()
        except Exception as e:
            logger.error(f"❌ {self.label}: Connection failed: {e}")
            if "401" in str(e) or "invalid" in str(e).lower():
                await self.telegram.send(f"❌ {self.label} token is invalid", self.label)
                self._invalid_token = True
                self._running = False
            else:
                await self._reconnect()

    async def _receive_loop(self):
        while self._running:
            try:
                raw = await self.ws.recv()
                if isinstance(raw, tuple):
                    message = raw[0].decode('utf-8', errors='ignore')
                else:
                    message = raw.decode('utf-8', errors='ignore')
                
                if not message:
                    continue

                data = json.loads(message)
                op = data.get('op')
                t = data.get('t')
                d = data.get('d', {})

                if op == 0:
                    self._seq = data.get('s', self._seq)
                    await self._handle_event(t, d)
                elif op == 1:
                    await self._send_heartbeat()
                elif op == 7:
                    logger.info(f"🔄 {self.label}: Server requested reconnect")
                    await self._reconnect()
                elif op == 9:
                    if d is False:
                        logger.error(f"❌ {self.label}: Invalid token")
                        await self.telegram.send(f"❌ {self.label} token is invalid", self.label)
                        self._invalid_token = True
                        self._running = False
                    else:
                        await self._send_identify()
                elif op == 10:
                    self._heartbeat_interval = d['heartbeat_interval'] / 1000.0
                    await self._send_identify()
                    self._connected = True
                    self.is_connected = True
                    logger.info(f"✅ {self.label}: Connected")
                elif op == 11:
                    pass

            except Exception as e:
                if "closed" in str(e).lower():
                    logger.warning(f"⚠️ {self.label}: Connection closed")
                    await self._reconnect()
                else:
                    logger.error(f"⚠️ {self.label}: Error: {e}")

    async def _handle_event(self, event_type: str, data: dict):
        if event_type == 'READY':
            guilds = data.get('guilds', [])
            self._guilds = {g['id']: g['name'] for g in guilds if 'name' in g}
            user = data.get('user', {})
            logger.info(f"✅ {self.label}: Connected as {user.get('username')} monitoring {len(self._guilds)} servers")
            await self.telegram.send(f"✅ {self.label} online, monitoring {len(self._guilds)} servers", self.label)
            
        elif event_type == 'GUILD_MEMBER_ADD':
            guild_id = data.get('guild_id')
            user = data.get('user', {})
            guild_name = self._guilds.get(guild_id, "Unknown Server")
            username = user.get('username', 'Unknown')
            
            alert = (
                f"🚨 New Discord Join!\n\n"
                f"🏠 Server: {guild_name}\n"
                f"👤 User: {username}\n"
                f"🕐 Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            logger.info(f"🎉 {self.label}: {username} joined {guild_name}")
            await self.telegram.send(alert, self.label)

    async def _send_identify(self):
        fingerprint = generate_fingerprint(self.account_index)
        payload = {
            "op": 2,
            "d": {
                "token": self.token,
                "capabilities": 8189,
                "properties": fingerprint,
                "compress": False,
                "large_threshold": 250,
                "guild_subscriptions": True,
                "presence": {"status": "online", "since": 0, "activities": [], "afk": False},
                "client_state": {
                    "guild_versions": {},
                    "highest_last_message_id": "0",
                    "read_state_version": 0,
                    "user_guild_settings_version": -1,
                    "user_settings_version": 0,
                    "private_channels_version": 0,
                    "api_code_version": 0
                }
            }
        }
        await self.ws.send(json.dumps(payload))

    async def _send_heartbeat(self):
        if random.random() < 0.05:
            return
        await self.ws.send(json.dumps({"op": 1, "d": self._seq}))

    async def _heartbeat_loop(self):
        while self._running:
            jitter = 1 + random.uniform(-HEARTBEAT_JITTER, HEARTBEAT_JITTER)
            await asyncio.sleep(self._heartbeat_interval * jitter)
            if self._connected:
                await self._send_heartbeat()

    async def _reconnect(self):
        if not self._running or self._invalid_token:
            return
        self._reconnect_attempt += 1
        wait = min(30, (2 ** min(self._reconnect_attempt, 4)) + random.uniform(0, 5))
        logger.info(f"🔄 {self.label}: Reconnect in {wait:.1f}s")
        await asyncio.sleep(wait)
        await self.connect()

    async def run(self):
        await self.connect()

    async def close(self):
        self._running = False
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        if self.ws:
            await self.ws.close()
        if self._session:
            await self._session.close()

class AccountManager:
    def __init__(self):
        self.accounts = []
        self.gateways = []
        self.telegram = TelegramService()

    def load_accounts(self):
        if os.path.exists("tokens.txt"):
            with open("tokens.txt", "r") as f:
                for idx, line in enumerate(f):
                    line = line.strip()
                    if line and ":" in line:
                        name, token = line.split(":", 1)
                        self.accounts.append({"name": name.strip(), "token": token.strip()})
        if not self.accounts:
            token = os.getenv("DISCORD_TOKEN", "")
            if token:
                self.accounts.append({"name": "Discord_1", "token": token})
        return self.accounts

    async def start_all(self):
        self.accounts = self.load_accounts()
        if not self.accounts:
            logger.error("❌ No accounts")
            return

        logger.info(f"🚀 Starting {len(self.accounts)} accounts")
        await self.telegram.send(f"🚀 Starting {len(self.accounts)} server join monitors", "System")

        for idx, acc in enumerate(self.accounts):
            if idx > 0:
                await asyncio.sleep(random.uniform(1, 3))
            
            gateway = DiscordGateway(
                token=acc["token"],
                label=acc["name"],
                account_index=idx,
                telegram=self.telegram
            )
            self.gateways.append(gateway)
            asyncio.create_task(gateway.run())

        while True:
            await asyncio.sleep(60)
            connected = sum(1 for g in self.gateways if g.is_connected)
            logger.info(f"📊 Connected: {connected}/{len(self.gateways)}")

    async def cleanup(self):
        for g in self.gateways:
            await g.close()
        await self.telegram.close()

async def main():
    print("=" * 50)
    print("🤖 Server Join Monitor - Tier 3")
    print("=" * 50)
    
    threading.Thread(target=run_flask, daemon=True).start()
    
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
        print("\nStopped")
