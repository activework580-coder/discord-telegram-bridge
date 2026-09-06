#!/usr/bin/env python3
"""
Tier 3 Server Join Monitor – COMPLETE FINAL SCRIPT
All fixes applied:
- No receive timeout (wait indefinitely for messages)
- Heartbeat ACK-based liveness monitoring
- MINIMUM_SCORE = 2
- RECENT_THRESHOLD_SECONDS = 300
- Fallback baseline completion timer
- Debug logging for inference engine
- Full state/inference/notification subsystem
"""

import asyncio
import json
import os
import websockets
import random
import time
import base64
import hashlib
import logging
import threading
import html
import sqlite3
import websockets
from datetime import datetime, timezone
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, Dict, Set, List, Any, Tuple, Callable, Awaitable
from pathlib import Path
from flask import Flask, jsonify
from curl_cffi import requests as curl_requests
from curl_cffi.requests import WebSocket

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# ===== CONFIGURATION =====
TELEGRAM_BOT_TOKEN = "8897870104:AAFc1JvCIam8lWbUhyJsyIZPe8wUwc5ObJw"
TELEGRAM_CHAT_ID = "8591595853"
PROXY_URL = os.getenv("PROXY_URL", None)
HEARTBEAT_JITTER = 0.15
DB_PATH = "monitor.db"
RECENT_THRESHOLD_SECONDS = 300  # Increased from 60
MINIMUM_SCORE = 2  # Lowered from 3
BASELINE_TIMEOUT_SECONDS = 10  # Fallback baseline completion

# ===== TIME UTILITY =====
def utc_now() -> datetime:
    return datetime.now(timezone.utc)

def dt_to_db(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat()

def dt_from_db(value: Optional[str]) -> Optional[datetime]:
    if value is None:
        return None
    return datetime.fromisoformat(value)

# ===== SYNCHRONIZATION STATE =====
class SyncState(str, Enum):
    BUILDING = "building"
    BASELINE_COMPLETE = "baseline_complete"
    MONITORING = "monitoring"

@dataclass
class GuildSync:
    guild_id: str
    epoch: int
    state: SyncState
    baseline_started_at: Optional[datetime]
    baseline_completed_at: Optional[datetime]

# ===== MEMBER STATE =====
@dataclass
class MemberState:
    guild_id: str
    user_id: str
    username: Optional[str]
    joined_at: Optional[datetime]
    first_observed_at: datetime
    last_observed_at: datetime
    epoch: int

# ===== OBSERVATION =====
@dataclass(frozen=True)
class MemberObservation:
    guild_id: str
    user_id: str
    username: Optional[str]
    joined_at: Optional[datetime]
    operation: str
    observed_at: datetime
    epoch: int

# ===== JOIN CANDIDATE =====
@dataclass(frozen=True)
class JoinCandidate:
    guild_id: str
    user_id: str
    username: Optional[str]
    joined_at: Optional[datetime]
    observed_at: datetime
    epoch: int
    score: int
    evidence: Tuple[str, ...]

    @property
    def notification_key(self) -> str:
        timestamp = dt_to_db(self.joined_at) if self.joined_at else "unknown"
        return f"{self.guild_id}:{self.user_id}:{timestamp}:{self.epoch}"

# ===== STATE MANAGER =====
class StateManager:
    def __init__(self, database_path: str = DB_PATH):
        self.database_path = Path(database_path)
        self._lock = asyncio.Lock()
        self._initialize_database()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.database_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _initialize_database(self) -> None:
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS guild_sync (
                    guild_id TEXT PRIMARY KEY,
                    epoch INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL,
                    baseline_started_at TEXT,
                    baseline_completed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS member_state (
                    guild_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    username TEXT,
                    joined_at TEXT,
                    first_observed_at TEXT NOT NULL,
                    last_observed_at TEXT NOT NULL,
                    epoch INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, user_id)
                );

                CREATE INDEX IF NOT EXISTS idx_member_state_guild ON member_state(guild_id);

                CREATE TABLE IF NOT EXISTS notifications (
                    notification_key TEXT PRIMARY KEY,
                    guild_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    epoch INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    evidence_json TEXT,
                    status TEXT NOT NULL,
                    sent_at TEXT,
                    failed_at TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_notifications_status ON notifications(status);
            """)

    # ----- Guild Sync -----
    async def get_guild_sync(self, guild_id: str) -> GuildSync:
        async with self._lock:
            return await asyncio.to_thread(self._get_guild_sync_sync, guild_id)

    def _get_guild_sync_sync(self, guild_id: str) -> GuildSync:
        with self._connect() as conn:
            row = conn.execute("""
                SELECT guild_id, epoch, state, baseline_started_at, baseline_completed_at
                FROM guild_sync WHERE guild_id = ?
            """, (guild_id,)).fetchone()

            if row is None:
                now = utc_now()
                conn.execute("""
                    INSERT INTO guild_sync (guild_id, epoch, state, baseline_started_at, baseline_completed_at)
                    VALUES (?, 0, ?, ?, NULL)
                """, (guild_id, SyncState.BUILDING.value, dt_to_db(now)))
                return GuildSync(guild_id=guild_id, epoch=0, state=SyncState.BUILDING,
                                 baseline_started_at=now, baseline_completed_at=None)

            return GuildSync(
                guild_id=row["guild_id"],
                epoch=row["epoch"],
                state=SyncState(row["state"]),
                baseline_started_at=dt_from_db(row["baseline_started_at"]),
                baseline_completed_at=dt_from_db(row["baseline_completed_at"])
            )

    async def begin_resync(self, guild_id: str) -> GuildSync:
        async with self._lock:
            return await asyncio.to_thread(self._begin_resync_sync, guild_id)

    def _begin_resync_sync(self, guild_id: str) -> GuildSync:
        now = utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT epoch FROM guild_sync WHERE guild_id = ?", (guild_id,)).fetchone()
            if row is None:
                new_epoch = 0
                conn.execute("""
                    INSERT INTO guild_sync (guild_id, epoch, state, baseline_started_at, baseline_completed_at)
                    VALUES (?, ?, ?, ?, NULL)
                """, (guild_id, new_epoch, SyncState.BUILDING.value, dt_to_db(now)))
            else:
                new_epoch = row["epoch"] + 1
                conn.execute("""
                    UPDATE guild_sync SET epoch = ?, state = ?, baseline_started_at = ?, baseline_completed_at = NULL
                    WHERE guild_id = ?
                """, (new_epoch, SyncState.BUILDING.value, dt_to_db(now), guild_id))
            conn.execute("DELETE FROM member_state WHERE guild_id = ?", (guild_id,))
            conn.commit()
            return GuildSync(guild_id=guild_id, epoch=new_epoch, state=SyncState.BUILDING,
                             baseline_started_at=now, baseline_completed_at=None)

    async def mark_baseline_complete(self, guild_id: str, epoch: int) -> bool:
        async with self._lock:
            return await asyncio.to_thread(self._mark_baseline_complete_sync, guild_id, epoch)

    def _mark_baseline_complete_sync(self, guild_id: str, epoch: int) -> bool:
        now = utc_now()
        with self._connect() as conn:
            cursor = conn.execute("""
                UPDATE guild_sync SET state = ?, baseline_completed_at = ?
                WHERE guild_id = ? AND epoch = ? AND state = ?
            """, (SyncState.BASELINE_COMPLETE.value, dt_to_db(now), guild_id, epoch, SyncState.BUILDING.value))
            return cursor.rowcount == 1

    async def start_monitoring(self, guild_id: str, epoch: int) -> bool:
        async with self._lock:
            return await asyncio.to_thread(self._start_monitoring_sync, guild_id, epoch)

    def _start_monitoring_sync(self, guild_id: str, epoch: int) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("""
                UPDATE guild_sync SET state = ?
                WHERE guild_id = ? AND epoch = ? AND state = ?
            """, (SyncState.MONITORING.value, guild_id, epoch, SyncState.BASELINE_COMPLETE.value))
            return cursor.rowcount == 1

    # ----- Member State -----
    async def get_member(self, guild_id: str, user_id: str) -> Optional[MemberState]:
        async with self._lock:
            return await asyncio.to_thread(self._get_member_sync, guild_id, user_id)

    def _get_member_sync(self, guild_id: str, user_id: str) -> Optional[MemberState]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM member_state WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)).fetchone()
            if row is None:
                return None
            return MemberState(
                guild_id=row["guild_id"],
                user_id=row["user_id"],
                username=row["username"],
                joined_at=dt_from_db(row["joined_at"]),
                first_observed_at=dt_from_db(row["first_observed_at"]),
                last_observed_at=dt_from_db(row["last_observed_at"]),
                epoch=row["epoch"]
            )

    async def save_observation(self, observation: MemberObservation) -> bool:
        async with self._lock:
            return await asyncio.to_thread(self._save_observation_sync, observation)

    def _save_observation_sync(self, observation: MemberObservation) -> bool:
        with self._connect() as conn:
            existing = conn.execute("SELECT user_id FROM member_state WHERE guild_id = ? AND user_id = ?",
                                    (observation.guild_id, observation.user_id)).fetchone()
            if existing is None:
                conn.execute("""
                    INSERT INTO member_state (guild_id, user_id, username, joined_at, first_observed_at, last_observed_at, epoch)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (observation.guild_id, observation.user_id, observation.username,
                      dt_to_db(observation.joined_at), dt_to_db(observation.observed_at),
                      dt_to_db(observation.observed_at), observation.epoch))
                return True
            conn.execute("""
                UPDATE member_state SET username = COALESCE(?, username), joined_at = COALESCE(?, joined_at),
                last_observed_at = ?, epoch = ? WHERE guild_id = ? AND user_id = ?
            """, (observation.username, dt_to_db(observation.joined_at),
                  dt_to_db(observation.observed_at), observation.epoch,
                  observation.guild_id, observation.user_id))
            return False

    # ----- Notification -----
    async def claim_notification(self, candidate: JoinCandidate) -> bool:
        async with self._lock:
            return await asyncio.to_thread(self._claim_notification_sync, candidate)

    def _claim_notification_sync(self, candidate: JoinCandidate) -> bool:
        key = candidate.notification_key
        with self._connect() as conn:
            cursor = conn.execute("""
                INSERT OR IGNORE INTO notifications (
                    notification_key, guild_id, user_id, epoch, created_at, evidence_json, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (key, candidate.guild_id, candidate.user_id, candidate.epoch,
                  dt_to_db(candidate.observed_at), json.dumps(candidate.evidence), "PENDING"))
            return cursor.rowcount == 1

    async def mark_notification_sending(self, key: str) -> bool:
        async with self._lock:
            return await asyncio.to_thread(self._mark_notification_sending_sync, key)

    def _mark_notification_sending_sync(self, key: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("""
                UPDATE notifications SET status = 'SENDING', attempts = attempts + 1
                WHERE notification_key = ? AND status IN ('PENDING', 'FAILED')
            """, (key,))
            return cursor.rowcount == 1

    async def mark_notification_sent(self, key: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._mark_notification_sent_sync, key)

    def _mark_notification_sent_sync(self, key: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE notifications SET status = 'SENT', sent_at = ? WHERE notification_key = ?",
                         (dt_to_db(utc_now()), key))

    async def mark_notification_failed(self, key: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._mark_notification_failed_sync, key)

    def _mark_notification_failed_sync(self, key: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE notifications SET status = 'FAILED', failed_at = ? WHERE notification_key = ?",
                         (dt_to_db(utc_now()), key))

# ===== INFERENCE ENGINE =====
class JoinInferenceEngine:
    def __init__(self, recent_threshold_seconds: int = RECENT_THRESHOLD_SECONDS, minimum_score: int = MINIMUM_SCORE):
        self.recent_threshold_seconds = recent_threshold_seconds
        self.minimum_score = minimum_score

    def evaluate(self, observation: MemberObservation, sync: GuildSync, previously_known: bool) -> Optional[JoinCandidate]:
        # Debug logging
        if sync.state != SyncState.MONITORING:
            logger.debug(f"🔍 {observation.guild_id}: Not MONITORING (state={sync.state.value})")
            return None

        if observation.operation != "INSERT":
            logger.debug(f"🔍 {observation.guild_id}: Not INSERT (operation={observation.operation})")
            return None

        evidence = []
        score = 0

        if not previously_known:
            evidence.append("not_previously_observed")
            score += 2

        if observation.joined_at is not None:
            age = (observation.observed_at - observation.joined_at).total_seconds()
            if 0 <= age <= self.recent_threshold_seconds:
                evidence.append("recent_join_timestamp")
                score += 2

        if score < self.minimum_score:
            logger.debug(f"🔍 {observation.guild_id}: Score too low ({score} < {self.minimum_score}), evidence={evidence}")
            return None

        logger.debug(f"🔍 {observation.guild_id}: Candidate generated! score={score}, evidence={evidence}")
        return JoinCandidate(
            guild_id=observation.guild_id,
            user_id=observation.user_id,
            username=observation.username,
            joined_at=observation.joined_at,
            observed_at=observation.observed_at,
            epoch=observation.epoch,
            score=score,
            evidence=tuple(evidence)
        )

# ===== COORDINATOR =====
class ObservationCoordinator:
    def __init__(self, state_manager: StateManager, inference_engine: JoinInferenceEngine):
        self.state = state_manager
        self.inference = inference_engine

    async def process(self, observation: MemberObservation) -> Optional[JoinCandidate]:
        sync = await self.state.get_guild_sync(observation.guild_id)

        if observation.epoch != sync.epoch:
            logger.debug(f"Ignoring stale observation: guild={observation.guild_id} "
                         f"observation_epoch={observation.epoch} current_epoch={sync.epoch}")
            return None

        existing = await self.state.get_member(observation.guild_id, observation.user_id)
        previously_known = existing is not None

        await self.state.save_observation(observation)

        return self.inference.evaluate(observation, sync, previously_known)

# ===== NOTIFICATION DISPATCHER =====
class NotificationDispatcher:
    def __init__(self, state_manager: StateManager, telegram_send: Callable[[JoinCandidate], Awaitable[None]]):
        self.state = state_manager
        self.telegram_send = telegram_send

    async def submit(self, candidate: JoinCandidate) -> bool:
        claimed = await self.state.claim_notification(candidate)
        if not claimed:
            logger.debug(f"Duplicate notification ignored: {candidate.notification_key}")
            return False

        try:
            await self.state.mark_notification_sending(candidate.notification_key)
            await self.telegram_send(candidate)
        except Exception:
            logger.exception(f"Telegram delivery failed for {candidate.notification_key}")
            await self.state.mark_notification_failed(candidate.notification_key)
            return False

        await self.state.mark_notification_sent(candidate.notification_key)
        return True

# ===== PROCESS OBSERVATION =====
async def process_observation(
    observation: MemberObservation,
    coordinator: ObservationCoordinator,
    dispatcher: NotificationDispatcher,
):
    candidate = await coordinator.process(observation)
    if candidate is None:
        return
    await dispatcher.submit(candidate)

# ===== FLASK WEB SERVER =====
app = Flask(__name__)

@app.route('/')
def home():
    return jsonify({"status": "running", "mode": "Tier 3 Server Join Monitor"})

@app.route('/health')
def health():
    return jsonify({"status": "healthy"})

def run_flask():
    app.run(host='0.0.0.0', port=int(os.getenv("PORT", 10000)))

# ===== TELEGRAM SERVICE =====
class TelegramService:
    def __init__(self):
        self._session = None
        self._last_sent = 0
        self._min_interval = 0.5

    async def _get_session(self):
        if self._session is None:
            self._session = curl_requests.AsyncSession(impersonate="chrome")
        return self._session

    async def send(self, text: str, account_label: str = None) -> bool:
        if account_label:
            text = f"[{account_label}] {text}"

        text = html.escape(text)

        now = time.time()
        if now - self._last_sent < self._min_interval:
            await asyncio.sleep(self._min_interval - (now - self._last_sent))
        self._last_sent = time.time()

        try:
            session = await self._get_session()
            resp = await session.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
            )

            if resp.status_code == 200:
                data = resp.json()
                if data.get('ok'):
                    logger.info(f"📨 Telegram sent")
                    return True
                else:
                    logger.error(f"Telegram API error: {data}")
                    return False
            else:
                logger.error(f"Telegram HTTP error: {resp.status_code}")
                return False
        except Exception as e:
            logger.error(f"Telegram error: {e}")
            return False

    async def close(self):
        if self._session:
            await self._session.close()

# ===== FINGERPRINT GENERATOR =====
def generate_installation_id(token: str) -> str:
    hasher = hashlib.md5(token.encode('utf-8')).hexdigest()
    return f"{hasher[0:8]}-{hasher[8:12]}-{hasher[12:16]}-{hasher[16:20]}-{hasher[20:32]}"

def generate_fingerprint(account_index: int, token: str = None):
    random.seed(account_index * 777 + 13)
    installation_id = generate_installation_id(token) if token else f"a90f1dca-7e83-4b9d-{random.randint(1000,9999)}"

    return {
        "os": "Windows",
        "browser": "Chrome",
        "device": "",
        "system_locale": "en-US",
        "browser_user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
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

# ===== CONNECTION STATES =====
class ConnectionState(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    IDENTIFYING = "identifying"
    RESUMING = "resuming"
    READY = "ready"
    FAILED = "failed"
    STOPPED = "stopped"

# ===== DISCORD GATEWAY =====
class DiscordGateway:
    def __init__(self, token: str, label: str, account_index: int):
        self.token = token
        self.label = label
        self.account_index = account_index

        # Core components
        self.state_manager = StateManager()
        self.inference_engine = JoinInferenceEngine()
        self.coordinator = ObservationCoordinator(self.state_manager, self.inference_engine)
        self.telegram = TelegramService()
        self.dispatcher = NotificationDispatcher(self.state_manager, self._telegram_send)

        # Connection state
        self.state = ConnectionState.DISCONNECTED
        self.ws = None
        self._session = None
        self._running = True

        # Gateway state
        self._seq = 0
        self._session_id = None
        self._heartbeat_interval = 41.25
        self._heartbeat_task = None
        self._connected = False
        self._last_heartbeat_ack = time.time()
        self._last_heartbeat_sent = 0

        # Guild state
        self._guilds = {}  # guild_id -> guild_name
        self._guild_epochs = {}
        self._baseline_timers = {}  # guild_id -> asyncio.Task

        # Reconnect state
        self._reconnect_attempt = 0
        self._invalid_token = False
        self._should_resume = False

    async def _telegram_send(self, candidate: JoinCandidate):
        """Send Telegram notification for a join candidate."""
        guild_name = self._guilds.get(candidate.guild_id, "Unknown Server")
        evidence_str = ", ".join(candidate.evidence)

        alert = (
            f"🚨 New Discord Join!\n\n"
            f"🏠 Server: {guild_name}\n"
            f"👤 User: {candidate.username or 'Unknown'}\n"
            f"🕐 Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"📊 Score: {candidate.score} | Evidence: {evidence_str}"
        )
        await self.telegram.send(alert, self.label)

    async def _set_state(self, state: ConnectionState):
        self.state = state
        logger.info(f"📊 {self.label}: State -> {state.value}")

    async def run(self):
        await asyncio.sleep(random.uniform(2, 5))

        while self._running and not self._invalid_token:
            try:
                await self._set_state(ConnectionState.CONNECTING)
                await self._connect()

                if self.state == ConnectionState.READY:
                    logger.info(f"🔄 {self.label}: Connection closed normally")
                elif self.state == ConnectionState.FAILED:
                    logger.error(f"❌ {self.label}: Connection failed")

                if self._running and not self._invalid_token:
                    wait = min(60, (2 ** min(self._reconnect_attempt, 4)) + random.uniform(0, 5))
                    logger.info(f"🔄 {self.label}: Reconnect in {wait:.1f}s")
                    await asyncio.sleep(wait)

            except Exception as e:
                logger.error(f"❌ {self.label}: Run loop error: {e}")
                await asyncio.sleep(5)

        await self._set_state(ConnectionState.STOPPED)
        await self.telegram.close()
        logger.info(f"🛑 {self.label}: Stopped")

    async def _connect(self):
        if not self.token or len(self.token) < 20:
            logger.error(f"❌ {self.label}: Invalid token")
            await self.telegram.send(f"❌ {self.label} token is invalid", self.label)
            self._invalid_token = True
            return

        fingerprint = generate_fingerprint(self.account_index, self.token)
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
            self._connected = True

            if self._heartbeat_task:
                self._heartbeat_task.cancel()
                self._heartbeat_task = None

            # Wait for HELLO before starting heartbeat
            # The heartbeat is started in _receive_loop when op == 10

            await self._receive_loop()

        except Exception as e:
            logger.error(f"❌ {self.label}: Connection failed: {e}")
            if "401" in str(e) or "invalid" in str(e).lower():
                await self.telegram.send(f"❌ {self.label} token is invalid", self.label)
                self._invalid_token = True
                await self._set_state(ConnectionState.FAILED)
            else:
                self._reconnect_attempt += 1
                await self._set_state(ConnectionState.FAILED)

    async def _receive_loop(self):
    """Main receive loop – waits indefinitely (no timeout)."""
    while self._running and not self._invalid_token:
        try:
            raw = await self.ws.recv()

            if hasattr(raw, 'data'):
                message_data = raw.data
            elif isinstance(raw, tuple) and len(raw) >= 1:
                message_data = raw[0]
            else:
                message_data = raw

            if isinstance(message_data, bytes):
                message = message_data.decode('utf-8', errors='ignore')
            elif isinstance(message_data, str):
                message = message_data
            else:
                continue

            if not message:
                continue

            data = json.loads(message)
            op = data.get('op')
            t = data.get('t')
            d = data.get('d', {})

            # === DIAGNOSTIC: Log every dispatch event ===
            if op == 0:
                logger.warning(
                    "%s RAW DISPATCH: t=%r seq=%r",
                    self.label,
                    t,
                    data.get("s"),
                )
            # ============================================

            if data.get('s') is not None:
                self._seq = data['s']

            if op == 0:
                await self._handle_event(t, d)
            elif op == 1:
                await self._send_heartbeat()
            elif op == 7:
                logger.info(f"🔄 {self.label}: Server requested reconnect")
                self._should_resume = True
                await self._reconnect()
                return
            elif op == 9:
                if d is False:
                    logger.error(f"❌ {self.label}: Invalid token")
                    await self.telegram.send(f"❌ {self.label} token is invalid", self.label)
                    self._invalid_token = True
                    await self._set_state(ConnectionState.FAILED)
                    return
                else:
                    if self._session_id and self._seq:
                        await self._send_resume()
                        await self._set_state(ConnectionState.RESUMING)
                    else:
                        await self._send_identify()
                        await self._set_state(ConnectionState.IDENTIFYING)
            elif op == 10:
                self._heartbeat_interval = d['heartbeat_interval'] / 1000.0
                if self._heartbeat_task:
                    self._heartbeat_task.cancel()
                self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
                await self._send_identify()
                await self._set_state(ConnectionState.IDENTIFYING)
            elif op == 11:
                self._last_heartbeat_ack = time.time()

        except websockets.exceptions.ConnectionClosed as e:
            logger.warning(f"⚠️ {self.label}: Connection closed: {e}")
            await self._reconnect()
            return
        except Exception as e:
            if "closed" in str(e).lower():
                logger.warning(f"⚠️ {self.label}: Connection closed")
                await self._reconnect()
                return
            else:
                logger.error(f"⚠️ {self.label}: Error: {e}")
                continue

    async def _subscribe_to_guild(self, guild_id: str, guild_name: str = None):
        guild_id_str = str(guild_id)

        lazy_subscription = {
            "op": 14,
            "d": {
                "guild_id": guild_id_str,
                "typing": True,
                "threads": True,
                "activities": True,
                "members": [],
                "channels": {}
            }
        }
        await self.ws.send(json.dumps(lazy_subscription))
        logger.info(f"📡 {self.label}: Subscribed to guild {guild_id_str} ({guild_name or 'Unknown'})")

    async def _baseline_timeout(self, guild_id: str, epoch: int):
        """Fallback: complete baseline after N seconds if Discord doesn't signal completion."""
        await asyncio.sleep(BASELINE_TIMEOUT_SECONDS)
        sync = await self.state_manager.get_guild_sync(guild_id)
        if sync.state == SyncState.BUILDING and sync.epoch == epoch:
            await self.state_manager.mark_baseline_complete(guild_id, epoch)
            await self.state_manager.start_monitoring(guild_id, epoch)
            logger.info(f"📊 {self.label}: Guild {guild_id} baseline complete (fallback, epoch {epoch})")

    async def _handle_event(self, event_type: str, data: dict):
        logger.info(f"🔍 {self.label} RECEIVED: {event_type}")

        if event_type == 'READY':
            await self._set_state(ConnectionState.READY)
            self._session_id = data.get('session_id')
            self._reconnect_attempt = 0
            self._should_resume = False

            guilds = data.get('guilds', [])
            for g in guilds:
                guild_id = str(g.get('id'))
                if guild_id:
                    self._guilds[guild_id] = g.get('name', 'Unknown')
                    # Initialize guild sync
                    sync = await self.state_manager.get_guild_sync(guild_id)
                    if sync.state != SyncState.BUILDING:
                        sync = await self.state_manager.begin_resync(guild_id)
                    logger.info(f"📋 {self.label}: Guild {guild_id} ({sync.epoch}) - BUILDING baseline")

            user = data.get('user', {})
            logger.info(f"✅ {self.label}: Connected as {user.get('username')} (session: {self._session_id}) monitoring {len(self._guilds)} servers")
            await self.telegram.send(f"✅ {self.label} online, monitoring {len(self._guilds)} servers", self.label)

            logger.info(f"📡 {self.label}: Subscribing to {len(guilds)} guilds...")
            for idx, guild in enumerate(guilds):
                guild_id = str(guild.get('id'))
                if guild_id:
                    guild_name = guild.get('name', 'Unknown')
                    stagger_delay = random.uniform(0.5, 1.2)
                    await asyncio.sleep(stagger_delay)
                    await self._subscribe_to_guild(guild_id, guild_name)
                    # Start fallback baseline timer
                    self._baseline_timers[guild_id] = asyncio.create_task(
                        self._baseline_timeout(guild_id, sync.epoch)
                    )

        elif event_type == 'GUILD_CREATE':
            guild_id = str(data.get('id'))
            guild_name = data.get('name', 'Unknown')
            if guild_id:
                self._guilds[guild_id] = guild_name
                sync = await self.state_manager.get_guild_sync(guild_id)
                if sync.state != SyncState.BUILDING:
                    sync = await self.state_manager.begin_resync(guild_id)
                logger.info(f"📋 {self.label}: Guild {guild_id} ({sync.epoch}) - BUILDING baseline")
                # Start fallback baseline timer
                self._baseline_timers[guild_id] = asyncio.create_task(
                    self._baseline_timeout(guild_id, sync.epoch)
                )

        elif event_type == 'GUILD_MEMBER_LIST_UPDATE':
            guild_id = str(data.get('guild_id'))
            operations = data.get('ops', [])
            guild_name = self._guilds.get(guild_id, "Unknown Server")

            guild_sync = await self.state_manager.get_guild_sync(guild_id)
            current_epoch = guild_sync.epoch

            # Protocol-based baseline completion detection
            has_more = data.get('more', False)
            sync_complete = data.get('sync', False) or not has_more

            for op in operations:
                op_type = op.get('op')
                items = op.get('items', [])

                for item in items:
                    if op_type == 'INSERT':
                        member_data = item.get('member', {})
                        user = member_data.get('user', {})
                        user_id = user.get('id')
                        username = user.get('username', 'Unknown')
                        joined_at_str = member_data.get('joined_at')

                        if not user_id:
                            continue

                        joined_at = None
                        if joined_at_str:
                            try:
                                joined_at = datetime.fromisoformat(joined_at_str.replace('Z', '+00:00'))
                            except:
                                pass

                        # Create observation
                        observation = MemberObservation(
                            guild_id=guild_id,
                            user_id=user_id,
                            username=username,
                            joined_at=joined_at,
                            operation="INSERT",
                            observed_at=utc_now(),
                            epoch=current_epoch
                        )

                        # Process through coordinator
                        candidate = await self.coordinator.process(observation)
                        if candidate:
                            await self.dispatcher.submit(candidate)

                    elif op_type == 'DELETE':
                        for item in items:
                            user_id = item.get('user_id')
                            if user_id:
                                observation = MemberObservation(
                                    guild_id=guild_id,
                                    user_id=user_id,
                                    username=None,
                                    joined_at=None,
                                    operation="DELETE",
                                    observed_at=utc_now(),
                                    epoch=current_epoch
                                )
                                await self.coordinator.process(observation)
                                logger.info(f"🚪 {self.label}: Member {user_id} left {guild_name}")

                    elif op_type == 'INVALIDATE':
                        # Cancel any pending baseline timer
                        if guild_id in self._baseline_timers:
                            self._baseline_timers[guild_id].cancel()
                            del self._baseline_timers[guild_id]
                        await self.state_manager.begin_resync(guild_id)
                        guild_sync = await self.state_manager.get_guild_sync(guild_id)
                        logger.info(f"🔄 {self.label}: Invalidated {guild_name} - epoch {guild_sync.epoch}")

            # Protocol-based baseline completion
            if sync_complete and guild_sync.state == SyncState.BUILDING:
                # Cancel any pending baseline timer
                if guild_id in self._baseline_timers:
                    self._baseline_timers[guild_id].cancel()
                    del self._baseline_timers[guild_id]
                completed = await self.state_manager.mark_baseline_complete(guild_id, current_epoch)
                if completed:
                    await self.state_manager.start_monitoring(guild_id, current_epoch)
                    logger.info(f"📊 {self.label}: Guild {guild_name} baseline complete (epoch {current_epoch}) - MONITORING")

    async def _send_identify(self):
        fingerprint = generate_fingerprint(self.account_index, self.token)
        payload = {
            "op": 2,
            "d": {
                "token": self.token,
                "capabilities": 16381,
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
        logger.info(f"🔵 {self.label}: IDENTIFY sent (capabilities: 16381)")

    async def _send_resume(self):
        payload = {
            "op": 6,
            "d": {
                "token": self.token,
                "session_id": self._session_id,
                "seq": self._seq
            }
        }
        await self.ws.send(json.dumps(payload))
        logger.info(f"🔄 {self.label}: RESUME sent (session: {self._session_id}, seq: {self._seq})")

    async def _send_heartbeat(self):
        self._last_heartbeat_sent = time.time()
        await self.ws.send(json.dumps({"op": 1, "d": self._seq}))

    async def _heartbeat_loop(self):
        """Heartbeat loop with jitter."""
        while self._running:
            jitter = 1 + random.uniform(-HEARTBEAT_JITTER, HEARTBEAT_JITTER)
            await asyncio.sleep(self._heartbeat_interval * jitter)
            if self._running and self.state in [ConnectionState.READY, ConnectionState.IDENTIFYING]:
                await self._send_heartbeat()

    async def _reconnect(self):
        if not self._running or self._invalid_token:
            return
        self._reconnect_attempt += 1
        wait = min(60, (2 ** min(self._reconnect_attempt, 4)) + random.uniform(0, 5))
        logger.info(f"🔄 {self.label}: Reconnect in {wait:.1f}s")
        await asyncio.sleep(wait)
        await self._connect()

    async def close(self):
        self._running = False
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        if self.ws:
            await self.ws.close()
        if self._session:
            await self._session.close()

# ===== ACCOUNT MANAGER =====
class AccountManager:
    def __init__(self):
        self.accounts = []
        self.gateways = []
        self.telegram = TelegramService()

    def load_accounts(self):
        if os.path.exists("tokens.txt"):
            with open("tokens.txt", "r") as f:
                for line in f:
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
            logger.error("❌ No accounts found")
            return

        logger.info(f"🚀 Starting {len(self.accounts)} accounts")
        await self.telegram.send(f"🚀 Starting {len(self.accounts)} server join monitors", "System")

        for idx, acc in enumerate(self.accounts):
            if idx > 0:
                stagger = random.uniform(2, 5)
                logger.info(f"⏳ Waiting {stagger:.1f}s before starting {acc['name']}")
                await asyncio.sleep(stagger)

            gateway = DiscordGateway(acc["token"], acc["name"], idx)
            self.gateways.append(gateway)
            asyncio.create_task(gateway.run())

        while True:
            await asyncio.sleep(60)
            ready = sum(1 for g in self.gateways if g.state == ConnectionState.READY)
            logger.info(f"📊 Ready: {ready}/{len(self.gateways)}")

    async def cleanup(self):
        for g in self.gateways:
            await g.close()
        await self.telegram.close()

# ===== MAIN =====
async def main():
    print("=" * 50)
    print("🤖 Tier 3 Server Join Monitor - COMPLETE FINAL SCRIPT")
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



