#!/usr/bin/env python3
"""
Custom WebSocket Join Monitor - Manual Implementation
Reads tokens from tokens.txt file
Receives GUILD_MEMBER_ADD events via raw WebSocket connection
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
import zlib
from datetime import datetime
from flask import Flask, jsonify
import websockets
import requests

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
    return jsonify({"status": "active", "mode": "Custom WebSocket Monitor"})

@app.route('/health')
def health():
    return jsonify({"status": "healthy"}), 200

def run_flask():
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    app.run(host='0.0.0.0', port=int(os.getenv("PORT", 10000)))

# ===== TELEGRAM SERVICE =====
class TelegramNotifier:
    def __init__(self):
        self.session = None

    async def send_alert(self, text: str, account_label: str = None):
        if account_label:
            text = f'<b>{account_label}</b>\n{text}'
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
        try:
            # Use curl_cffi for better compatibility
            from curl_cffi import requests as curl_requests
            response = curl_requests.post(url, json=payload, timeout=10, impersonate="chrome")
            if response.status_code == 200:
                logger.info("📨 Alert sent to Telegram")
            else:
                logger.error(f"Telegram error: {response.status_code}")
        except Exception as e:
            logger.error(f"Telegram error: {e}")

notifier = TelegramNotifier()

# ===== DISCORD GATEWAY =====
class DiscordGateway:
    def __init__(self, token: str, label: str):
        self.token = token
        self.label = label
        self.ws = None
        self._running = True
        self._seq = 0
        self._session_id = None
        self._heartbeat_interval = 41250
        self._heartbeat_task = None
        self._connected = False
        self._guilds = {}
        self._user_id = None
        self._username = None
        self._reconnect_attempt = 0
        self._subscribed_guilds = set()
        self._gateway_url = None
        
    async def connect(self):
        """Connect to Discord WebSocket"""
        if not self.token or len(self.token) < 20:
            logger.error(f"{self.label}: Invalid token")
            await notifier.send_alert(f"❌ Invalid token for {self.label}", self.label)
            return False
            
        # Get gateway URL with retries
        gateway_url = await self._get_gateway_url_with_retry()
        if not gateway_url:
            logger.error(f"{self.label}: Failed to get gateway URL")
            await notifier.send_alert(f"❌ Failed to connect to Discord API", self.label)
            return False
        
        logger.info(f"{self.label}: 🔌 Connecting to Discord WebSocket...")
        try:
            # Connect with headers using the correct method
            self.ws = await websockets.connect(
                gateway_url,
                user_agent_header="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                compression=None,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=10
            )
            logger.info(f"{self.label}: ✅ WebSocket connected")
        except Exception as e:
            logger.error(f"{self.label}: WebSocket connection failed: {e}")
            await notifier.send_alert(f"❌ WebSocket connection failed: {str(e)[:100]}", self.label)
            return False
        
        # Start heartbeat loop
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        
        # Receive loop
        await self._receive_loop()
        return True
    
    async def _get_gateway_url_with_retry(self, max_retries=3):
        """Get gateway URL with retries and fallback URLs"""
        # Try multiple gateway URLs
        gateway_options = [
            "https://discord.com/api/v9/gateway",
            "https://discord.com/api/v10/gateway",
            "https://gateway.discord.gg/"
        ]
        
        for attempt in range(max_retries):
            for url in gateway_options:
                try:
                    logger.info(f"{self.label}: Attempting to get gateway from {url}")
                    response = requests.get(url, timeout=10)
                    if response.status_code == 200:
                        data = response.json()
                        gateway_url = data.get("url")
                        if gateway_url:
                            logger.info(f"{self.label}: 🌐 Gateway: {gateway_url}")
                            return f"{gateway_url}?v=9&encoding=json"
                except Exception as e:
                    logger.warning(f"{self.label}: Gateway attempt failed: {e}")
                    continue
            
            # Wait before retry
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt
                logger.info(f"{self.label}: Retrying in {wait_time}s...")
                await asyncio.sleep(wait_time)
        
        # Fallback: Use known gateway
        fallback = "wss://gateway.discord.gg/?v=9&encoding=json"
        logger.warning(f"{self.label}: Using fallback gateway: {fallback}")
        return fallback
    
    async def _receive_loop(self):
        """Main receive loop"""
        while self._running:
            try:
                message = await asyncio.wait_for(self.ws.recv(), timeout=60)
                
                # Handle different message types
                if isinstance(message, bytes):
                    try:
                        message = message.decode('utf-8')
                    except:
                        try:
                            decompressed = zlib.decompress(message)
                            message = decompressed.decode('utf-8')
                        except:
                            logger.warning(f"{self.label}: Failed to decode message")
                            continue
                elif isinstance(message, str):
                    pass
                else:
                    message = str(message)
                
                # Parse JSON
                try:
                    data = json.loads(message)
                except json.JSONDecodeError:
                    logger.warning(f"{self.label}: Invalid JSON: {message[:100]}")
                    continue
                
                # Process the message
                await self._process_message(data)
                
            except asyncio.TimeoutError:
                logger.warning(f"{self.label}: Receive timeout, sending heartbeat...")
                await self._send_heartbeat()
            except websockets.exceptions.ConnectionClosed as e:
                logger.warning(f"{self.label}: Connection closed: {e}")
                await self._reconnect()
                break
            except Exception as e:
                logger.error(f"{self.label}: Receive error: {e}")
                await asyncio.sleep(1)
    
    async def _process_message(self, data: dict):
        """Process incoming WebSocket messages"""
        op = data.get('op')
        t = data.get('t')
        d = data.get('d', {})
        s = data.get('s')
        
        # Update sequence number for heartbeats
        if s:
            self._seq = s
        
        # Handle different opcodes
        if op == 0:  # Dispatch
            await self._handle_dispatch(t, d)
            
        elif op == 1:  # Heartbeat
            await self._send_heartbeat()
            
        elif op == 7:  # Reconnect
            logger.info(f"{self.label}: 🔁 Reconnect requested by server")
            await self._reconnect()
            
        elif op == 9:  # Invalid session
            if d is False:
                logger.error(f"{self.label}: ❌ Invalid session, re-identifying...")
                await self._send_identity()
            else:
                await self._resume_session()
                
        elif op == 10:  # Hello
            self._heartbeat_interval = d.get('heartbeat_interval', 41250) / 1000.0
            logger.info(f"{self.label}: 💓 Heartbeat interval: {self._heartbeat_interval}s")
            self._connected = True
            await self._send_identity()
            
        elif op == 11:  # Heartbeat ACK
            logger.debug(f"{self.label}: 💓 Heartbeat ACK")
            
        else:
            logger.debug(f"{self.label}: Unhandled opcode: {op}")
    
    async def _handle_dispatch(self, event_type: str, data: dict):
        """Handle dispatched events"""
        
        if event_type == "READY":
            user = data.get('user', {})
            self._user_id = user.get('id')
            self._username = user.get('username')
            
            # Cache guilds
            guilds = data.get('guilds', [])
            for g in guilds:
                if 'id' in g:
                    self._guilds[g['id']] = g.get('name', 'Unknown')
            
            self._session_id = data.get('session_id')
            
            logger.info(f"{self.label}: ✅ Connected as: {self._username} (ID: {self._user_id})")
            
            # Send Telegram notification
            guild_names = []
            for g in list(self._guilds.values())[:10]:
                guild_names.append(f"  🏠 {g}")
            
            alert = (
                f"✅ <b>Monitor Online</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 <b>Account:</b> {self._username}\n"
                f"🆔 <b>ID:</b> <code>{self._user_id}</code>\n"
                f"📊 <b>Servers:</b> {len(guilds)}\n"
            )
            if guild_names:
                alert += f"\n<b>Monitoring:</b>\n" + "\n".join(guild_names)
                if len(guilds) > 10:
                    alert += f"\n  ... and {len(guilds) - 10} more"
            
            alert += f"\n━━━━━━━━━━━━━━━━━━━━\n"
            alert += f"⏰ <b>Time:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            alert += f"🔄 <b>Status:</b> Monitoring for new joins..."
            
            await notifier.send_alert(alert, self.label)
            
            # Subscribe to guild members for each guild
            for guild in guilds:
                guild_id = guild.get('id')
                if guild_id:
                    await asyncio.sleep(random.uniform(0.3, 0.8))
                    await self._subscribe_to_guild(guild_id)
            
            # Send ready confirmation
            await asyncio.sleep(2)
            await notifier.send_alert(
                f"🔍 <b>Now monitoring {len(guilds)} servers</b>\n"
                f"✅ Ready for join notifications!",
                self.label
            )
            
        elif event_type == "GUILD_CREATE":
            guild_id = data.get('id')
            guild_name = data.get('name', 'Unknown')
            if guild_id:
                self._guilds[guild_id] = guild_name
                logger.info(f"{self.label}: 📁 Cached: {guild_name}")
                await self._subscribe_to_guild(guild_id)
            
        elif event_type == "GUILD_MEMBER_ADD":
            # THIS IS THE EVENT WE WANT!
            guild_id = data.get('guild_id')
            user = data.get('user', {})
            
            username = user.get('username', 'Unknown')
            user_id = user.get('id', 'Unknown')
            avatar_hash = user.get('avatar')
            guild_name = self._guilds.get(guild_id, f'Server {guild_id}')
            
            # Build avatar URL if available
            avatar_url = None
            if avatar_hash:
                avatar_url = f"https://cdn.discordapp.com/avatars/{user_id}/{avatar_hash}.png"
            
            # Build the notification
            alert = (
                f"🆕 <b>New Discord Join!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🏠 <b>Server:</b> {guild_name}\n"
                f"👤 <b>User:</b> {username}\n"
                f"🆔 <b>User ID:</b> <code>{user_id}</code>\n"
            )
            
            if avatar_url:
                alert += f"🖼️ <b>Avatar:</b> <a href='{avatar_url}'>View</a>\n"
            
            alert += f"━━━━━━━━━━━━━━━━━━━━\n"
            alert += f"⏰ <b>Time:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            
            logger.info(f"{self.label}: 🚨 {username} joined {guild_name}")
            await notifier.send_alert(alert, self.label)
            
        elif event_type == "GUILD_MEMBER_UPDATE":
            pass
            
        elif event_type == "PRESENCE_UPDATE":
            pass
            
        # Log all events for debugging
        if event_type and event_type not in ["PRESENCE_UPDATE", "TYPING_START"]:
            logger.debug(f"{self.label}: 📨 Event: {event_type}")
    
    async def _subscribe_to_guild(self, guild_id: str):
        """Subscribe to guild members using Opcode 14"""
        if guild_id in self._subscribed_guilds:
            return
            
        subscription = {
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
            await self.ws.send(json.dumps(subscription))
            self._subscribed_guilds.add(guild_id)
            logger.info(f"{self.label}: 📡 Subscribed to guild: {guild_id}")
        except Exception as e:
            logger.error(f"{self.label}: Failed to subscribe: {e}")
    
    async def _send_identity(self):
        """Send identify payload"""
        fingerprint = self._generate_fingerprint()
        
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
        
        await self.ws.send(json.dumps(identity))
        logger.info(f"{self.label}: 🆔 Identity sent")
    
    async def _resume_session(self):
        """Resume a previous session"""
        resume = {
            "op": 6,
            "d": {
                "token": self.token,
                "session_id": self._session_id,
                "seq": self._seq
            }
        }
        await self.ws.send(json.dumps(resume))
        logger.info(f"{self.label}: 🔄 Resume attempt sent")
    
    def _generate_fingerprint(self):
        """Generate browser fingerprint"""
        return {
            "os": "Windows",
            "browser": "Chrome",
            "device": "",
            "system_locale": "en-US",
            "browser_user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
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
            "installation_id": self._generate_installation_id()
        }
    
    def _generate_installation_id(self):
        """Generate consistent installation ID"""
        hasher = hashlib.md5(self.token.encode('utf-8'))
        hash_hex = hasher.hexdigest()
        return f"{hash_hex[0:8]}-{hash_hex[8:12]}-{hash_hex[12:16]}-{hash_hex[16:20]}-{hash_hex[20:32]}"
    
    async def _send_heartbeat(self):
        """Send heartbeat"""
        if self.ws:
            try:
                await self.ws.send(json.dumps({"op": 1, "d": self._seq}))
                logger.debug(f"{self.label}: 💓 Heartbeat sent")
            except Exception as e:
                logger.error(f"{self.label}: Heartbeat failed: {e}")
    
    async def _heartbeat_loop(self):
        """Heartbeat loop"""
        while self._running:
            await asyncio.sleep(self._heartbeat_interval + random.uniform(-0.15, 0.15))
            if self.ws and self._connected:
                await self._send_heartbeat()
    
    async def _reconnect(self):
        """Reconnect to Discord"""
        self._reconnect_attempt += 1
        delay = min(30, 2 ** self._reconnect_attempt)
        logger.info(f"{self.label}: 🔄 Reconnecting in {delay}s...")
        self._connected = False
        await self.close()
        await asyncio.sleep(delay)
        await self.connect()
    
    async def close(self):
        """Clean up connection"""
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

# ===== ACCOUNT MANAGER =====
class AccountManager:
    def __init__(self):
        self.accounts = []
        self.gateways = []
        self._running = True

    def load_accounts(self):
        """Load Discord tokens from tokens.txt file"""
        if os.path.exists("tokens.txt"):
            try:
                with open("tokens.txt", "r") as f:
                    for line in f:
                        line = line.strip()
                        if line and ":" in line:
                            name, token = line.split(":", 1)
                            self.accounts.append({"name": name.strip(), "token": token.strip()})
                            logger.info(f"📁 Loaded: {name.strip()}")
            except Exception as e:
                logger.error(f"Error reading tokens.txt: {e}")
        else:
            logger.warning("tokens.txt not found!")
            # Create sample tokens.txt
            with open("tokens.txt", "w") as f:
                f.write("# Add your Discord tokens here\n")
                f.write("# Format: AccountName:TOKEN\n")
                f.write("Account1:YOUR_TOKEN_HERE\n")
            logger.info("📝 Created sample tokens.txt - please add your tokens!")
        return self.accounts

    async def start_all(self):
        """Start all accounts"""
        self.accounts = self.load_accounts()
        if not self.accounts:
            logger.error("No accounts loaded")
            await notifier.send_alert(
                "❌ <b>No Discord tokens found!</b>\n"
                "Create <code>tokens.txt</code> with:\n"
                "<code>Name:TOKEN</code>",
                "System"
            )
            return

        logger.info(f"🚀 Starting {len(self.accounts)} monitors")
        
        # Send startup notification
        account_names = "\n".join([f"  👤 {acc['name']}" for acc in self.accounts])
        await notifier.send_alert(
            f"🚀 <b>Discord Join Monitor Starting</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 <b>Accounts:</b> {len(self.accounts)}\n"
            f"{account_names}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⏰ <b>Started:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "System"
        )

        # Start each account
        for idx, acc in enumerate(self.accounts):
            if idx > 0:
                stagger = random.uniform(2, 5)
                logger.info(f"⏳ Waiting {stagger:.1f}s before starting {acc['name']}")
                await asyncio.sleep(stagger)
            
            gateway = DiscordGateway(acc["token"], acc["name"])
            self.gateways.append(gateway)
            asyncio.create_task(gateway.connect())

        # Keep running
        while self._running:
            await asyncio.sleep(60)
            connected = sum(1 for g in self.gateways if g.ws and not g.ws.closed and g._connected)
            total = len(self.gateways)
            logger.info(f"📊 Status: {connected}/{total} connected")

    async def cleanup(self):
        """Clean up all gateways"""
        self._running = False
        for g in self.gateways:
            await g.close()

# ===== MAIN =====
async def main():
    print("=" * 60)
    print("■ Discord Join Monitor - Custom WebSocket")
    print("=" * 60)
    
    # Start Flask server
    threading.Thread(target=run_flask, daemon=True).start()
    
    # Create account manager
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
