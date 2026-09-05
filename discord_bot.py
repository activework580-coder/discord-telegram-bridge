#!/usr/bin/env python3
"""
Tier 3 Server Join Monitor
Uses the exact same gateway logic as discord.py for user tokens
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
from typing import Optional, Dict, Any, Set, List
from flask import Flask, jsonify
from curl_cffi import requests as curl_requests
from curl_cffi.requests import WebSocket

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = "8897870104:AAFc1JvCIam8lWbUhyJsyIZPe8wUwc5ObJw"
TELEGRAM_CHAT_ID = "8591595853"
MAX_ACCOUNTS = 999
HEARTBEAT_JITTER = 0.15
READY_TIMEOUT = 15

PROXY_URL = os.getenv("PROXY_URL", None)

app = Flask(__name__)
stats = {"total_accounts": 0, "connected_accounts": 0}

@app.route('/')
def home():
    return jsonify({
        "status": "running",
        "mode": "Tier 3 - Server Join Monitor",
        "proxy": PROXY_URL,
        "accounts": stats
    })

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

    async def send(self, text: str, account_label: str = None, priority: bool = False) -> bool:
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
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True
                }
            )
            logger.info(f"📨 Telegram sent: {text[:50]}...")
            return True
        except Exception as e:
            logger.error(f"⚠️ Telegram send error: {e}")
            return False

    async def close(self):
        if self._session:
            await self._session.close()

def generate_fingerprint(account_index: int) -> Dict[str, Any]:
    random.seed(account_index * 777 + 13)
    os_versions = ["10.0.19045", "10.0.22621", "10.0.22000", "10.0.20348"]
    build_numbers = [287275, 288216, 289123, 290456, 291234, 292345]
    browser_versions = ["126.0.0.0", "127.0.0.0", "128.0.0.0", "129.0.0.0"]
    locales = ["en-US", "en-GB", "en-AU", "en-CA"]
    timezones = ["America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles"]

    return {
        "os": "Windows",
        "browser": "Chrome",
        "device": "",
        "system_locale": locales[account_index % len(locales)],
        "timezone": timezones[account_index % len(timezones)],
        "browser_user_agent": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{browser_versions[account_index % len(browser_versions)]} Safari/537.36",
        "browser_version": browser_versions[account_index % len(browser_versions)],
        "os_version": os_versions[account_index % len(os_versions)],
        "referrer": "",
        "referring_domain": "",
        "referrer_current": "",
        "referring_domain_current": "",
        "release_channel": "stable",
        "client_build_number": build_numbers[account_index % len(build_numbers)],
        "client_event_source": None,
        "architecture": "x64" if account_index % 2 == 0 else "arm64",
        "launch_signature": base64.b64encode(random.randbytes(8)).decode('utf-8'),
        "has_client_mods": False
    }

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
        self._device_id = self._generate_device_id()
        self._invalid_token = False
        self._last_heartbeat_ack = time.time()
        self._ready_received = False
        self._guilds = {}
        self._user_id = None

    def _generate_device_id(self) -> str:
        seed = f"{self.token}_{self.account_index}_{time.time() // 86400}"
        return hashlib.sha256(seed.encode()).hexdigest()[:32]

    def _validate_token(self) -> bool:
        return bool(self.token and len(self.token) >= 20 and self.token.count('.') == 2)

    async def _on_member_join(self, guild_id: str, user: Dict[str, Any]):
        join_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        guild_name = self._guilds.get(guild_id, {}).get("name", "Unknown Server")
        username = user.get("username", "Unknown")
        user_id = user.get("id", "Unknown")
        alert_text = (
            f"🚨 New Discord Join Detected!\n\n"
            f"🏠 Server: {guild_name}\n"
            f"👤 User: {username}\n"
            f"🆔 User ID: {user_id}\n"
            f"⏰ Time: {join_time}"
        )
        logger.info(f"🔥🔥🔥 EVENT CAUGHT: User {username} joined {guild_name}")
        await self.telegram.send(alert_text, self.label)

    async def connect(self):
        if not self._validate_token():
            logger.error(f"❌ {self.label}: Invalid token format - skipping")
            await self.telegram.send(f"❌ {self.label} token is invalid", self.label)
            self._invalid_token = True
            self._running = False
            self.is_connected = False
            return

        fingerprint = generate_fingerprint(self.account_index)
        encoded_props = base64.b64encode(json.dumps(fingerprint).encode()).decode('utf-8')

        headers = {
            "User-Agent": fingerprint["browser_user_agent"],
            "Origin": "https://discord.com",
            "X-Super-Properties": encoded_props,
            "X-Discord-Timezone": fingerprint.get("timezone", "America/New_York"),
            "X-Discord-Locale": fingerprint.get("system_locale", "en-US"),
            "X-Discord-Device-Id": self._device_id,
            "Accept-Encoding": "gzip, deflate, br",
            "Accept-Language": "en-US,en;q=0.9"
        }

        ws_url = "wss://gateway.discord.gg/?v=9&encoding=json"

        try:
            self._session = curl_requests.AsyncSession(
                impersonate="chrome",
                proxies={"https": PROXY_URL, "http": PROXY_URL} if PROXY_URL else None
            )

            self.ws = await asyncio.wait_for(
                self._session.ws_connect(url=ws_url, headers=headers),
                timeout=30
            )

            logger.info(f"🔌 {self.label}: Connected (Chrome TLS spoofed)")
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            await self._receive_loop()

        except asyncio.TimeoutError:
            logger.error(f"❌ {self.label}: Connection timeout")
            await self.telegram.send(f"❌ {self.label} token is invalid (timeout)", self.label)
            self._invalid_token = True
            self._running = False
            self.is_connected = False
        except Exception as e:
            error_msg = str(e).lower()
            if "401" in error_msg or "invalid" in error_msg or "authentication" in error_msg:
                logger.error(f"❌ {self.label}: Invalid token (authentication failed)")
                await self.telegram.send(f"❌ {self.label} token is invalid", self.label)
                self._invalid_token = True
                self._running = False
                self.is_connected = False
            else:
                logger.error(f"❌ {self.label}: Connection failed: {e}")
                await self._reconnect()

    async def _ready_timeout(self):
        await asyncio.sleep(READY_TIMEOUT)
        if not self._ready_received and self._running:
            logger.error(f"❌ {self.label}: READY timeout after {READY_TIMEOUT}s – invalid token")
            await self.telegram.send(f"❌ {self.label} token is invalid (READY timeout)", self.label)
            self._invalid_token = True
            self._running = False
            self.is_connected = False
            if self.ws:
                await self.ws.close()

    async def _receive_loop(self):
        ready_timer = asyncio.create_task(self._ready_timeout())

        while self._running:
            try:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=35)

                if isinstance(raw, tuple):
                    message_data, opcode = raw
                    if opcode == 0x2:
                        message = message_data.decode('utf-8', errors='ignore')
                    else:
                        message = message_data if isinstance(message_data, str) else message_data.decode('utf-8', errors='ignore')
                else:
                    message = raw if isinstance(raw, str) else raw.decode('utf-8', errors='ignore')

                if not message:
                    continue

                if not message.startswith('{') and not message.startswith('['):
                    continue

                data = json.loads(message)
                op = data.get('op')

                if op == 0:
                    self._seq = data.get('s', self._seq)
                    await self._handle_dispatch(data)
                    if data.get('t') == 'READY':
                        self._ready_received = True
                        ready_timer.cancel()
                elif op == 1:
                    await self._send_heartbeat()
                elif op == 7:
                    logger.info(f"🔄 {self.label}: Server requested reconnect")
                    ready_timer.cancel()
                    await self._reconnect()
                    return
                elif op == 9:
                    if data.get('d') is False:
                        logger.error(f"❌ {self.label}: Authentication failed (4004) - invalid token")
                        await self.telegram.send(f"❌ {self.label} token is invalid", self.label)
                        self._invalid_token = True
                        self._running = False
                        self.is_connected = False
                        if self.ws:
                            await self.ws.close()
                        ready_timer.cancel()
                        return
                    else:
                        logger.warning(f"⚠️ {self.label}: Invalid session, re-identifying...")
                        await self._send_identify()
                        continue
                elif op == 10:
                    self._heartbeat_interval = data['d']['heartbeat_interval'] / 1000.0
                    await self._send_identify()
                    self._connected = True
                    self.is_connected = True
                    self._reconnect_attempt = 0
                    logger.info(f"✅ {self.label}: Connected")
                elif op == 11:
                    self._last_heartbeat_ack = time.time()

            except asyncio.TimeoutError:
                if time.time() - self._last_heartbeat_ack > 60:
                    logger.warning(f"⚠️ {self.label}: No heartbeat ACK for 60s – reconnecting")
                    ready_timer.cancel()
                    await self._reconnect()
                    return
                else:
                    logger.debug(f"⚠️ {self.label}: Receive timeout (connection alive)")
                    continue
            except json.JSONDecodeError:
                logger.debug(f"⚠️ {self.label}: Bad JSON, skipping")
                continue
            except Exception as e:
                error_msg = str(e).lower()

                if "closed" in error_msg and not self._ready_received:
                    logger.error(f"❌ {self.label}: Connection closed before READY – invalid token")
                    await self.telegram.send(f"❌ {self.label} token is invalid", self.label)
                    self._invalid_token = True
                    self._running = False
                    self.is_connected = False
                    if self.ws:
                        await self.ws.close()
                    ready_timer.cancel()
                    return
                elif "4004" in error_msg or "authentication failed" in error_msg:
                    logger.error(f"❌ {self.label}: Authentication failed – stopping")
                    await self.telegram.send(f"❌ {self.label} token is invalid", self.label)
                    self._invalid_token = True
                    self._running = False
                    self.is_connected = False
                    if self.ws:
                        await self.ws.close()
                    ready_timer.cancel()
                    return
                elif "closed" in error_msg:
                    if self.is_connected:
                        logger.warning(f"⚠️ {self.label}: Connection closed unexpectedly")
                        ready_timer.cancel()
                        await self._reconnect()
                        return
                else:
                    logger.error(f"⚠️ {self.label}: Error: {e}")
                    continue

    async def _handle_dispatch(self, data: Dict[str, Any]):
        event_type = data.get('t')
        event_data = data.get('d', {})

        # === LOG EVERY EVENT ===
        logger.info(f"🔍 {self.label} RECEIVED: {event_type}")

        if event_type == 'READY':
            self._guilds = {g['id']: {'name': g['name']} for g in event_data.get('guilds', [])}
            user = event_data.get('user', {})
            self._user_id = user.get('id')
            username = user.get('username', 'Unknown')
            logger.info(f"✅ {self.label}: Connected as {username} monitoring {len(self._guilds)} servers")
            await self.telegram.send(f"✅ {self.label} online, monitoring {len(self._guilds)} servers", self.label)

        elif event_type == 'GUILD_MEMBER_ADD':
            guild_id = event_data.get('guild_id')
            user = event_data.get('user', {})
            logger.info(f"🔥🔥🔥 GUILD_MEMBER_ADD received! guild_id={guild_id}, user={user.get('username')}")
            if guild_id and user:
                await self._on_member_join(guild_id, user)

    async def _send_identify(self):
        fingerprint = generate_fingerprint(self.account_index)
        payload = {
            "op": 2,
            "d": {
                "token": self.token,
                "properties": fingerprint,
                "compress": False,
                "large_threshold": 250,
                "guild_subscriptions": True,
                "presence": {
                    "status": "online",
                    "since": 0,
                    "activities": [],
                    "afk": False
                },
                "client_state": {
                    "guild_versions": {},
                    "highest_last_message_id": "0",
                    "read_state_version": 0,
                    "user_guild_settings_version": -1,
                    "user_settings_version": -1
                }
            }
        }
        await self.ws.send(json.dumps(payload))
        logger.info(f"🔵 {self.label}: IDENTIFY sent with guild_subscriptions=True")

    async def _send_heartbeat(self):
        if random.random() < 0.02:
            return
        if random.random() < 0.1:
            await asyncio.sleep(random.uniform(0, 0.2))
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

        if self.is_connected:
            logger.warning(f"⚠️ {self.label}: Reconnect called while connected – ignoring")
            return

        self._connected = False
        self.is_connected = False
        self._reconnect_attempt += 1

        wait = min(30, (2 ** min(self._reconnect_attempt, 4)) + random.uniform(0, 5))
        offset = (self.account_index * 1.5) % 10
        wait = wait + offset

        logger.info(f"🔄 {self.label}: Reconnect in {wait:.1f}s")
        await asyncio.sleep(wait)

        try:
            if self.ws:
                await self.ws.close()
            if self._session:
                await self._session.close()
            await self.connect()
            self._reconnect_attempt = 0
        except Exception as e:
            if self._reconnect_attempt < 5:
                await self._reconnect()
            else:
                logger.error(f"❌ {self.label}: Max retries")
                self._running = False

    async def run(self):
        try:
            await self.connect()
        except Exception as e:
            logger.error(f"❌ {self.label}: Fatal: {e}")

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
        self.accounts: List[Dict[str, str]] = []
        self.gateways: List[DiscordGateway] = []
        self.telegram = TelegramService()

    def load_accounts(self) -> List[Dict[str, str]]:
        accounts = []
        if os.path.exists("tokens.txt"):
            try:
                with open("tokens.txt", "r") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        if ":" in line:
                            name, token = line.split(":", 1)
                            accounts.append({"name": name.strip(), "token": token.strip()})
                        else:
                            accounts.append({"name": f"Account {len(accounts)+1}", "token": line})
                logger.info(f"📱 Loaded {len(accounts)} accounts from tokens.txt")
            except Exception as e:
                logger.error(f"Error loading tokens.txt: {e}")
        
        if not accounts:
            token = os.getenv("DISCORD_TOKEN", "")
            if token:
                accounts.append({"name": "Discord_1", "token": token})
        return accounts[:MAX_ACCOUNTS]

    async def start_all(self):
        self.accounts = self.load_accounts()
        if not self.accounts:
            logger.error("❌ No accounts found")
            return

        stats["total_accounts"] = len(self.accounts)
        logger.info(f"🚀 Starting {len(self.accounts)} accounts as server join monitors")

        await self.telegram.send(f"🚀 Starting {len(self.accounts)} server join monitors", "System")

        for idx, acc in enumerate(self.accounts):
            delay = random.uniform(0.5, 3.0) if idx > 0 else 0
            if delay > 0:
                logger.info(f"⏳ {acc['name']} starting in {delay:.1f}s")
            await asyncio.sleep(delay)

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
            stats["connected_accounts"] = connected
            logger.info(f"📊 Connected: {connected}/{len(self.gateways)}")

    async def cleanup(self):
        for gateway in self.gateways:
            await gateway.close()
        await self.telegram.close()

async def main():
    print("=" * 60)
    print("🤖 Server Join Monitor - Tier 3 (Chrome TLS Spoofed)")
    print("=" * 60)

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    manager = AccountManager()
    try:
        await manager.start_all()
    except KeyboardInterrupt:
        logger.info("🛑 Shutting down...")
    finally:
        await manager.cleanup()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Interrupted")
    except Exception as e:
        print(f"❌ Error: {e}")
