import asyncio
import json
import os
import random
import time
import base64
import hashlib
import logging
import threading
import signal
import sys
from datetime import datetime
from typing import Optional, Dict, Any
from flask import Flask, jsonify
from curl_cffi import requests as curl_requests
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# ===== LOGGING CONFIGURATION =====
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('discord_monitor.log')
    ]
)
logger = logging.getLogger(__name__)

# ===== CREDENTIALS (Load from environment) =====
DISCORD_USER_TOKEN = os.getenv("DISCORD_TOKEN")
TELEGRAM_BOT_TOKEN = os.getenv("8897870104:AAFc1JvC1am81WbUhyJsyI2Pe8wUwc50bJw")
TELEGRAM_CHAT_ID = os.getenv("8591595853")
PROXY_URL = os.getenv("PROXY_URL")
PORT = int(os.getenv("PORT", 10000))

# Validate required credentials
if not DISCORD_USER_TOKEN:
    raise ValueError("DISCORD_TOKEN environment variable is required")
if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    logger.warning("Telegram credentials not set. Notifications will be disabled.")

# ===== FLASK WEB SERVER =====
app = Flask(__name__)

@app.route('/')
def home():
    return jsonify({
        "status": "active",
        "mode": "Passive Join Listener",
        "uptime": time.time() - START_TIME
    })

@app.route('/health')
def health():
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now().isoformat()
    }), 200

def run_flask():
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    app.run(host='0.0.0.0', port=PORT)

# ===== TELEGRAM NOTIFIER =====
class TelegramNotifier:
    def __init__(self):
        self.session: Optional[curl_requests.AsyncSession] = None
        self.enabled = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
        self.rate_limit_delay = 1.0  # Minimum seconds between messages
        self.last_send_time = 0

    async def _ensure_session(self):
        if not self.session:
            self.session = curl_requests.AsyncSession(impersonate="chrome120")
        return self.session

    async def send_alert(self, text: str):
        """Send alert with rate limiting"""
        if not self.enabled:
            logger.info(f"Alert (Telegram disabled): {text}")
            return

        # Rate limiting
        current_time = time.time()
        if current_time - self.last_send_time < self.rate_limit_delay:
            await asyncio.sleep(self.rate_limit_delay - (current_time - self.last_send_time))

        session = await self._ensure_session()
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text[:4000],  # Telegram message limit
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        
        try:
            res = await session.post(url, json=payload, timeout=10)
            if res.status_code == 200:
                self.last_send_time = time.time()
                logger.info("📨 Alert forwarded to Telegram")
            else:
                logger.error(f"Telegram API error: {res.status_code} - {res.text}")
        except Exception as e:
            logger.error(f"Telegram dispatch error: {e}")

# ===== DEVICE FINGERPRINT GENERATOR =====
def generate_client_fingerprint(token: str) -> tuple[Dict[str, Any], str]:
    """Generate deterministic browser fingerprint based on token"""
    hasher = hashlib.sha256(token.encode()).hexdigest()
    installation_id = f"{hasher[0:8]}-{hasher[8:12]}-{hasher[12:16]}-{hasher[16:20]}-{hasher[20:32]}"
    
    properties = {
        "os": "Windows",
        "browser": "Chrome",
        "device": "",
        "system_locale": "en-US",
        "browser_user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "browser_version": "126.0.0.0",
        "os_version": "10.0.22621",
        "referrer": "",
        "referring_domain": "",
        "release_channel": "stable",
        "client_build_number": 287275,
        "client_event_source": None,
        "installation_id": installation_id
    }
    encoded = base64.b64encode(json.dumps(properties).encode()).decode()
    return properties, encoded

# ===== DISCORD GATEWAY =====
class PassiveDiscordGateway:
    def __init__(self, token: str, notifier: TelegramNotifier):
        self.token = token
        self.notifier = notifier
        self.ws = None
        self.session = None
        self.running = True
        self.seq = 0
        self.heartbeat_interval = 41.25
        self.is_connected = False
        self.guilds_cache: Dict[str, str] = {}
        self.last_msg_time = time.time()
        self.reconnect_attempts = 0
        self.max_reconnect_attempts = 10
        self.props, self.props_encoded = generate_client_fingerprint(token)

    async def connect(self):
        """Main connection loop with reconnection logic"""
        headers = {
            "User-Agent": self.props["browser_user_agent"],
            "Origin": "https://discord.com",
            "X-Super-Properties": self.props_encoded
        }

        try:
            self.session = curl_requests.AsyncSession(
                impersonate="chrome120",
                proxies={"https": PROXY_URL, "http": PROXY_URL} if PROXY_URL else None,
                timeout=30
            )
            
            async with self.session.ws_connect(
                "wss://gateway.discord.gg/?v=9&encoding=json",
                headers=headers
            ) as ws:
                self.ws = ws
                self.last_msg_time = time.time()
                self.reconnect_attempts = 0
                logger.info("WebSocket connection established")
                
                async for message in ws:
                    self.last_msg_time = time.time()
                    if not message:
                        continue
                    
                    try:
                        data = json.loads(message)
                    except json.JSONDecodeError:
                        logger.warning("Failed to decode WebSocket message")
                        continue
                    
                    op = data.get('op')
                    t = data.get('t')
                    d = data.get('d', {})

                    if op == 0:  # Dispatch
                        self.seq = data.get('s', self.seq)
                        await self._parse_event(t, d)
                    elif op == 1:  # Heartbeat
                        await self._send_heartbeat()
                    elif op == 7:  # Reconnect
                        logger.warning("Reconnect signal received from Discord")
                        break
                    elif op == 9:  # Invalid session
                        logger.warning("Invalid session, re-identifying")
                        await self._send_identify()
                    elif op == 10:  # Hello
                        self.heartbeat_interval = d['heartbeat_interval'] / 1000.0
                        self._start_heartbeat_task()
                        await self._send_identify()
                        self.is_connected = True

        except Exception as e:
            logger.error(f"Gateway error: {e}")
        
        self.is_connected = False
        await self._handle_reconnect()

    async def _handle_reconnect(self):
        """Handle reconnection with exponential backoff"""
        self.reconnect_attempts += 1
        
        if self.reconnect_attempts > self.max_reconnect_attempts:
            logger.critical("Max reconnection attempts reached. Restarting...")
            await asyncio.sleep(30)
            self.reconnect_attempts = 0
        
        backoff = min(2 ** self.reconnect_attempts, 60)
        jitter = random.uniform(0, 1)
        delay = backoff + jitter
        
        logger.info(f"Reconnecting in {delay:.2f} seconds (attempt {self.reconnect_attempts})")
        await asyncio.sleep(delay)

    def _start_heartbeat_task(self):
        """Start heartbeat loop if not already running"""
        if not hasattr(self, '_heartbeat_task') or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def _send_identify(self):
        payload = {
            "op": 2,
            "d": {
                "token": self.token,
                "capabilities": 16381,
                "properties": self.props,
                "compress": False,
                "large_threshold": 100,
                "guild_subscriptions": True,
                "presence": {
                    "status": "invisible",  # More discrete than "online"
                    "since": 0,
                    "activities": [],
                    "afk": False
                },
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
        logger.info("IDENTIFY packet sent")

    async def _subscribe_to_guild(self, guild_id: str):
        """Subscribe to guild events with rate limiting"""
        payload = {
            "op": 14,
            "d": {
                "guild_id": guild_id,
                "typing": False,  # Disable unnecessary events
                "threads": True,
                "activities": False,  # Reduce bandwidth
                "members": [],
                "channels": {}
            }
        }
        await self.ws.send(json.dumps(payload))
        logger.debug(f"Subscribed to guild {guild_id}")

    async def _parse_event(self, event_type: str, data: dict):
        """Handle Discord gateway events"""
        if event_type == 'READY':
            logger.info("Successfully authenticated to Discord gateway")
            guilds = data.get('guilds', [])
            logger.info(f"Monitoring {len(guilds)} guilds")
            
            # Process guilds in batches to avoid rate limits
            batch_size = 5
            for i in range(0, len(guilds), batch_size):
                batch = guilds[i:i+batch_size]
                for g in batch:
                    if 'id' in g and 'name' in g:
                        self.guilds_cache[g['id']] = g['name']
                        await asyncio.sleep(random.uniform(0.3, 0.6))
                        await self._subscribe_to_guild(g['id'])
                
                # Wait between batches
                if i + batch_size < len(guilds):
                    await asyncio.sleep(random.uniform(1, 2))

        elif event_type == 'GUILD_CREATE':
            g_id = data.get('id')
            g_name = data.get('name')
            if g_id and g_name:
                self.guilds_cache[g_id] = g_name
                # Subscribe if not already subscribed
                await self._subscribe_to_guild(g_id)

        elif event_type == 'GUILD_MEMBER_ADD':
            await self._handle_member_join(data)

    async def _handle_member_join(self, data: dict):
        """Handle new member join events"""
        guild_id = data.get('guild_id')
        user = data.get('user', {})
        guild_name = self.guilds_cache.get(guild_id, f"Server ({guild_id})")
        username = user.get('username', 'Unknown User')
        user_id = user.get('id', 'Unknown ID')
        
        alert_text = (
            f"🚨 <b>New Discord Join Detected!</b>\n\n"
            f"🏠 <b>Server:</b> {guild_name}\n"
            f"👤 <b>User:</b> {username}\n"
            f"🆔 <b>User ID:</b> <code>{user_id}</code>\n"
            f"⏰ <b>Time:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        
        logger.info(f"New member {username} joined {guild_name}")
        await self.notifier.send_alert(alert_text)

    async def _send_heartbeat(self):
        """Send heartbeat packet"""
        if self.ws:
            try:
                await self.ws.send(json.dumps({"op": 1, "d": self.seq}))
            except Exception as e:
                logger.error(f"Heartbeat send failed: {e}")

    async def _heartbeat_loop(self):
        """Maintain heartbeat with jitter"""
        while self.is_connected:
            jitter = random.uniform(0.85, 1.15)
            await asyncio.sleep(self.heartbeat_interval * jitter)
            if self.is_connected:
                await self._send_heartbeat()

    async def cleanup(self):
        """Clean up resources"""
        self.running = False
        self.is_connected = False
        if self.ws:
            try:
                await self.ws.close()
            except:
                pass
        if self.session:
            try:
                await self.session.close()
            except:
                pass

# ===== MAIN MONITOR LOOP =====
START_TIME = time.time()
gateway_instance = None

async def run_monitor():
    """Main monitoring loop"""
    global gateway_instance
    
    notifier = TelegramNotifier()
    gateway = PassiveDiscordGateway(DISCORD_USER_TOKEN, notifier)
    gateway_instance = gateway
    
    # Graceful shutdown handler
    def signal_handler(sig, frame):
        logger.info("Shutdown signal received")
        asyncio.create_task(gateway.cleanup())
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Watchdog task
    async def watchdog():
        while True:
            await asyncio.sleep(30)
            if gateway.is_connected and (time.time() - gateway.last_msg_time > 120):
                logger.warning("Connection appears stalled, forcing reconnect")
                gateway.is_connected = False
                # Force reconnect by closing websocket
                if gateway.ws:
                    try:
                        await gateway.ws.close()
                    except:
                        pass
    
    watchdog_task = asyncio.create_task(watchdog())
    
    # Main connection loop
    while True:
        try:
            logger.info("Starting Discord gateway connection...")
            await gateway.connect()
        except KeyboardInterrupt:
            logger.info("Interrupted by user")
            break
        except Exception as e:
            logger.error(f"Unexpected error in main loop: {e}")
            await asyncio.sleep(5)

if __name__ == "__main__":
    # Start Flask server in background
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    # Run the async monitor
    try:
        asyncio.run(run_monitor())
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    except Exception as e:
        logger.critical(f"Fatal error: {e}")
        sys.exit(1)
