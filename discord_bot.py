#!/usr/bin/env python3

"""
Tier 3 Server Join Monitor - REPAIRED PRODUCTION BUILD
Receives GUILD_MEMBER_ADD with user tokens via Opcode 14 subscriptions
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
import re
from datetime import datetime
from flask import Flask, jsonify
from curl_cffi import requests as curl_requests

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# ====== HARDCODED CREDENTIALS ======
TELEGRAM_BOT_TOKEN = "8897870104:AAFc1JvC1am81WbUhyJsyI2Pe8wUwc50bJw"
TELEGRAM_CHAT_ID = "8591595853"
PROXY_URL = None
HEARTBEAT_JITTER = 0.15

# ====== FLASK WEB SERVER ======
app = Flask(__name__)

@app.route('/')
def home():
    return jsonify({'status': 'running', 'mode': 'Tier 3 Server Join Monitor'})

@app.route('/health')
def health():
    return jsonify({'status': 'healthy'})

def run_flask():
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    app.run(host='0.0.0.0', port=int(os.getenv("PORT", 10000)))

# ======= TELEGRAM SERVICE =======
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
            text = f'<b>{account_label}</b>: {text}'
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
                    "parse_mode": "HTML"
                }
            )
            logger.info(f"Telegram alert dispatched successfully.")
            return True
        except Exception as e:
            logger.error(f"Telegram error: {e}")
            return False

    async def close(self):
        if self._session:
            await self._session.close()

# ======= FINGERPRINT GENERATOR =======
def generate_installation_id(token: str) -> str:
    hasher = hashlib.md5(token.encode('utf-8')).hexdigest()
    return f"{hasher[0:8]}-{hasher[8:12]}-{hasher[12:16]}-{hasher[16:20]}-{hasher[20:32]}"

def generate_fingerprint(account_index: int, token: str = None):
    random.seed(account_index * 777 + 13)
    installation_id = generate_installation_id(token) if token else f"a90fldca-7e83-4b9d-{random.randint(1000, 5000)}"
    return {
        "os": "Windows",
        "browser": "Chrome",
        "device": " ",
        "system_locale": "en-US",
        "browser_user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)",
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
        "has_client_mods": False,
        "installation_id": installation_id
    }

# ====== DISCORD GATEWAY ======
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
        self._ready_received = False
        self._subscribed_guilds = set()

    async def run(self):
        while self._running and not self._invalid_token:
            await self.connect()
            if not self._running:
                break
            await asyncio.sleep(random.uniform(1, 5))

    async def connect(self):
        if not self.token or len(self.token) < 20:
            logger.error(f"{self.label}: Invalid token string structure.")
            await self.telegram.send(f"❌ Token is invalid", self.label)
            self._invalid_token = True
            self._running = False
            return

        fingerprint = generate_fingerprint(self.account_index, self.token)
        encoded_props = base64.b64encode(json.dumps(fingerprint).encode()).decode('utf-8')

        headers = {
            "User-Agent": fingerprint["browser_user_agent"],
            "Origin": "https://discord.com",
            "X-Super-Properties": encoded_props,
            "X-Discord-Device-Id": hashlib.sha256(f"{self.token}_{self.account_index}".encode()).hexdigest()[1:5]
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
            logger.info(f"{self.label}: Connected to Discord Gateway.")
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            await self._receive_loop()
        except Exception as e:
            logger.error(f"{self.label}: Connection runtime failure: {e}")
            if "401" in str(e) or "invalid" in str(e).lower():
                await self.telegram.send(f"❌ Token status indicates invalid authentication", self.label)
                self._invalid_token = True
                self._running = False
            else:
                await self._reconnect()

    def _clean_json_message(self, message: str) -> str:
        """Attempt to clean malformed JSON messages"""
        if not message:
            return message
            
        # Remove b'...' wrapper if present
        if message.startswith("b'") and message.endswith("'"):
            message = message[2:-1]
        elif message.startswith('b"') and message.endswith('"'):
            message = message[2:-1]
        
        # Unescape quotes
        message = message.replace('\\"', '"')
        
        # Fix common JSON issues
        # 1. Fix trailing commas before closing braces/brackets
        message = re.sub(r',\s*}', '}', message)
        message = re.sub(r',\s*\]', ']', message)
        
        # 2. Fix missing commas between objects in arrays
        message = re.sub(r'}\s*{', '},{', message)
        
        # 3. Fix single quotes to double quotes (but careful with strings)
        # Only replace single quotes that are not inside strings
        def replace_single_quotes(match):
            return '"' + match.group(1) + '"'
        # This is a simplistic approach - use JSON5 or ast.literal_eval for better handling
        
        # 4. Remove control characters
        message = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', message)
        
        return message.strip()

    def _safe_json_loads(self, message: str):
        """Safely load JSON with multiple fallback methods"""
        # First try: normal JSON parse
        try:
            return json.loads(message)
        except json.JSONDecodeError:
            pass
        
        # Second try: clean and try again
        try:
            cleaned = self._clean_json_message(message)
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass
        
        # Third try: use ast.literal_eval for Python-like dicts
        try:
            import ast
            # Convert single quotes to double quotes for JSON
            if "'" in message and '"' not in message:
                # Try to parse as Python dict with single quotes
                data = ast.literal_eval(message)
                if isinstance(data, dict):
                    return data
        except:
            pass
        
        # Fourth try: Extract JSON using regex (find anything that looks like JSON)
        try:
            # Find JSON object or array pattern
            json_pattern = r'\{[^{}]*\}|\[[^\[\]]*\]'
            matches = re.findall(json_pattern, message)
            for match in matches:
                try:
                    return json.loads(match)
                except:
                    continue
        except:
            pass
        
        # If all fails, raise the original error
        raise json.JSONDecodeError(f"Unable to parse JSON: {message[:200]}", message, 0)

    async def _extract_message(self, raw):
        """Extract and clean message from various WebSocket response formats"""
        try:
            if raw is None:
                return None
                
            # Handle tuple
            if isinstance(raw, tuple):
                if len(raw) > 0:
                    raw = raw[0]
                else:
                    return None
            
            # Handle WebSocket message object
            if hasattr(raw, "data"):
                raw = raw.data
            
            # Convert to string
            if isinstance(raw, bytes):
                message = raw.decode('utf-8', errors='ignore')
            elif isinstance(raw, str):
                message = raw
            else:
                message = str(raw)
            
            return message.strip() if message else None
            
        except Exception as e:
            logger.error(f"{self.label}: Error extracting message: {e}")
            return None

    async def _receive_loop(self):
        while self._running:
            try:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=45)
                
                message = await self._extract_message(raw)
                
                if not message:
                    continue

                # Log raw message for debugging (first 200 chars)
                logger.debug(f"{self.label} RAW: {message[:200]}...")

                # Try to parse JSON with robust handling
                try:
                    data = self._safe_json_loads(message)
                except json.JSONDecodeError as e:
                    # Check if it's a simple numeric opcode
                    if message.strip().isdigit():
                        op = int(message.strip())
                        if op == 11:
                            logger.debug(f"{self.label}: Heartbeat ACK")
                            continue
                    
                    logger.warning(f"{self.label}: Failed to parse JSON: {e}")
                    logger.debug(f"{self.label}: Message: {message[:500]}")
                    continue

                # Ensure data is a dict
                if not isinstance(data, dict):
                    logger.warning(f"{self.label}: Unexpected data type: {type(data)}")
                    continue

                # Process the message
                op = data.get('op')
                t = data.get('t')
                d = data.get('d', {})

                if op == 0:
                    self._seq = data.get('s', self._seq)
                    await self._handle_event(t, d)
                elif op == 1:
                    await self._send_heartbeat()
                elif op == 7:
                    logger.info(f"{self.label}: Server reset requested.")
                    await self._reconnect()
                    return
                elif op == 9:
                    if d is False:
                        logger.error(f"{self.label}: Handshake rejected.")
                        await self.telegram.send(f"❌ Authorization dropped", self.label)
                        self._invalid_token = True
                        self._running = False
                        return
                    else:
                        await self._send_identity()
                elif op == 10:
                    self._heartbeat_interval = d.get('heartbeat_interval', 41250) / 1000.0
                    await self._send_identity()
                    self._connected = True
                    self.is_connected = True
                    logger.info(f"{self.label}: Authorized. Heartbeat: {self._heartbeat_interval}s")
                elif op == 11:
                    logger.debug(f"{self.label}: Heartbeat ACK")
                else:
                    logger.debug(f"{self.label}: Opcode {op}")
                    
            except asyncio.TimeoutError:
                logger.warning(f"{self.label}: Receive timeout")
                await self._send_heartbeat()
            except Exception as e:
                error_str = str(e).lower()
                if "closed" in error_str or "connection" in error_str:
                    logger.warning(f"{self.label}: Connection closed")
                    await self._reconnect()
                    return
                else:
                    logger.error(f"{self.label}: Receive error: {e}")
                    await asyncio.sleep(1)

    async def _heartbeat_loop(self):
        while self._running:
            try:
                await asyncio.sleep(self._heartbeat_interval + random.uniform(-HEARTBEAT_JITTER, HEARTBEAT_JITTER))
                if self._connected and self.ws:
                    await self._send_heartbeat()
            except Exception as e:
                logger.error(f"{self.label}: Heartbeat error: {e}")
                await asyncio.sleep(5)

    async def _send_heartbeat(self):
        if self.ws:
            try:
                await self.ws.send(json.dumps({"op": 1, "d": self._seq}))
                logger.debug(f"{self.label}: Heartbeat sent")
            except Exception as e:
                logger.error(f"{self.label}: Heartbeat failed: {e}")
                self._connected = False
                self.is_connected = False

    async def _send_identity(self):
        fingerprint = generate_fingerprint(self.account_index, self.token)
        identity = {
            "op": 2,
            "d": {
                "token": self.token,
                "properties": fingerprint,
                "presence": {
                    "status": "online",
                    "since": 0,
                    "activities": [],
                    "afk": False
                },
                "compress": False,
                "large_threshold": 250,
                "client_state": {
                    "guild_versions": {},
                    "highest_last_message_id": "0",
                    "read_state_version": 0,
                    "user_guild_settings_version": 0,
                    "user_settings_version": 0
                }
            }
        }
        try:
            await self.ws.send(json.dumps(identity))
            logger.info(f"{self.label}: Identity sent")
        except Exception as e:
            logger.error(f"{self.label}: Identity failed: {e}")

    async def _subscribe_to_guild(self, guild_id: str):
        lazy_subscription = {
            "op": 14,
            "d": {
                "guild_id": guild_id,
                "typing": True,
                "threads": True,
                "activities": True,
                "members": [],
                "channels": {}
            }
        }
        try:
            await self.ws.send(json.dumps(lazy_subscription))
            self._subscribed_guilds.add(guild_id)
            logger.info(f"{self.label}: Subscribed to {guild_id}")
        except Exception as e:
            logger.error(f"{self.label}: Subscribe failed: {e}")

    async def _handle_event(self, event_type: str, data: dict):
        if event_type == "READY":
            self._ready_received = True
            guilds = data.get('guilds', [])
            for g in guilds:
                if 'id' in g:
                    self._guilds[g['id']] = g.get('name', 'Unknown')
            user = data.get('user', {})
            username = user.get('username', 'Unknown')
            logger.info(f"{self.label}: Connected as {username}")
            await self.telegram.send(
                f"✅ <b>Online</b> | <code>{username}</code>",
                self.label
            )
            for guild in guilds:
                guild_id = guild.get('id')
                if guild_id:
                    await asyncio.sleep(random.uniform(0.3, 0.8))
                    await self._subscribe_to_guild(guild_id)

        elif event_type == 'GUILD_CREATE':
            g_id = data.get('id')
            g_name = data.get('name')
            if g_id and g_name:
                self._guilds[g_id] = g_name
                logger.info(f"{self.label} Cached: {g_name}")

        elif event_type == 'GUILD_MEMBER_ADD':
            guild_id = data.get('guild_id')
            user = data.get('user', {})
            guild_name = self._guilds.get(guild_id, f'Server {guild_id}')
            username = user.get('username', 'Unknown')
            user_id = user.get('id', 'Unknown')
            
            alert = (
                f"🆕 <b>New Discord Join!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🏠 <b>Server:</b> <code>{guild_name}</code>\n"
                f"👤 <b>User:</b> <code>{username}</code>\n"
                f"🆔 <b>ID:</b> <code>{user_id}</code>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"⏰ <b>Time:</b> <code>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>"
            )
            
            logger.info(f"{self.label}: {username} joined {guild_name}")
            await self.telegram.send(alert, self.label)

    async def _reconnect(self):
        logger.info(f"{self.label}: Reconnecting...")
        self._connected = False
        self.is_connected = False
        await self.close()
        self._reconnect_attempt += 1
        delay = min(30, 2 ** self._reconnect_attempt)
        logger.info(f"{self.label}: Waiting {delay}s")
        await asyncio.sleep(delay)

    async def close(self):
        self._running = False
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except:
                pass
        if self.ws:
            try:
                await self.ws.close()
            except:
                pass
        if self._session:
            try:
                await self._session.close()
            except:
                pass

# ====== ACCOUNT MANAGER ======
class AccountManager:
    def __init__(self):
        self.accounts = []
        self.gateways = []
        self.telegram = TelegramService()

    def load_accounts(self):
        if os.path.exists("tokens.txt"):
            try:
                with open("tokens.txt", "r") as f:
                    for line in f:
                        line = line.strip()
                        if line and ":" in line:
                            name, token = line.split(":", 1)
                            self.accounts.append({"name": name.strip(), "token": token.strip()})
                            logger.info(f"Loaded: {name.strip()}")
            except Exception as e:
                logger.error(f"Error reading tokens.txt: {e}")
        else:
            logger.warning("tokens.txt not found!")
        return self.accounts

    async def start_all(self):
        self.accounts = self.load_accounts()
        if not self.accounts:
            logger.error("No accounts loaded")
            await self.telegram.send(
                "❌ <b>No Discord tokens found!</b>\n"
                "Create <code>tokens.txt</code> with:\n"
                "<code>Name:TOKEN</code>",
                "System"
            )
            return

        logger.info(f"Starting {len(self.accounts)} monitors")
        await self.telegram.send(
            f"🚀 <b>Starting {len(self.accounts)} monitors</b>",
            "System"
        )

        for idx, acc in enumerate(self.accounts):
            if idx > 0:
                stagger = random.uniform(2, 5)
                logger.info(f"Waiting {stagger:.1f}s for {acc['name']}")
                await asyncio.sleep(stagger)
            gateway = DiscordGateway(acc["token"], acc["name"], idx, self.telegram)
            self.gateways.append(gateway)
            asyncio.create_task(gateway.run())

        while True:
            await asyncio.sleep(60)
            connected = sum(1 for g in self.gateways if g.is_connected)
            total = len(self.gateways)
            logger.info(f"Status: {connected}/{total} connected")

    async def cleanup(self):
        for g in self.gateways:
            await g.close()
        await self.telegram.close()

# ====== MAIN ======
async def main():
    print("=" * 50)
    print("■ Tier 3 Server Join Monitor")
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
        print("\nClosed.")
