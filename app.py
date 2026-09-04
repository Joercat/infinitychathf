import os
import re
import json
import time
import uuid
import base64
import logging
import tempfile
import shutil
import asyncio
from typing import Optional, Dict, List, Any, Tuple
from contextlib import asynccontextmanager

import aiosqlite

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Header, Query, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.backends import default_backend

from storage_handler import (
    store_file, retrieve_file, delete_file,
    download_database, upload_database, start_db_sync
)

# ------------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------------
APP_VERSION = "2.0.0"
DATABASE_URL = os.environ.get("DATABASE_URL", "/data/infinitychat.db")
MESSAGE_KEY_B64 = os.environ.get("SECRET_KEY", None)
FILE_ENCRYPTION_KEY_B64 = os.environ.get("FILE_ENCRYPTION_KEY", None)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("InfinityChat")

if MESSAGE_KEY_B64 is None:
    MESSAGE_KEY_B64 = base64.urlsafe_b64encode(os.urandom(32)).decode()
    logger.warning(
        "SECRET_KEY not set - generated a random key for this run. "
        "Existing messages will be unreadable next restart unless you persist it!"
    )
try:
    MESSAGE_KEY = base64.urlsafe_b64decode(MESSAGE_KEY_B64)
except Exception:
    raise ValueError("SECRET_KEY is not valid base64")
assert len(MESSAGE_KEY) == 32, "SECRET_KEY must decode to exactly 32 bytes"

# Temp directory for chunk assembly - uses disk not RAM
TEMP_DIR = os.environ.get("TEMP_DIR", "/tmp/infinitychat_uploads")
os.makedirs(TEMP_DIR, exist_ok=True)

# ------------------------------------------------------------------------
# App constants
# ------------------------------------------------------------------------
GLOBAL_CONVERSATION_ID = 1          # id of the public lobby room
MAX_PRIVATE_CHATS_PER_PAIR = 3      # up to 3 private chats between two people
MAX_MESSAGE_LENGTH = 10000
MAX_FILE_SIZE = 500 * 1024 * 1024   # 500MB upload cap
DOWNLOAD_CHUNK_SIZE = 1024 * 1024   # 1MB streaming chunks

# ------------------------------------------------------------------------
# Database initialization + additive migrations (v1 -> v2)
# ------------------------------------------------------------------------
async def _table_columns(db: aiosqlite.Connection, table: str) -> set:
    cursor = await db.execute(f"PRAGMA table_info({table})")
    rows = await cursor.fetchall()
    return {r["name"] for r in rows}


async def _ensure_column(db: aiosqlite.Connection, table: str, column: str, ddl: str):
    """Add a column if it doesn't exist yet (safe, non-destructive upgrade)."""
    cols = await _table_columns(db, table)
    if column not in cols:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        logger.info(f"🛠  Migration: added column {table}.{column}")


async def init_database():
    os.makedirs(os.path.dirname(DATABASE_URL), exist_ok=True)
    db_existed = download_database(DATABASE_URL)
    if db_existed:
        logger.info("✅ Restored database from bucket")
    else:
        logger.info("🆕 Starting with fresh database")

    async with aiosqlite.connect(DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA foreign_keys=ON;")
        await db.execute("PRAGMA synchronous=NORMAL;")
        await db.execute("PRAGMA cache_size=-20000;")
        await db.execute("PRAGMA busy_timeout=10000;")

        # --- Base tables (v1 schema, unchanged shapes so old DBs keep working) ---
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                display_name TEXT NOT NULL DEFAULT '',
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                avatar_path TEXT,
                token TEXT UNIQUE,
                created_at INTEGER DEFAULT (strftime('%s','now')),
                last_seen INTEGER DEFAULT (strftime('%s','now')),
                status TEXT DEFAULT 'offline'
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id INTEGER NOT NULL,
                encrypted_content TEXT NOT NULL,
                timestamp_ms INTEGER NOT NULL,
                reply_to_id INTEGER,
                is_edited INTEGER DEFAULT 0,
                is_deleted INTEGER DEFAULT 0,
                file_path TEXT,
                file_type TEXT,
                file_name TEXT,
                file_size INTEGER DEFAULT 0,
                conversation_id INTEGER NOT NULL DEFAULT 1,
                client_id TEXT,
                FOREIGN KEY(sender_id) REFERENCES users(id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS read_receipts (
                user_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                read_at INTEGER DEFAULT (strftime('%s','now')),
                PRIMARY KEY (user_id, message_id),
                FOREIGN KEY(user_id) REFERENCES users(id),
                FOREIGN KEY(message_id) REFERENCES messages(id)
            )
        """)

        # --- v2 tables ---
        await db.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL DEFAULT 'global' CHECK(type IN ('global','dm')),
                title TEXT,
                created_by INTEGER,
                user_low_id INTEGER,
                user_high_id INTEGER,
                created_at INTEGER DEFAULT (strftime('%s','now'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS conversation_members (
                conversation_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                joined_at INTEGER DEFAULT (strftime('%s','now')),
                last_read_message_id INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (conversation_id, user_id),
                FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)

        # Blocking: one-way rows (A blocks B). Chat creation/access is restricted
        # when either user in a pair has blocked the other.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_blocks (
                blocker_id INTEGER NOT NULL,
                blocked_id INTEGER NOT NULL,
                created_at INTEGER DEFAULT (strftime('%s','now')),
                PRIMARY KEY (blocker_id, blocked_id),
                FOREIGN KEY(blocker_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(blocked_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)

        # --- Additive migrations for databases created before v2 ---
        msg_cols = await _table_columns(db, "messages")
        if "conversation_id" not in msg_cols:
            await _ensure_column(db, "messages", "conversation_id",
                                 "INTEGER NOT NULL DEFAULT 1")
        if "client_id" not in msg_cols:
            await _ensure_column(db, "messages", "client_id", "TEXT")
        if "is_edited" not in msg_cols:
            await _ensure_column(db, "messages", "is_edited", "INTEGER DEFAULT 0")
        if "is_deleted" not in msg_cols:
            await _ensure_column(db, "messages", "is_deleted", "INTEGER DEFAULT 0")

        usr_cols = await _table_columns(db, "users")
        for col, ddl in {
            "display_name": "TEXT NOT NULL DEFAULT ''",
            "avatar_path": "TEXT",
            "last_seen": "INTEGER DEFAULT (strftime('%s','now'))",
            "status": "TEXT DEFAULT 'offline'",
        }.items():
            if col not in usr_cols:
                await _ensure_column(db, "users", col, ddl)

        conv_cols = await _table_columns(db, "conversations")
        for col, ddl in {
            "is_group": "INTEGER NOT NULL DEFAULT 0",
            "custom_name": "TEXT",
        }.items():
            if col not in conv_cols:
                await _ensure_column(db, "conversations", col, ddl)

        cm_cols = await _table_columns(db, "conversation_members")
        for col, ddl in {
            "status": "TEXT NOT NULL DEFAULT 'accepted'",
            "role": "TEXT NOT NULL DEFAULT 'member'",
        }.items():
            if col not in cm_cols:
                await _ensure_column(db, "conversation_members", col, ddl)

        # Existing memberships predate invites/roles: make them accepted.
        await db.execute(
            "UPDATE conversation_members SET status = 'accepted' "
            "WHERE status IS NULL OR status = '' OR status = 'pending'"
        )
        await db.execute(
            "UPDATE conversation_members SET role = 'member' "
            "WHERE role IS NULL OR role = ''"
        )
        # Group owner is the first accepted member of each group with no owner.
        await db.execute("""
            UPDATE conversation_members
            SET role = 'owner'
            WHERE conversation_id IN (
                SELECT id FROM conversations WHERE is_group = 1
            )
              AND user_id = (
                  SELECT user_id FROM conversation_members cm
                  WHERE cm.conversation_id = conversation_members.conversation_id
                    AND cm.status = 'accepted'
                  ORDER BY cm.joined_at ASC, cm.user_id ASC LIMIT 1
              )
        """)

        rc_cols = await _table_columns(db, "read_receipts")
        if "read_at" not in rc_cols:
            await _ensure_column(db, "read_receipts", "read_at",
                                 "INTEGER DEFAULT (strftime('%s','now'))")
        # Old receipts stored seconds - upgrade them to milliseconds
        await db.execute(
            "UPDATE read_receipts SET read_at = read_at * 1000 WHERE read_at < 100000000000"
        )

        # Seed the global lobby conversation (id 1 stays stable across versions)
        await db.execute(
            "INSERT OR IGNORE INTO conversations (id, type, title, created_by) "
            "VALUES (?, 'global', 'Global Chat', NULL)",
            (GLOBAL_CONVERSATION_ID,)
        )

        # Backfill: any message that somehow predates conversations gets the lobby
        await db.execute(
            "UPDATE messages SET conversation_id = ? "
            "WHERE conversation_id IS NULL OR conversation_id NOT IN "
            "(SELECT id FROM conversations WHERE id = ?)",
            (GLOBAL_CONVERSATION_ID, GLOBAL_CONVERSATION_ID)
        )

        # --- Indexes ---
        await db.execute("CREATE INDEX IF NOT EXISTS idx_messages_sender_time ON messages(sender_id, timestamp_ms)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_messages_time_id ON messages(timestamp_ms, id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_messages_not_deleted ON messages(is_deleted, id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, id)")
        await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_client_dedupe "
                         "ON messages(sender_id, client_id) WHERE client_id IS NOT NULL")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_receipts_user_msg ON read_receipts(user_id, message_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_receipts_message ON read_receipts(message_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_members_user ON conversation_members(user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_members_status "
                         "ON conversation_members(user_id, status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_conv_dm_pair "
                         "ON conversations(type, user_low_id, user_high_id) WHERE type = 'dm'")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_blocks_blocked "
                         "ON user_blocks(blocked_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_blocks_blocker "
                         "ON user_blocks(blocker_id)")
        await db.commit()

    upload_database(DATABASE_URL)
    start_db_sync(DATABASE_URL)
    logger.info("✅ Database initialized successfully (schema v2)")

    # Clean up any leftover temp upload dirs from previous runs
    _cleanup_temp_dir()


# ------------------------------------------------------------------------
# FastAPI lifespan
# ------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_database()
    yield
    logger.info("🔄 Final database sync on shutdown...")
    upload_database(DATABASE_URL)
    _cleanup_temp_dir()


app = FastAPI(title="InfinityChat", version=APP_VERSION, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,     # auth travels via header/query, never cookies
    allow_methods=["*"],
    allow_headers=["*"]
)
app.mount("/static", StaticFiles(directory="static"), name="static")


# ------------------------------------------------------------------------
# Temp directory cleanup
# ------------------------------------------------------------------------
def _cleanup_temp_dir():
    """Remove all leftover temp chunk dirs older than 2 hours."""
    try:
        now = time.time()
        for entry in os.scandir(TEMP_DIR):
            if entry.is_dir():
                age = now - entry.stat().st_mtime
                if age > 7200:  # 2 hours
                    shutil.rmtree(entry.path, ignore_errors=True)
                    logger.debug(f"🧹 Cleaned up old temp dir: {entry.path}")
    except Exception as e:
        logger.warning(f"Temp cleanup error: {e}")


# ------------------------------------------------------------------------
# Database helper
# ------------------------------------------------------------------------
async def get_db() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(DATABASE_URL)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    await conn.execute("PRAGMA busy_timeout=10000")
    return conn


def schedule_db_sync():
    import threading
    threading.Thread(target=upload_database, args=(DATABASE_URL,), daemon=True).start()


# ------------------------------------------------------------------------
# Encryption helpers (AES-256-GCM)
# ------------------------------------------------------------------------
def encrypt_message(plaintext: str) -> str:
    aesgcm = AESGCM(MESSAGE_KEY)
    nonce = os.urandom(12)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode('utf-8'), None)
    return base64.urlsafe_b64encode(nonce + ciphertext).decode('utf-8')


def decrypt_message(encrypted_b64: str) -> str:
    try:
        raw = base64.urlsafe_b64decode(encrypted_b64)
        nonce, ciphertext = raw[:12], raw[12:]
        aesgcm = AESGCM(MESSAGE_KEY)
        return aesgcm.decrypt(nonce, ciphertext, None).decode('utf-8')
    except Exception:
        return "[Decryption Error]"


# ------------------------------------------------------------------------
# Password hashing (PBKDF2)
# ------------------------------------------------------------------------
def hash_password(password: str, salt: Optional[str] = None) -> Tuple[str, str]:
    if salt is None:
        salt = os.urandom(32).hex()
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt.encode(),
        iterations=600000,
        backend=default_backend()
    )
    key = base64.urlsafe_b64encode(kdf.derive(password.encode())).decode()
    return key, salt


def verify_password(password: str, salt: str, stored_hash: str) -> bool:
    key, _ = hash_password(password, salt)
    return key == stored_hash


def generate_token() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


# ------------------------------------------------------------------------
# Authentication
# ------------------------------------------------------------------------
async def authenticate_user(token: str) -> Optional[dict]:
    if not token or token in ('null', 'undefined'):
        return None
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, username, display_name, avatar_path, status FROM users WHERE token = ?",
            (token,)
        )
        user = await cursor.fetchone()
        if user:
            await db.execute(
                "UPDATE users SET last_seen = ? WHERE id = ?",
                (int(time.time()), user['id'])
            )
            await db.commit()
            return dict(user)
        return None
    finally:
        await db.close()


# ------------------------------------------------------------------------
# Conversation helpers
# ------------------------------------------------------------------------
async def _block_exists(db: aiosqlite.Connection, a: int, b: int) -> bool:
    """True when blocking exists in either direction (mutual restriction)."""
    if a == b:
        return False
    cursor = await db.execute(
        "SELECT 1 FROM user_blocks WHERE (blocker_id = ? AND blocked_id = ?) "
        "OR (blocker_id = ? AND blocked_id = ?) LIMIT 1",
        (a, b, b, a)
    )
    return await cursor.fetchone() is not None


async def _conversation_blocked(db: aiosqlite.Connection, cid: int, uid: int) -> bool:
    """A non-global conversation is hidden when the user blocks (or is blocked by)
    any other accepted member."""
    if cid == GLOBAL_CONVERSATION_ID:
        return False
    member_ids = await _raw_conversation_member_ids(db, cid)
    for mid in member_ids:
        if mid != uid and await _block_exists(db, uid, mid):
            return True
    return False


async def _membership(db: aiosqlite.Connection, cid: int, uid: int) -> Optional[dict]:
    cursor = await db.execute(
        "SELECT * FROM conversation_members WHERE conversation_id = ? AND user_id = ?",
        (cid, uid)
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def user_in_conversation(db: aiosqlite.Connection, uid: int, cid: int) -> bool:
    """Everyone is in the global lobby; other chats require an accepted membership
    and no active block with another member."""
    if cid == GLOBAL_CONVERSATION_ID:
        return True
    member = await _membership(db, cid, uid)
    if not member or member.get("status") != "accepted":
        return False
    if await _conversation_blocked(db, cid, uid):
        return False
    return True


async def _raw_conversation_member_ids(db: aiosqlite.Connection, cid: int) -> List[int]:
    if cid == GLOBAL_CONVERSATION_ID:
        cursor = await db.execute("SELECT id FROM users")
        return [r["id"] for r in await cursor.fetchall()]
    cursor = await db.execute(
        "SELECT user_id FROM conversation_members "
        "WHERE conversation_id = ? AND status = 'accepted'",
        (cid,)
    )
    return [r["user_id"] for r in await cursor.fetchall()]


async def _blocked_member_ids(db: aiosqlite.Connection, cid: int) -> set:
    """Members of a non-global conversation who are in a mutual block pair with
    another accepted member. Those members are excluded from live broadcasts so
    hidden/blocked chats never leak messages."""
    if cid == GLOBAL_CONVERSATION_ID:
        return set()
    member_ids = await _raw_conversation_member_ids(db, cid)
    blocked = set()
    for mid in member_ids:
        for other in member_ids:
            if mid != other and await _block_exists(db, mid, other):
                blocked.add(mid)
                blocked.add(other)
    return blocked


async def conversation_member_ids(db: aiosqlite.Connection, cid: int) -> List[int]:
    """Accepted member ids of a conversation (for scoped broadcasts).

    Global broadcasts include everyone. Non-global broadcasts exclude users who
    are in a block pair so hidden chats never leak messages."""
    member_ids = await _raw_conversation_member_ids(db, cid)
    if cid == GLOBAL_CONVERSATION_ID:
        return member_ids
    blocked = await _blocked_member_ids(db, cid)
    return [mid for mid in member_ids if mid not in blocked]


async def _group_update_recipient_ids(db: aiosqlite.Connection, cid: int) -> List[int]:
    """Recipients for group summary/update/deleted events.

    Includes accepted members and pending invitees, but still excludes anyone
    who has an active block with another member of the group so hidden chats
    never leak through live events."""
    if cid == GLOBAL_CONVERSATION_ID:
        return []
    ids = await _raw_conversation_member_ids(db, cid)
    cursor = await db.execute(
        "SELECT user_id FROM conversation_members WHERE conversation_id = ? AND status = 'pending'",
        (cid,)
    )
    ids += [r["user_id"] for r in await cursor.fetchall()]
    return [mid for mid in ids if not await _conversation_blocked(db, cid, mid)]


async def _conversation_member_rows(db: aiosqlite.Connection, cid: int, include_pending: bool = False) -> List[dict]:
    cond = "" if include_pending else "AND cm.status = 'accepted'"
    cursor = await db.execute(
        f"""SELECT u.id, u.username, u.display_name, u.avatar_path,
                   cm.status, cm.role, cm.joined_at, u.status AS user_status,
                   u.last_seen
            FROM conversation_members cm JOIN users u ON u.id = cm.user_id
            WHERE cm.conversation_id = ? {cond}
            ORDER BY cm.joined_at ASC, u.id ASC""",
        (cid,)
    )
    return [dict(r) for r in await cursor.fetchall()]


async def _ensure_global_member(db: aiosqlite.Connection, uid: int):
    """First sighting of a user: mark the whole existing lobby as already-read."""
    cursor = await db.execute(
        "SELECT MAX(id) AS max_id FROM messages WHERE conversation_id = ?",
        (GLOBAL_CONVERSATION_ID,)
    )
    row = await cursor.fetchone()
    max_id = row["max_id"] or 0
    await db.execute(
        "INSERT OR IGNORE INTO conversation_members "
        "(conversation_id, user_id, last_read_message_id, status, role) "
        "VALUES (?, ?, ?, 'accepted', 'member')",
        (GLOBAL_CONVERSATION_ID, uid, max_id)
    )


def _row_to_public_user(row) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"],
        "avatar_path": row["avatar_path"],
    }


def _preview_for(content: str, file_type: str, file_name: str) -> str:
    content = (content or "").strip()
    if content:
        # mirror client-side markdown so previews read like the real message
        text = re.sub(r'<img\b[^>]*>', ' ', content)
        text = re.sub(r'```', ' ', text)
        text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
        text = re.sub(r'`([^`]+)`', r'\1', text)
        text = re.sub(r'([\s(>]|^)\*([^*\n]+)\*(?=[\s.,!?;:)\n]|$)', r'\1\2', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:90]
    if file_type and file_type.startswith("image/"):
        return "📷 Photo"
    if file_type and file_type.startswith("video/"):
        return "🎬 Video"
    if file_type and file_type.startswith("audio/"):
        return "🎵 Audio"
    return f"📎 {file_name or 'File'}"


async def _peer_for(db: aiosqlite.Connection, cid: int, uid: int) -> Optional[dict]:
    """The other person inside a DM conversation."""
    cursor = await db.execute(
        """SELECT u.id, u.username, u.display_name, u.avatar_path, u.status, u.last_seen
           FROM conversation_members cm JOIN users u ON u.id = cm.user_id
           WHERE cm.conversation_id = ? AND cm.user_id != ? LIMIT 1""",
        (cid, uid)
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def _dm_number(db: aiosqlite.Connection, cid: int) -> int:
    cursor = await db.execute(
        """SELECT id FROM conversations
           WHERE type = 'dm' AND is_group = 0 AND user_low_id = (
               SELECT user_low_id FROM conversations WHERE id = ?
           ) AND user_high_id = (
               SELECT user_high_id FROM conversations WHERE id = ?
           ) ORDER BY id""",
        (cid, cid)
    )
    ids = [r["id"] for r in await cursor.fetchall()]
    try:
        return ids.index(cid) + 1
    except ValueError:
        return 1


async def _build_conversation_summary(
    db: aiosqlite.Connection, cid: int, uid: int, online_ids: set = None
) -> dict:
    """One conversation as sent to the client (sidebar row)."""
    cursor = await db.execute(
        "SELECT * FROM conversations WHERE id = ?", (cid,)
    )
    conv = await cursor.fetchone()
    if not conv:
        raise HTTPException(404, "Conversation not found")

    online_ids = online_ids if online_ids is not None else set()
    membership = await _membership(db, cid, uid) or {}
    is_group = bool(conv["is_group"] or 0) if "is_group" in conv.keys() else False

    # Display title: custom rename wins for DMs/groups, otherwise group title
    # or the name of the other person in a DM.
    title = conv["title"] or "Chat"
    custom_name = conv["custom_name"] if "custom_name" in conv.keys() else None
    dm_number = 1
    if custom_name:
        title = custom_name

    summary = {
        "id": conv["id"],
        "type": conv["type"],
        "is_group": is_group,
        "title": title,
        "custom_name": custom_name,
        "created_at": conv["created_at"],
        "peer": None,
        "peer_online": False,
        "dm_number": dm_number,
        "status": membership.get("status", "accepted"),
        "role": membership.get("role", "member"),
        "is_owner": membership.get("role") == "owner",
        "is_pending": membership.get("status") == "pending",
        "member_count": 0,
        "pending_member_count": 0,
        "members": [],
        "last_message_id": None,
        "last_message_preview": "",
        "last_message_ts": None,
        "last_sender_name": "",
        "last_sender_id": None,
        "last_message_type": None,
        "unread_count": 0,
    }

    members = await _conversation_member_rows(db, cid, include_pending=is_group)
    summary["member_count"] = sum(1 for m in members if m["status"] == "accepted")
    summary["pending_member_count"] = sum(1 for m in members if m["status"] == "pending")
    public_members = []
    for m in members:
        public_members.append({
            "id": m["id"],
            "username": m["username"],
            "display_name": m["display_name"],
            "avatar_path": m["avatar_path"],
            "online": m["id"] in online_ids,
            "last_seen": m["last_seen"],
            "status": m["status"],
            "role": m["role"],
        })
    summary["members"] = public_members

    if not is_group and conv["type"] == "dm":
        peer = await _peer_for(db, cid, uid)
        if peer:
            summary["peer"] = {
                "id": peer["id"],
                "username": peer["username"],
                "display_name": peer["display_name"],
                "avatar_path": peer["avatar_path"],
                "online": peer["id"] in online_ids,
                "last_seen": peer["last_seen"],
            }
            summary["peer_online"] = peer["id"] in online_ids
            summary["dm_number"] = await _dm_number(db, cid)
            if not custom_name:
                title = peer["display_name"] or peer["username"]
                summary["title"] = title
        # If no custom name and no peer (unlikely), keep generic title.

    # Newest visible message (encrypted in DB - decrypt only what we preview)
    cursor = await db.execute(
        """SELECT m.id, m.sender_id, m.encrypted_content, m.timestamp_ms,
                  m.file_path, m.file_type, m.file_name, u.username, u.display_name
           FROM messages m JOIN users u ON u.id = m.sender_id
           WHERE m.conversation_id = ? AND m.is_deleted = 0
           ORDER BY m.id DESC LIMIT 1""",
        (cid,)
    )
    last = await cursor.fetchone()
    if last:
        plain = decrypt_message(last["encrypted_content"])
        summary["last_message_id"] = last["id"]
        summary["last_message_preview"] = _preview_for(plain, last["file_type"], last["file_name"])
        summary["last_message_ts"] = last["timestamp_ms"]
        summary["last_sender_id"] = last["sender_id"]
        summary["last_sender_name"] = last["display_name"] or last["username"]
        summary["last_message_type"] = last["file_type"] or "text"

    # Unread = messages from other people past the point the user last read to.
    # Pending invites do not count as unread messages.
    if summary["is_pending"]:
        summary["unread_count"] = 0
    else:
        cursor = await db.execute(
            """SELECT COUNT(*) AS cnt FROM messages m
               WHERE m.conversation_id = ? AND m.sender_id != ? AND m.is_deleted = 0
                 AND m.id > (SELECT COALESCE(last_read_message_id, 0)
                             FROM conversation_members
                             WHERE conversation_id = ? AND user_id = ?)""",
            (cid, uid, cid, uid)
        )
        unread = await cursor.fetchone()
        summary["unread_count"] = unread["cnt"] or 0
    return summary


async def _conversations_for_user(db: aiosqlite.Connection, uid: int, online_ids=None) -> List[dict]:
    """Global lobby first, then the user's private chats + groups newest-activity first.

    Pending group invites are included so clients can accept/reject them. Active
    blocks hide the conversation entirely (but leave the underlying rows intact)."""
    cursor = await db.execute(
        """SELECT c.id FROM conversations c
           WHERE c.id = ?
              OR EXISTS (
                     SELECT 1 FROM conversation_members cm
                     WHERE cm.conversation_id = c.id AND cm.user_id = ?
                       AND cm.status IN ('accepted', 'pending'))
           ORDER BY c.id""",
        (GLOBAL_CONVERSATION_ID, uid)
    )
    ids = [r["id"] for r in await cursor.fetchall()]
    summaries = []
    for cid in ids:
        try:
            # Hide chats when either user has blocked the other; never delete them.
            if _conversation_blocked is not None and cid != GLOBAL_CONVERSATION_ID \
                    and await _conversation_blocked(db, cid, uid):
                continue
            summaries.append(await _build_conversation_summary(db, cid, uid, online_ids))
        except HTTPException:
            continue

    def sort_key(s):
        if s["id"] == GLOBAL_CONVERSATION_ID:
            return (0, 0)
        # Pending invites float to the top so they are easy to accept.
        if s.get("is_pending"):
            return (0, 1)
        return (1, -(s["last_message_ts"] or 0))

    return sorted(summaries, key=sort_key)


# ------------------------------------------------------------------------
# Message serialization
# ------------------------------------------------------------------------
async def _attach_receipt_info(db: aiosqlite.Connection, msgs: List[dict], uid: int):
    """
    For the current user's own messages, attach who has read them
    (readers + reader_count). Used by the clickable read-receipt tick.
    """
    own = [m["id"] for m in msgs if m["sender_id"] == uid]
    if not own:
        return msgs
    placeholders = ",".join("?" * len(own))
    reader_map: Dict[int, List[dict]] = {mid: [] for mid in own}
    count_map: Dict[int, int] = {mid: 0 for mid in own}

    cursor = await db.execute(
        f"""SELECT r.message_id, r.read_at, u.id, u.username, u.display_name, u.avatar_path
            FROM read_receipts r JOIN users u ON u.id = r.user_id
            WHERE r.message_id IN ({placeholders})
            ORDER BY r.read_at ASC""",
        tuple(own)
    )
    for row in await cursor.fetchall():
        reader_map.setdefault(row["message_id"], []).append({
            "user_id": row["id"],
            "username": row["username"],
            "display_name": row["display_name"],
            "avatar_path": row["avatar_path"],
            "read_at": row["read_at"],
        })
        count_map[row["message_id"]] = count_map.get(row["message_id"], 0) + 1

    cursor = await db.execute(
        f"""SELECT message_id, COUNT(*) AS cnt FROM read_receipts
            WHERE message_id IN ({placeholders}) GROUP BY message_id""",
        tuple(own)
    )
    for row in await cursor.fetchall():
        count_map[row["message_id"]] = row["cnt"]

    for m in msgs:
        if m["sender_id"] == uid:
            m["readers"] = reader_map.get(m["id"], [])[:12]
            m["reader_count"] = count_map.get(m["id"], 0)
            m["status"] = "read" if m["reader_count"] > 0 else "sent"
        else:
            m["readers"] = []
            m["reader_count"] = 0
    return msgs


def _row_to_message(row, content: str) -> dict:
    return {
        "id": row["id"],
        "conversation_id": row["conversation_id"],
        "sender_id": row["sender_id"],
        "sender_username": row["username"],
        "sender_display_name": row["display_name"],
        "sender_avatar_path": row["avatar_path"],
        "content": content,
        "timestamp_ms": row["timestamp_ms"],
        "reply_to_id": row["reply_to_id"],
        "is_edited": bool(row["is_edited"]),
        "is_deleted": False,
        "file_path": row["file_path"],
        "file_type": row["file_type"],
        "file_name": row["file_name"],
        "file_size": row["file_size"],
        "status": "sent",
        "readers": [],
        "reader_count": 0,
    }


# ------------------------------------------------------------------------
# HTTP Endpoints
# ------------------------------------------------------------------------
@app.get("/api/health")
async def health_check():
    return {"status": "healthy", "timestamp": int(time.time()), "version": APP_VERSION}


@app.post("/api/auth/signup")
async def signup(
    username: str = Query(..., min_length=3, max_length=30),
    password: str = Query(..., min_length=6),
    display_name: str = Query(None, max_length=50)
):
    if not username.replace('_', '').isalnum():
        raise HTTPException(400, "Invalid username - only letters, numbers and underscores allowed")
    db = await get_db()
    try:
        async with db.execute("SELECT id FROM users WHERE username = ?", (username.lower(),)) as cursor:
            if await cursor.fetchone():
                raise HTTPException(409, "Username taken")
        pwd_hash, salt = hash_password(password)
        token = generate_token()
        display = display_name or username
        cursor = await db.execute(
            "INSERT INTO users (username, display_name, password_hash, salt, token, status) "
            "VALUES (?,?,?,?,?,'online')",
            (username.lower(), display, pwd_hash, salt, token)
        )
        new_uid = cursor.lastrowid
        await _ensure_global_member(db, new_uid)
        await db.commit()
        schedule_db_sync()
        return {
            "token": token,
            "user": {
                "id": new_uid,
                "username": username.lower(),
                "display_name": display,
                "avatar_path": None,
                "status": "online"
            }
        }
    finally:
        await db.close()


@app.post("/api/auth/login")
async def login(username: str = Query(...), password: str = Query(...)):
    db = await get_db()
    try:
        async with db.execute("SELECT * FROM users WHERE username = ?", (username.lower(),)) as cursor:
            user = await cursor.fetchone()
        if not user or not verify_password(password, user['salt'], user['password_hash']):
            raise HTTPException(401, "Invalid credentials")
        token = generate_token()
        await db.execute(
            "UPDATE users SET token = ?, last_seen = ?, status = 'online' WHERE id = ?",
            (token, int(time.time()), user['id'])
        )
        await _ensure_global_member(db, user['id'])
        await db.commit()
        schedule_db_sync()
        return {
            "token": token,
            "user": {
                "id": user['id'],
                "username": user['username'],
                "display_name": user['display_name'],
                "avatar_path": user['avatar_path'],
                "status": "online"
            }
        }
    finally:
        await db.close()


@app.get("/api/auth/verify")
async def verify_token(token: str = Header(..., alias="X-Auth-Token")):
    user = await authenticate_user(token)
    if not user:
        raise HTTPException(401)
    return {"user": user}


@app.post("/api/auth/logout")
async def logout(token: str = Header(..., alias="X-Auth-Token")):
    db = await get_db()
    try:
        cursor = await db.execute("SELECT id FROM users WHERE token = ?", (token,))
        row = await cursor.fetchone()
        await db.execute(
            "UPDATE users SET token = NULL, status = 'offline', last_seen = ? WHERE token = ?",
            (int(time.time()), token)
        )
        await db.commit()
        schedule_db_sync()
        if row:
            # a logged-out device/tab must not keep receiving messages
            await manager.force_close_user(row["id"])
        return {"status": "logged_out"}
    finally:
        await db.close()


# ------------------------------------------------------------------------
# Profile
# ------------------------------------------------------------------------
@app.get("/api/profile")
async def get_profile(token: str = Header(..., alias="X-Auth-Token")):
    user = await authenticate_user(token)
    if not user:
        raise HTTPException(401)
    return {"user": user}


@app.patch("/api/profile")
async def update_profile(
    display_name: str = Query(..., max_length=50),
    token: str = Header(..., alias="X-Auth-Token")
):
    user = await authenticate_user(token)
    if not user:
        raise HTTPException(401)
    db = await get_db()
    try:
        updated_name = display_name.strip() or user['username']
        await db.execute(
            "UPDATE users SET display_name = ? WHERE id = ?",
            (updated_name, user['id'])
        )
        await db.commit()
        schedule_db_sync()
        user['display_name'] = updated_name
        # Live presence uses in-memory state; keep the online-users list fresh
        manager.update_profile(user['id'], display_name=updated_name, avatar_path=user.get('avatar_path'))
        await manager.broadcast({
            "type": "profile_updated",
            "user_id": user['id'],
            "username": user['username'],
            "display_name": updated_name,
            "avatar_path": user.get('avatar_path'),
        })
        return {"user": user}
    finally:
        await db.close()


@app.post("/api/profile/password")
async def change_password(
    current_password: str = Query(...),
    new_password: str = Query(..., min_length=6, max_length=200),
    token: str = Header(..., alias="X-Auth-Token")
):
    user = await authenticate_user(token)
    if not user:
        raise HTTPException(401)
    db = await get_db()
    try:
        async with db.execute(
            "SELECT password_hash, salt FROM users WHERE id = ?", (user['id'],)
        ) as cursor:
            row = await cursor.fetchone()
        if not row or not verify_password(current_password, row['salt'], row['password_hash']):
            raise HTTPException(400, "Current password is incorrect")
        pwd_hash, salt = hash_password(new_password)
        await db.execute(
            "UPDATE users SET password_hash = ?, salt = ? WHERE id = ?",
            (pwd_hash, salt, user['id'])
        )
        await db.commit()
        schedule_db_sync()
        return {"status": "ok"}
    finally:
        await db.close()


_MAGIC_TO_TYPE = [
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),
    (b"\x00\x00\x01\x00", "image/x-icon"),
    (b"BM", "image/bmp"),
]


def sniff_image_type(data: bytes) -> Optional[str]:
    for magic, mime in _MAGIC_TO_TYPE:
        if data.startswith(magic):
            if mime == "image/webp" and len(data) > 12:
                if data[8:12] != b"WEBP":
                    continue
            return mime
    return None


@app.post("/api/profile/avatar")
async def upload_avatar(
    file: UploadFile = File(...),
    token: str = Header(..., alias="X-Auth-Token")
):
    user = await authenticate_user(token)
    if not user:
        raise HTTPException(401)

    # Write to temp file on disk first, not RAM
    with tempfile.NamedTemporaryFile(delete=False, suffix=".img", dir=TEMP_DIR) as tmp:
        tmp_path = tmp.name
        size = 0
        head = b""
        chunk = await file.read(65536)
        while chunk:
            if size < 32:
                head += chunk[:32]
            size += len(chunk)
            if size > 5 * 1024 * 1024:
                os.unlink(tmp_path)
                raise HTTPException(400, "Image too large (max 5MB)")
            tmp.write(chunk)
            chunk = await file.read(65536)

    # Only actual image bytes are accepted (magic-number sniffing)
    detected = sniff_image_type(head) if head else None
    if not detected:
        os.unlink(tmp_path)
        raise HTTPException(400, "Invalid image file - must be PNG, JPEG, GIF, WebP or BMP")

    try:
        with open(tmp_path, 'rb') as f:
            data = f.read()
        ext = {".jpg": "", ".png": "", ".gif": "", ".webp": "", ".ico": "", ".bmp": ""}.get(
            os.path.splitext(file.filename or "")[1].lower(), "")
        if detected == "image/jpeg":
            ext = ".jpg"
        elif detected == "image/png":
            ext = ".png"
        elif detected == "image/gif":
            ext = ".gif"
        elif detected == "image/webp":
            ext = ".webp"
        remote_path = f"avatars/{user['username']}_{uuid.uuid4().hex}{ext}"
        await asyncio.to_thread(store_file, remote_path, data)
    finally:
        os.unlink(tmp_path)

    db = await get_db()
    try:
        await db.execute(
            "UPDATE users SET avatar_path = ? WHERE id = ?",
            (remote_path, user['id'])
        )
        await db.commit()
        schedule_db_sync()
    finally:
        await db.close()

    # Live presence uses in-memory state; keep the online-users list fresh
    manager.update_profile(user['id'], avatar_path=remote_path)
    await manager.broadcast({
        "type": "profile_updated",
        "user_id": user['id'],
        "username": user['username'],
        "display_name": user['display_name'],
        "avatar_path": remote_path,
    })
    return {"avatar_path": remote_path}


# ------------------------------------------------------------------------
# Conversations (REST fallbacks - primary channel is WebSocket)
# ------------------------------------------------------------------------
@app.get("/api/conversations")
async def list_conversations(token: str = Header(..., alias="X-Auth-Token")):
    user = await authenticate_user(token)
    if not user:
        raise HTTPException(401)
    db = await get_db()
    try:
        # REST fallback must seed the global watermark too (it normally happens
        # on first WS connect / signup), otherwise unread counts would be wrong.
        await _ensure_global_member(db, user['id'])
        await db.commit()
        online = {u["id"] for u in manager.get_online_users()}
        return {"conversations": await _conversations_for_user(db, user['id'], online)}
    finally:
        await db.close()


_dm_create_lock = asyncio.Lock()


async def _create_dm(db: aiosqlite.Connection, uid: int, target_id: int,
                     manager_ref=None) -> Optional[dict]:
    """Create a private chat. Returns summary or None if invalid/at limit.

    Up to MAX_PRIVATE_CHATS_PER_PAIR private chats are allowed per pair of users.
    Blocked users cannot create or be added to a private chat; the global room
    remains the only shared conversation.
    """
    if target_id == uid:
        raise HTTPException(400, "You can't start a private chat with yourself")

    async with _dm_create_lock:
        cursor = await db.execute(
            "SELECT id, username, display_name FROM users WHERE id = ?", (target_id,)
        )
        target = await cursor.fetchone()
        if not target:
            raise HTTPException(404, "User not found")
        if await _block_exists(db, uid, target_id):
            raise HTTPException(403, "You can't start a private chat with this user")

        low, high = sorted([uid, target_id])
        cursor = await db.execute(
            """SELECT COUNT(*) AS cnt FROM conversations
               WHERE type = 'dm' AND is_group = 0 AND user_low_id = ? AND user_high_id = ?""",
            (low, high)
        )
        existing = (await cursor.fetchone())["cnt"]

        if existing >= MAX_PRIVATE_CHATS_PER_PAIR:
            raise HTTPException(
                400,
                f"You already have {MAX_PRIVATE_CHATS_PER_PAIR} private chats with "
                f"{target['username']} - delete one to start another"
            )

        cursor = await db.execute(
            """INSERT INTO conversations (type, title, created_by, user_low_id, user_high_id, is_group)
               VALUES ('dm', '', ?, ?, ?, 0)""",
            (uid, low, high)
        )
        cid = cursor.lastrowid
        now = int(time.time())
        await db.executemany(
            "INSERT OR IGNORE INTO conversation_members "
            "(conversation_id, user_id, joined_at, status, role) VALUES (?, ?, ?, 'accepted', 'member')",
            [(cid, uid, now), (cid, target_id, now)]
        )
        await db.commit()
        schedule_db_sync()

        online = {u["id"] for u in manager.get_online_users()} if manager_ref else set()
        return await _build_conversation_summary(db, cid, uid, online)


@app.post("/api/conversations/dm")
async def create_dm_rest(
    user_id: int = Query(...),
    token: str = Header(..., alias="X-Auth-Token")
):
    user = await authenticate_user(token)
    if not user:
        raise HTTPException(401)
    db = await get_db()
    try:
        summary = await _create_dm(db, user['id'], user_id, manager)
        if summary is None:
            raise HTTPException(400, "Could not create private chat")
        # Live-push the new conversation to both members (sidebar updates)
        online = {u["id"] for u in manager.get_online_users()}
        await manager.send_to_user(user['id'], {"type": "dm_created", "conversation": summary})
        peer = summary.get("peer") or {}
        if peer.get("id"):
            peer_summary = await _build_conversation_summary(db, summary["id"], peer["id"], online)
            await manager.send_to_user(peer["id"], {"type": "dm_created", "conversation": peer_summary})
        return {"conversation": summary}
    finally:
        await db.close()


_group_create_lock = asyncio.Lock()
_MAX_GROUP_NAME = 60


async def _create_group(db: aiosqlite.Connection, creator_id: int, name: str,
                        member_ids: List[int]) -> dict:
    if not name or not name.strip():
        raise HTTPException(400, "Group name is required")
    name = name.strip()[:_MAX_GROUP_NAME]
    member_ids = sorted({int(m) for m in member_ids if int(m) != creator_id})
    if not member_ids:
        raise HTTPException(400, "Select at least one member")
    if len(member_ids) > 50:
        raise HTTPException(400, "A group can have at most 50 members")

    placeholders = ",".join("?" * len(member_ids))
    cursor = await db.execute(
        f"SELECT id, username, display_name FROM users WHERE id IN ({placeholders})",
        member_ids
    )
    users = {r["id"]: r for r in await cursor.fetchall()}
    for mid in member_ids:
        if mid not in users:
            raise HTTPException(404, "Could not find a member")
        if await _block_exists(db, creator_id, mid):
            raise HTTPException(403, f"You can't include a blocked user: {users[mid]['username']}")
    for i in range(len(member_ids)):
        for j in range(i + 1, len(member_ids)):
            if await _block_exists(db, member_ids[i], member_ids[j]):
                raise HTTPException(
                    403,
                    f"{users[member_ids[i]]['username']} and {users[member_ids[j]]['username']} "
                    "have a block; they cannot be in the same group"
                )

    async with _group_create_lock:
        cursor = await db.execute(
            """INSERT INTO conversations (type, title, created_by, is_group)
               VALUES ('dm', ?, ?, 1)""",
            (name, creator_id)
        )
        cid = cursor.lastrowid
        now = int(time.time())
        await db.execute(
            "INSERT INTO conversation_members "
            "(conversation_id, user_id, joined_at, status, role) VALUES (?, ?, ?, 'accepted', 'owner')",
            (cid, creator_id, now)
        )
        await db.executemany(
            "INSERT OR IGNORE INTO conversation_members "
            "(conversation_id, user_id, joined_at, status, role) VALUES (?, ?, ?, 'pending', 'member')",
            [(cid, mid, now) for mid in member_ids]
        )
        await db.commit()
        schedule_db_sync()

    online = {u["id"] for u in manager.get_online_users()}
    summary = await _build_conversation_summary(db, cid, creator_id, online)

    # Invitees see a pending conversation so they can accept or decline.
    for mid in member_ids:
        invite = await _build_conversation_summary(db, cid, mid, online)
        await manager.send_to_user(mid, {"type": "conversation_updated", "conversation": invite})
    return summary


@app.post("/api/conversations/group")
async def create_group_rest(
    name: str = Query(..., max_length=_MAX_GROUP_NAME),
    member_ids: str = Query(...),
    token: str = Header(..., alias="X-Auth-Token")
):
    user = await authenticate_user(token)
    if not user:
        raise HTTPException(401)
    ids = [int(x) for x in member_ids.split(",") if x.strip()]
    db = await get_db()
    try:
        summary = await _create_group(db, user['id'], name, ids)
        await manager.send_to_user(user['id'], {"type": "dm_created", "conversation": summary})
        return {"conversation": summary}
    finally:
        await db.close()


async def _accept_conversation_invite(db: aiosqlite.Connection, uid: int, cid: int) -> dict:
    member = await _membership(db, cid, uid)
    if not member or member.get("status") != "pending":
        raise HTTPException(404, "You don't have a pending invite to this group")
    if await _conversation_blocked(db, cid, uid):
        raise HTTPException(403, "This conversation is hidden because of a block")
    await db.execute(
        "UPDATE conversation_members SET status = 'accepted' WHERE conversation_id = ? AND user_id = ?",
        (cid, uid)
    )
    await db.commit()
    schedule_db_sync()
    return await _build_conversation_summary(db, cid, uid, {u["id"] for u in manager.get_online_users()})


async def _reject_conversation_invite(db: aiosqlite.Connection, uid: int, cid: int) -> bool:
    member = await _membership(db, cid, uid)
    if not member or member.get("status") != "pending":
        raise HTTPException(404, "You don't have a pending invite to this group")
    await db.execute("DELETE FROM conversation_members WHERE conversation_id = ? AND user_id = ?",
                     (cid, uid))
    await db.commit()
    schedule_db_sync()
    return True


@app.post("/api/conversations/{conversation_id}/accept")
async def accept_conversation_invite_rest(
    conversation_id: int,
    token: str = Header(..., alias="X-Auth-Token")
):
    user = await authenticate_user(token)
    if not user:
        raise HTTPException(401)
    db = await get_db()
    try:
        summary = await _accept_conversation_invite(db, user['id'], conversation_id)
        online = {u["id"] for u in manager.get_online_users()}
        members = await _group_update_recipient_ids(db, conversation_id)
        for mid in members:
            await manager.send_to_user(mid, {"type": "conversation_updated",
                                             "conversation": await _build_conversation_summary(
                                                 db, conversation_id, mid, online)})
        return {"conversation": summary}
    finally:
        await db.close()


@app.post("/api/conversations/{conversation_id}/reject")
async def reject_conversation_invite_rest(
    conversation_id: int,
    token: str = Header(..., alias="X-Auth-Token")
):
    user = await authenticate_user(token)
    if not user:
        raise HTTPException(401)
    db = await get_db()
    try:
        await _reject_conversation_invite(db, user['id'], conversation_id)
        return {"status": "rejected"}
    finally:
        await db.close()


@app.post("/api/conversations/{conversation_id}/members")
async def add_group_members_rest(
    conversation_id: int,
    member_ids: str = Query(...),
    token: str = Header(..., alias="X-Auth-Token")
):
    user = await authenticate_user(token)
    if not user:
        raise HTTPException(401)
    db = await get_db()
    try:
        member = await _membership(db, conversation_id, user['id'])
        conv = await db.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,))
        row = await conv.fetchone()
        if not row or not row["is_group"]:
            raise HTTPException(400, "Only groups can have members added")
        if not member or member.get("status") != "accepted":
            raise HTTPException(403, "Accept the group invite first")
        if not await user_in_conversation(db, user['id'], conversation_id):
            raise HTTPException(403, "You can no longer access this conversation")

        ids = [int(x) for x in member_ids.split(",") if x.strip()]
        existing = await _raw_conversation_member_ids(db, conversation_id)
        new_ids = [mid for mid in ids if mid != user['id']]
        if len(existing) + len(set(new_ids)) > 50:
            raise HTTPException(400, "A group can have at most 50 members")
        now = int(time.time())
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                if await _block_exists(db, ids[i], ids[j]):
                    raise HTTPException(403, "Selected users have a block between them")
        for mid in ids:
            if not mid or mid == user['id']:
                continue
            if await _block_exists(db, user['id'], mid):
                raise HTTPException(403, "You can't add a blocked user")
            for existing_id in existing:
                if await _block_exists(db, mid, existing_id):
                    raise HTTPException(
                        403, "That user has a block with someone already in this group")
            await db.execute(
                "INSERT OR IGNORE INTO conversation_members "
                "(conversation_id, user_id, joined_at, status, role) VALUES (?, ?, ?, 'pending', 'member')",
                (conversation_id, mid, now)
            )
        await db.commit()
        schedule_db_sync()
        online = {u["id"] for u in manager.get_online_users()}
        summary = await _build_conversation_summary(db, conversation_id, user['id'], online)
        members = await _group_update_recipient_ids(db, conversation_id)
        for mid in ids:
            if mid == user['id'] or await _conversation_blocked(db, conversation_id, mid):
                continue
            invite = await _build_conversation_summary(db, conversation_id, mid, online)
            await manager.send_to_user(mid, {"type": "conversation_updated", "conversation": invite})
        for mid in members:
            await manager.send_to_user(mid, {"type": "conversation_updated",
                                             "conversation": await _build_conversation_summary(
                                                 db, conversation_id, mid, online)})
        return {"conversation": summary}
    finally:
        await db.close()


@app.patch("/api/conversations/{conversation_id}")
async def rename_conversation_rest(
    conversation_id: int,
    name: str = Query(..., max_length=_MAX_GROUP_NAME),
    token: str = Header(..., alias="X-Auth-Token")
):
    user = await authenticate_user(token)
    if not user:
        raise HTTPException(401)
    name = name.strip()
    if not name:
        raise HTTPException(400, "Name cannot be empty")
    db = await get_db()
    try:
        if not await user_in_conversation(db, user['id'], conversation_id):
            raise HTTPException(403, "You can't rename this conversation")
        await db.execute(
            "UPDATE conversations SET custom_name = ?, title = ? WHERE id = ?",
            (name, name, conversation_id)
        )
        await db.commit()
        schedule_db_sync()
        online = {u["id"] for u in manager.get_online_users()}
        summary = await _build_conversation_summary(db, conversation_id, user['id'], online)
        members = await _group_update_recipient_ids(db, conversation_id)
        for mid in members:
            await manager.send_to_user(mid, {"type": "conversation_updated",
                                             "conversation": await _build_conversation_summary(
                                                 db, conversation_id, mid, online)})
        return {"conversation": summary}
    finally:
        await db.close()


@app.delete("/api/conversations/{conversation_id}")
async def delete_or_leave_conversation_rest(
    conversation_id: int,
    token: str = Header(..., alias="X-Auth-Token")
):
    user = await authenticate_user(token)
    if not user:
        raise HTTPException(401)
    db = await get_db()
    try:
        member = await _membership(db, conversation_id, user['id'])
        cursor = await db.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,))
        conv = await cursor.fetchone()
        if not conv:
            raise HTTPException(404, "Conversation not found")
        if conv["is_group"] != 1:
            raise HTTPException(400, "Only group chats can be left or deleted")
        if not member or member.get("status") != "accepted":
            raise HTTPException(403, "You are not a member of this group")

        notify_ids = await _group_update_recipient_ids(db, conversation_id)
        # Owner deletes the group permanently; members leave the group.
        if member and member.get("role") == "owner" and member.get("status") == "accepted":
            await db.execute("DELETE FROM read_receipts WHERE message_id IN "
                             "(SELECT id FROM messages WHERE conversation_id = ?)", (conversation_id,))
            await db.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
            await db.execute("DELETE FROM conversation_members WHERE conversation_id = ?", (conversation_id,))
            await db.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
            await db.commit()
            schedule_db_sync()
            for mid in notify_ids:
                await manager.send_to_user(mid, {"type": "conversation_deleted",
                                                 "conversation_id": conversation_id})
            return {"status": "deleted", "conversation_id": conversation_id}

        # Non-owner: leave (removes membership, keeps the group + history).
        await db.execute("DELETE FROM conversation_members WHERE conversation_id = ? AND user_id = ?",
                         (conversation_id, user['id']))
        await db.commit()
        schedule_db_sync()
        # Only notify the members who remain in the group.
        remaining = await conversation_member_ids(db, conversation_id)
        for mid in remaining:
            await manager.send_to_user(mid, {"type": "conversation_left",
                                             "conversation_id": conversation_id,
                                             "user_id": user['id'],
                                             "conversation": await _build_conversation_summary(
                                                 db, conversation_id, mid,
                                                 {u["id"] for u in manager.get_online_users()})})
        return {"status": "left", "conversation_id": conversation_id}
    finally:
        await db.close()


# ------------------------------------------------------------------------
# Users: search + blocking
# ------------------------------------------------------------------------
@app.get("/api/users")
async def list_users(
    query: str = Query("", max_length=100),
    token: str = Header(..., alias="X-Auth-Token")
):
    user = await authenticate_user(token)
    if not user:
        raise HTTPException(401)
    q = query.strip().lower()
    db = await get_db()
    try:
        if q:
            cursor = await db.execute(
                """SELECT id, username, display_name, avatar_path, status
                   FROM users WHERE id != ? AND (lower(username) LIKE ? OR lower(display_name) LIKE ?)
                   ORDER BY CASE WHEN status = 'online' THEN 0 ELSE 1 END, username LIMIT 100""",
                (user['id'], f"%{q}%", f"%{q}%")
            )
        else:
            cursor = await db.execute(
                """SELECT id, username, display_name, avatar_path, status FROM users
                   WHERE id != ? ORDER BY CASE WHEN status = 'online' THEN 0 ELSE 1 END, username LIMIT 100""",
                (user['id'],)
            )
        out = []
        for r in await cursor.fetchall():
            out.append({
                "id": r["id"],
                "username": r["username"],
                "display_name": r["display_name"],
                "avatar_path": r["avatar_path"],
                "online": r["status"] == "online" and manager.is_online(r["id"]),
                "blocked": await _block_exists(db, user['id'], r["id"]),
            })
        return {"users": out}
    finally:
        await db.close()


@app.get("/api/users/blocked")
async def blocked_users(token: str = Header(..., alias="X-Auth-Token")):
    user = await authenticate_user(token)
    if not user:
        raise HTTPException(401)
    db = await get_db()
    try:
        cursor = await db.execute(
            """SELECT u.id, u.username, u.display_name, u.avatar_path, b.created_at
               FROM user_blocks b JOIN users u ON u.id = b.blocked_id
               WHERE b.blocker_id = ? ORDER BY b.created_at DESC""",
            (user['id'],)
        )
        return {"users": [dict(r) for r in await cursor.fetchall()]}
    finally:
        await db.close()


@app.post("/api/users/{user_id}/block")
async def block_user_rest(user_id: int, token: str = Header(..., alias="X-Auth-Token")):
    user = await authenticate_user(token)
    if not user:
        raise HTTPException(401)
    if user_id == user['id']:
        raise HTTPException(400, "You can't block yourself")
    db = await get_db()
    try:
        cursor = await db.execute("SELECT id FROM users WHERE id = ?", (user_id,))
        if not await cursor.fetchone():
            raise HTTPException(404, "User not found")
        await db.execute(
            "INSERT OR IGNORE INTO user_blocks (blocker_id, blocked_id) VALUES (?, ?)",
            (user['id'], user_id)
        )
        await db.commit()
        schedule_db_sync()
        await manager.send_to_user(user_id, {
            "type": "block_changed",
            "blocker_id": user['id'],
            "blocked": True,
        })
        return {"status": "blocked", "user_id": user_id}
    finally:
        await db.close()


@app.delete("/api/users/{user_id}/block")
async def unblock_user_rest(user_id: int, token: str = Header(..., alias="X-Auth-Token")):
    user = await authenticate_user(token)
    if not user:
        raise HTTPException(401)
    db = await get_db()
    try:
        await db.execute(
            "DELETE FROM user_blocks WHERE blocker_id = ? AND blocked_id = ?",
            (user['id'], user_id)
        )
        await db.commit()
        schedule_db_sync()
        await manager.send_to_user(user_id, {
            "type": "block_changed",
            "blocker_id": user['id'],
            "blocked": False,
        })
        return {"status": "unblocked", "user_id": user_id}
    finally:
        await db.close()


# ------------------------------------------------------------------------
# Messages (REST fallbacks for reliable edit/delete/receipts)
# ------------------------------------------------------------------------
async def _get_message_row(db: aiosqlite.Connection, mid: int):
    cursor = await db.execute(
        """SELECT m.*, u.username, u.display_name, u.avatar_path
           FROM messages m JOIN users u ON u.id = m.sender_id
           WHERE m.id = ?""",
        (mid,)
    )
    return await cursor.fetchone()


@app.patch("/api/messages/{message_id}")
async def edit_message_rest(
    message_id: int,
    content: str = Query(..., max_length=MAX_MESSAGE_LENGTH),
    token: str = Header(..., alias="X-Auth-Token")
):
    user = await authenticate_user(token)
    if not user:
        raise HTTPException(401)
    content = content.strip()
    if not content:
        raise HTTPException(400, "Message cannot be empty")
    db = await get_db()
    try:
        row = await _get_message_row(db, message_id)
        if not row or row["sender_id"] != user['id'] or row["is_deleted"]:
            raise HTTPException(404, "Message not found")
        if not await user_in_conversation(db, user['id'], row["conversation_id"]):
            raise HTTPException(403, "You can no longer access this conversation")
        new_enc = encrypt_message(content)
        await db.execute(
            "UPDATE messages SET encrypted_content = ?, is_edited = 1 WHERE id = ?",
            (new_enc, message_id)
        )
        await db.commit()
        schedule_db_sync()
        await _broadcast_to_conversation(db, row["conversation_id"], {
            "type": "message_edited",
            "message_id": message_id,
            "conversation_id": row["conversation_id"],
            "content": content,
            "editor_id": user['id'],
        }, exclude_user_id=None)   # include the author's other tabs
        members = await conversation_member_ids(db, row["conversation_id"])
        await _push_conversation_updates(user['id'], row["conversation_id"],
                                         [m for m in members if m != user['id']])
        return {"status": "ok", "message_id": message_id, "content": content}
    finally:
        await db.close()


@app.delete("/api/messages/{message_id}")
async def delete_message_rest(
    message_id: int,
    token: str = Header(..., alias="X-Auth-Token")
):
    user = await authenticate_user(token)
    if not user:
        raise HTTPException(401)
    db = await get_db()
    try:
        row = await _get_message_row(db, message_id)
        if not row:
            # Idempotent: an already-removed message should not make the
            # client keep showing "delete failed" when a resend/reconnect
            # replays the request.
            return {"status": "already_deleted", "message_id": message_id}
        if row["sender_id"] != user['id'] or row["is_deleted"]:
            raise HTTPException(404, "Message not found")
        if not await user_in_conversation(db, user['id'], row["conversation_id"]):
            raise HTTPException(403, "You can no longer access this conversation")

        if row["file_path"]:
            try:
                await asyncio.to_thread(delete_file, row["file_path"])
            except Exception as e:
                logger.warning(f"Could not delete remote file {row['file_path']}: {e}")

        await db.execute("DELETE FROM messages WHERE id = ?", (message_id,))
        await db.execute("DELETE FROM read_receipts WHERE message_id = ?", (message_id,))
        await db.commit()
        schedule_db_sync()
        await _broadcast_to_conversation(db, row["conversation_id"], {
            "type": "message_deleted",
            "message_id": message_id,
            "conversation_id": row["conversation_id"],
            "deleted_by": user['id'],
        }, exclude_user_id=None)   # everyone gets the event; sender removes locally
        members = await conversation_member_ids(db, row["conversation_id"])
        await _push_conversation_updates(user['id'], row["conversation_id"],
                                         [m for m in members if m != user['id']])
        return {"status": "deleted", "message_id": message_id}
    finally:
        await db.close()


@app.get("/api/messages/{message_id}/read-receipts")
async def message_read_receipts(
    message_id: int,
    token: str = Header(..., alias="X-Auth-Token")
):
    """Who read this message and when (only visible to the message author)."""
    user = await authenticate_user(token)
    if not user:
        raise HTTPException(401)
    db = await get_db()
    try:
        row = await _get_message_row(db, message_id)
        if not row or row["is_deleted"]:
            raise HTTPException(404, "Message not found")
        if row["sender_id"] != user['id']:
            raise HTTPException(403, "Only the author can view read receipts")
        if not await user_in_conversation(db, user['id'], row["conversation_id"]):
            raise HTTPException(403, "Not a member of this conversation")

        cursor = await db.execute(
            """SELECT r.read_at, u.id, u.username, u.display_name, u.avatar_path, u.status
               FROM read_receipts r JOIN users u ON u.id = r.user_id
               WHERE r.message_id = ? ORDER BY r.read_at ASC LIMIT 500""",
            (message_id,)
        )
        readers = []
        for r in await cursor.fetchall():
            readers.append({
                "user_id": r["id"],
                "username": r["username"],
                "display_name": r["display_name"],
                "avatar_path": r["avatar_path"],
                "read_at": r["read_at"],
                "online": r["status"] == "online" and manager.is_online(r["id"]),
            })

        cursor = await db.execute(
            "SELECT COUNT(*) AS cnt FROM read_receipts WHERE message_id = ?", (message_id,)
        )
        reader_total = (await cursor.fetchone())["cnt"]

        result = {
            "message_id": message_id,
            "conversation_id": row["conversation_id"],
            "readers": readers,
            "reader_count": reader_total,
            "readers_truncated": reader_total > len(readers),
            "is_dm": row["conversation_id"] != GLOBAL_CONVERSATION_ID,
            "not_read": [],
        }

        # DM: explicitly list the other person if they haven't read yet
        if result["is_dm"]:
            cursor = await db.execute(
                """SELECT u.id, u.username, u.display_name, u.avatar_path, u.status
                   FROM conversation_members cm JOIN users u ON u.id = cm.user_id
                   WHERE cm.conversation_id = ? AND u.id != ?""",
                (row["conversation_id"], user['id'])
            )
            for r in await cursor.fetchall():
                already = any(p["user_id"] == r["id"] for p in readers)
                if not already:
                    result["not_read"].append({
                        "user_id": r["id"],
                        "username": r["username"],
                        "display_name": r["display_name"],
                        "avatar_path": r["avatar_path"],
                        "online": r["status"] == "online" and manager.is_online(r["id"]),
                    })
        return result
    finally:
        await db.close()


# ------------------------------------------------------------------------
# Chunked Upload - assembles chunks on DISK not RAM
# ------------------------------------------------------------------------
upload_sessions: Dict[str, dict] = {}


@app.post("/api/upload/chunk")
async def upload_chunk(
    file: UploadFile = File(...),
    chunk_index: int = Query(...),
    total_chunks: int = Query(...),
    file_name: str = Query(...),
    file_type: str = Query(...),
    file_size: int = Query(...),
    upload_id: str = Query(...),
    token: str = Header(..., alias="X-Auth-Token")
):
    user = await authenticate_user(token)
    if not user:
        raise HTTPException(401)

    if file_size > MAX_FILE_SIZE:
        raise HTTPException(400, "File too large (max 500MB)")
    if len(file_name) > 300:
        raise HTTPException(400, "File name too long")
    if "\x00" in file_name or "/" in file_name or "\\" in file_name:
        raise HTTPException(400, "Invalid file name")

    # Sweep stale sessions (aborted uploads) so the dict can't grow forever
    if len(upload_sessions) > 64:
        stale_before = time.time() - 7200
        for sid, sess in list(upload_sessions.items()):
            if sess["created_at"] < stale_before:
                del upload_sessions[sid]

    # Create session with temp dir on disk
    if upload_id not in upload_sessions:
        session_dir = os.path.join(TEMP_DIR, f"upload_{upload_id}")
        os.makedirs(session_dir, exist_ok=True)
        upload_sessions[upload_id] = {
            "session_dir": session_dir,
            "filename": file_name,
            "file_type": file_type,
            "file_size": file_size,
            "total_chunks": total_chunks,
            "received_chunks": set(),
            "user_id": user['id'],
            "created_at": time.time()
        }

    session = upload_sessions[upload_id]
    if session['user_id'] != user['id']:
        raise HTTPException(403)
    if chunk_index < 0 or chunk_index >= total_chunks or total_chunks > 10000:
        raise HTTPException(400, "Invalid chunk parameters")
    if len(session['received_chunks']) >= total_chunks:
        raise HTTPException(400, "Upload already complete")

    # Write chunk directly to disk
    chunk_path = os.path.join(session['session_dir'], f"chunk_{chunk_index:06d}")
    with open(chunk_path, 'wb') as f:
        data = await file.read(65536)
        while data:
            f.write(data)
            data = await file.read(65536)

    session['received_chunks'].add(chunk_index)

    if len(session['received_chunks']) == total_chunks:
        remote_path = f"uploads/{user['username']}/{uuid.uuid4().hex}/{file_name}"
        session_dir = session['session_dir']

        try:
            assembled_path = os.path.join(session_dir, "assembled")
            with open(assembled_path, 'wb') as out_f:
                for i in range(total_chunks):
                    chunk_file = os.path.join(session_dir, f"chunk_{i:06d}")
                    with open(chunk_file, 'rb') as in_f:
                        buf = in_f.read(1024 * 1024)
                        while buf:
                            out_f.write(buf)
                            buf = in_f.read(1024 * 1024)
                    os.unlink(chunk_file)

            with open(assembled_path, 'rb') as f:
                full_data = f.read()
            # Blocking bucket write happens off the event loop
            await asyncio.to_thread(store_file, remote_path, full_data)
            del full_data

        finally:
            shutil.rmtree(session_dir, ignore_errors=True)
            del upload_sessions[upload_id]

        return {
            "status": "complete",
            "file_path": remote_path,
            "file_name": file_name,
            "file_type": file_type,
            "file_size": file_size
        }

    return {
        "status": "in_progress",
        "received": len(session['received_chunks']),
        "total": total_chunks
    }


# ------------------------------------------------------------------------
# File Download - streams from bucket to client in chunks
# ------------------------------------------------------------------------
@app.get("/api/download/{file_path:path}")
async def download_file(
    file_path: str,
    token: str = Header(None, alias="X-Auth-Token"),
    t: str = Query(None)
):
    auth_token = token or t
    user = await authenticate_user(auth_token)
    if not user:
        raise HTTPException(401)
    if '..' in file_path or '\\' in file_path:
        raise HTTPException(400, "Invalid path")

    # Avatars are public profile data. Chat files belong to a conversation, so
    # a user blocked out of it must not be able to keep downloading the files.
    if not file_path.startswith("avatars/"):
        db = await get_db()
        try:
            cursor = await db.execute(
                "SELECT conversation_id FROM messages WHERE file_path = ? ORDER BY id DESC LIMIT 1",
                (file_path,)
            )
            row = await cursor.fetchone()
            if not row:
                raise HTTPException(404, "File not found")
            if not await user_in_conversation(db, user['id'], row["conversation_id"]):
                raise HTTPException(403, "You can no longer access this file")
        finally:
            await db.close()

    try:
        # AES-GCM needs the whole ciphertext to verify its auth tag, so one full
        # read is required for security - the response is streamed in chunks.
        data = await asyncio.to_thread(retrieve_file, file_path)
    except FileNotFoundError:
        raise HTTPException(404, "File not found")
    except Exception as e:
        logger.error(f"Download error for {file_path}: {e}")
        raise HTTPException(500, "Failed to retrieve file")

    import mimetypes
    mt = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
    file_size = len(data)

    # sanitize for the Content-Disposition header
    disp_name = os.path.basename(file_path).replace('"', "'").replace("\r", "").replace("\n", "")

    def iter_data():
        offset = 0
        while offset < len(data):
            yield data[offset:offset + DOWNLOAD_CHUNK_SIZE]
            offset += DOWNLOAD_CHUNK_SIZE

    return StreamingResponse(
        iter_data(),
        media_type=mt,
        headers={
            "Content-Length": str(file_size),
            "Cache-Control": "private, max-age=3600",
            "Content-Disposition": f'inline; filename="{disp_name}"'
        }
    )


# Lightweight per-user flood guards (in-memory only)
_send_history: Dict[int, List[float]] = {}
_typing_history: Dict[Tuple[int, int], float] = {}


def _allow_send(uid: int) -> bool:
    """Simple burst guard: max 20 sends per 5 seconds per user."""
    now = time.time()
    hist = _send_history.setdefault(uid, [])
    hist[:] = [t for t in hist if now - t < 5.0]
    if len(hist) >= 20:
        return False
    hist.append(now)
    return True


def _allow_typing(uid: int, cid: int) -> bool:
    now = time.time()
    key = (uid, cid)
    last = _typing_history.get(key, 0.0)
    if now - last < 0.8:
        return False
    _typing_history[key] = now
    return True


# ------------------------------------------------------------------------
# WebSocket Manager
# ------------------------------------------------------------------------
class ConnectionManager:
    def __init__(self):
        self.active: Dict[int, Dict[str, Any]] = {}

    async def connect(self, ws: WebSocket, user_id: int, username: str,
                      display_name: str = "", avatar_path: Optional[str] = None):
        await ws.accept()
        if user_id not in self.active:
            self.active[user_id] = {
                "username": username,
                "display_name": display_name or username,
                "avatar_path": avatar_path,
                "connections": set(),
            }
        else:
            self.active[user_id]["display_name"] = display_name or username
            self.active[user_id]["avatar_path"] = avatar_path or self.active[user_id]["avatar_path"]
        self.active[user_id]["connections"].add(ws)

    def disconnect(self, ws: WebSocket, user_id: int):
        if user_id in self.active:
            self.active[user_id]["connections"].discard(ws)
            if not self.active[user_id]["connections"]:
                del self.active[user_id]

    async def send_to_user(self, user_id: int, msg: dict):
        if user_id in self.active:
            for ws in list(self.active[user_id]["connections"]):
                try:
                    await ws.send_json(msg)
                except Exception:
                    self.disconnect(ws, user_id)

    async def send_to_users(self, user_ids: List[int], msg: dict, exclude_user_id=None):
        for uid in user_ids:
            if uid == exclude_user_id:
                continue
            await self.send_to_user(uid, msg)

    async def broadcast(self, msg: dict, exclude_user_id: Optional[int] = None):
        for uid, info in list(self.active.items()):
            if uid == exclude_user_id:
                continue
            for ws in list(info["connections"]):
                try:
                    await ws.send_json(msg)
                except Exception:
                    self.disconnect(ws, uid)

    def get_online_users(self) -> List[Dict]:
        return [
            {
                "id": uid,
                "username": info["username"],
                "display_name": info.get("display_name") or info["username"],
                "avatar_path": info.get("avatar_path"),
            }
            for uid, info in self.active.items()
        ]

    def update_profile(self, user_id: int, display_name: Optional[str] = None,
                       avatar_path: Optional[str] = None):
        """Keep the in-memory presence record in sync with profile changes."""
        if user_id not in self.active:
            return
        info = self.active[user_id]
        if display_name is not None and display_name != "":
            info["display_name"] = display_name
        if avatar_path is not None:
            info["avatar_path"] = avatar_path

    def is_online(self, user_id: int) -> bool:
        return user_id in self.active

    async def force_close_user(self, user_id: int, code: int = 4001, reason: str = ""):
        """Close every live socket of a user (e.g. after logging out)."""
        if user_id not in self.active:
            return
        for ws in list(self.active[user_id]["connections"]):
            try:
                await ws.close(code=code, reason=reason)
            except Exception:
                pass


manager = ConnectionManager()


async def _broadcast_to_conversation(db, cid: int, msg: dict, exclude_user_id=None):
    """Send a WS event to every online member of a conversation."""
    try:
        members = await conversation_member_ids(db, cid)
    except Exception as e:
        logger.error(f"Membership lookup failed for conv {cid}: {e}")
        return
    await manager.send_to_users(members, msg, exclude_user_id=exclude_user_id)


async def _push_conversation_updates(uid: int, cid: int, other_member_ids: List[int]):
    """Refreshed sidebar summaries after activity in a conversation."""
    db = await get_db()
    try:
        online = {u["id"] for u in manager.get_online_users()}
        summary = await _build_conversation_summary(db, cid, uid, online)
        await manager.send_to_user(uid, {"type": "conversation_updated", "conversation": summary})
        if other_member_ids:
            for mid in other_member_ids:
                other_summary = await _build_conversation_summary(db, cid, mid, online)
                await manager.send_to_user(mid, {"type": "conversation_updated", "conversation": other_summary})
    except Exception as e:
        logger.error(f"conversation update push failed: {e}")
    finally:
        await db.close()


# ------------------------------------------------------------------------
# WebSocket endpoint
# ------------------------------------------------------------------------
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket, token: str = Query(...)):
    user = await authenticate_user(token)
    if not user:
        await ws.close(code=4001, reason="Invalid token")
        return

    uid = user['id']
    username = user['username']
    display_name = user['display_name']
    avatar_path = user['avatar_path']

    await manager.connect(ws, uid, username, display_name, avatar_path)

    online_ids = {u["id"] for u in manager.get_online_users()}

    db = await get_db()
    try:
        await _ensure_global_member(db, uid)
        # persist "online" so REST presence fields (e.g. receipts modal) agree
        await db.execute(
            "UPDATE users SET status = 'online', last_seen = ? WHERE id = ?",
            (int(time.time()), uid)
        )
        await db.commit()
        conversations = await _conversations_for_user(db, uid, online_ids)
    finally:
        await db.close()

    await ws.send_json({
        "type": "connection_established",
        "user_id": uid,
        "username": username,
        "display_name": display_name,
        "avatar_path": avatar_path,
        "online_users": manager.get_online_users(),
        "conversations": conversations,
        "server_time": int(time.time() * 1000),
    })

    await manager.broadcast({
        "type": "user_status",
        "user_id": uid,
        "username": username,
        "display_name": display_name,
        "avatar_path": avatar_path,
        "status": "online"
    }, exclude_user_id=uid)

    try:
        while True:
            data = await ws.receive_json()
            mtype = data.get("type")

            # ---- keepalive ----
            if mtype == "ping":
                await ws.send_json({"type": "pong", "t": data.get("t")})
                continue

            # ---- create private chat ----
            if mtype == "create_dm":
                target_id = data.get("user_id")
                if not target_id:
                    continue
                db = await get_db()
                try:
                    try:
                        summary = await _create_dm(db, uid, int(target_id), manager)
                        if summary is None:
                            continue
                    except HTTPException as e:
                        await ws.send_json({
                            "type": "error", "code": "DM_FAILED",
                            "message": e.detail
                        })
                        continue
                    await ws.send_json({
                        "type": "dm_created",
                        "conversation": summary,
                    })
                    peer = summary.get("peer") or {}
                    online = {u["id"] for u in manager.get_online_users()}
                    # Send the same conversation into the other person's sidebar
                    if peer.get("id"):
                        db2 = await get_db()
                        try:
                            peer_summary = await _build_conversation_summary(
                                db2, summary["id"], peer["id"], online)
                            await manager.send_to_user(peer["id"], {
                                "type": "dm_created",
                                "conversation": peer_summary,
                            })
                        finally:
                            await db2.close()
                finally:
                    await db.close()
                continue

            # ---- request conversations ----
            if mtype == "request_conversations":
                online = {u["id"] for u in manager.get_online_users()}
                db = await get_db()
                try:
                    conversations = await _conversations_for_user(db, uid, online)
                finally:
                    await db.close()
                await ws.send_json({"type": "conversations", "conversations": conversations})
                continue

            # ---- send message ----
            if mtype == "send_message":
                if not _allow_send(uid):
                    await ws.send_json({"type": "error", "code": "RATE_LIMIT",
                                        "message": "You're sending too fast - slow down",
                                        "client_id": data.get("client_id")})
                    continue
                content = (data.get("content") or "").strip()
                conversation_id = int(data.get("conversation_id") or GLOBAL_CONVERSATION_ID)
                reply_to_id = data.get("reply_to_id")
                client_id = data.get("client_id")
                file_path = data.get("file_path")
                file_type = data.get("file_type")
                file_name = data.get("file_name")
                file_size = data.get("file_size", 0)

                if not content and not file_path:
                    await ws.send_json({"type": "error", "code": "EMPTY", "message": "Message cannot be empty",
                                        "client_id": client_id})
                    continue
                if len(content) > MAX_MESSAGE_LENGTH:
                    await ws.send_json({"type": "error", "code": "TOO_LONG", "message": "Message too long",
                                        "client_id": client_id})
                    continue

                db = await get_db()
                try:
                    if not await user_in_conversation(db, uid, conversation_id):
                        await ws.send_json({"type": "error", "code": "FORBIDDEN",
                                            "message": "You are not in this conversation",
                                            "client_id": client_id})
                        continue

                    # Idempotent insert: replaying a send after reconnect won't duplicate
                    if client_id:
                        cursor = await db.execute(
                            "SELECT id FROM messages WHERE sender_id = ? AND client_id = ?",
                            (uid, client_id)
                        )
                        existing = await cursor.fetchone()
                        if existing:
                            await ws.send_json({
                                "type": "error", "code": "DUPLICATE",
                                "message": "duplicate_message", "message_id": existing["id"],
                                "client_id": client_id
                            })
                            continue

                    encrypted = encrypt_message(content)
                    ts = int(time.time() * 1000)
                    cursor = await db.execute(
                        """INSERT INTO messages
                           (sender_id, encrypted_content, timestamp_ms, reply_to_id,
                            file_path, file_type, file_name, file_size,
                            conversation_id, client_id)
                           VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (uid, encrypted, ts, reply_to_id, file_path, file_type,
                         file_name, file_size, conversation_id, client_id or None)
                    )
                    mid = cursor.lastrowid
                    await db.commit()
                    schedule_db_sync()

                    sender = {
                        "sender_username": username,
                        "sender_display_name": display_name,
                        "sender_avatar_path": avatar_path,
                    }
                    msg_obj = {
                        "id": mid,
                        "conversation_id": conversation_id,
                        "sender_id": uid,
                        **sender,
                        "content": content,
                        "timestamp_ms": ts,
                        "reply_to_id": reply_to_id,
                        "file_path": file_path,
                        "file_type": file_type,
                        "file_name": file_name,
                        "file_size": file_size,
                        "is_edited": False,
                        "is_deleted": False,
                        "status": "sent",
                        "readers": [],
                        "reader_count": 0,
                    }

                    members = await conversation_member_ids(db, conversation_id)
                    for member_id in members:
                        # send to every connection of the member (multi-tab safe).
                        # the originator gets client_id so it can ack its pending send.
                        await manager.send_to_user(member_id, {
                            "type": "new_message",
                            "message": msg_obj,
                            "client_id": client_id if member_id == uid else None,
                        })

                    for member_id in members:
                        if member_id != uid:
                            await _push_conversation_updates(member_id, conversation_id, [])
                finally:
                    await db.close()
                continue

            # ---- load messages (cursor pagination) ----
            if mtype == "load_messages":
                conversation_id = int(data.get("conversation_id") or GLOBAL_CONVERSATION_ID)
                cursor_id = data.get("cursor")
                limit = min(int(data.get("limit", 50) or 50), 100)
                db = await get_db()
                try:
                    if not await user_in_conversation(db, uid, conversation_id):
                        await ws.send_json({"type": "error", "code": "FORBIDDEN",
                                            "message": "Not in conversation"})
                        continue
                    if cursor_id:
                        rows = await db.execute(
                            """SELECT m.*, u.username, u.display_name, u.avatar_path
                               FROM messages m JOIN users u ON m.sender_id = u.id
                               WHERE m.conversation_id = ? AND m.is_deleted = 0 AND m.id < ?
                               ORDER BY m.id DESC LIMIT ?""",
                            (conversation_id, cursor_id, limit)
                        )
                    else:
                        rows = await db.execute(
                            """SELECT m.*, u.username, u.display_name, u.avatar_path
                               FROM messages m JOIN users u ON m.sender_id = u.id
                               WHERE m.conversation_id = ? AND m.is_deleted = 0
                               ORDER BY m.id DESC LIMIT ?""",
                            (conversation_id, limit)
                        )

                    msgs = []
                    for row in reversed(await rows.fetchall()):
                        decrypted = decrypt_message(row['encrypted_content'])
                        msgs.append(_row_to_message(row, decrypted))

                    await _attach_receipt_info(db, msgs, uid)
                    next_cursor = msgs[0]['id'] if msgs else None
                    await ws.send_json({
                        "type": "messages_loaded",
                        "conversation_id": conversation_id,
                        "messages": msgs,
                        "next_cursor": next_cursor,
                        "has_more": len(msgs) == limit
                    })
                finally:
                    await db.close()
                continue

            # ---- edit (via REST normally, kept for legacy clients) ----
            if mtype == "edit_message":
                mid = data.get("message_id")
                new_content = (data.get("content") or "").strip()
                if not mid or not new_content:
                    continue
                db = await get_db()
                try:
                    async with db.execute(
                        "SELECT * FROM messages WHERE id = ? AND sender_id = ? AND is_deleted = 0",
                        (mid, uid)
                    ) as cursor:
                        row = await cursor.fetchone()
                    if not row:
                        await ws.send_json({"type": "error", "code": "NOT_FOUND",
                                            "message": "Message not found"})
                        continue
                    new_enc = encrypt_message(new_content)
                    await db.execute(
                        "UPDATE messages SET encrypted_content = ?, is_edited = 1 WHERE id = ?",
                        (new_enc, mid)
                    )
                    await db.commit()
                    schedule_db_sync()
                    await _broadcast_to_conversation(db, row['conversation_id'], {
                        "type": "message_edited",
                        "message_id": mid,
                        "conversation_id": row['conversation_id'],
                        "content": new_content,
                        "editor_id": uid,
                    }, exclude_user_id=uid)
                    members = await conversation_member_ids(db, row['conversation_id'])
                    await _push_conversation_updates(uid, row['conversation_id'],
                                                     [m for m in members if m != uid])
                finally:
                    await db.close()
                continue

            # ---- delete (via REST normally, kept for legacy clients) ----
            if mtype == "delete_message":
                mid = data.get("message_id")
                if not mid:
                    continue
                db = await get_db()
                try:
                    async with db.execute(
                        "SELECT * FROM messages WHERE id = ? AND sender_id = ?",
                        (mid, uid)
                    ) as cursor:
                        row = await cursor.fetchone()
                    if not row:
                        await ws.send_json({"type": "error", "code": "NOT_FOUND",
                                            "message": "Message not found"})
                        continue
                    if row['file_path']:
                        try:
                            await asyncio.to_thread(delete_file, row['file_path'])
                        except Exception:
                            pass
                    await db.execute("DELETE FROM messages WHERE id = ?", (mid,))
                    await db.execute("DELETE FROM read_receipts WHERE message_id = ?", (mid,))
                    await db.commit()
                    schedule_db_sync()
                    await _broadcast_to_conversation(db, row['conversation_id'], {
                        "type": "message_deleted",
                        "message_id": mid,
                        "conversation_id": row['conversation_id'],
                        "deleted_by": uid,
                    })
                    members = await conversation_member_ids(db, row['conversation_id'])
                    await _push_conversation_updates(uid, row['conversation_id'],
                                                     [m for m in members if m != uid])
                finally:
                    await db.close()
                continue

            # ---- typing (scoped to one conversation) ----
            if mtype == "typing":
                conversation_id = int(data.get("conversation_id") or GLOBAL_CONVERSATION_ID)
                if bool(data.get("is_typing", False)) and not _allow_typing(uid, conversation_id):
                    continue
                db = await get_db()
                try:
                    await _broadcast_to_conversation(db, conversation_id, {
                        "type": "typing_indicator",
                        "user_id": uid,
                        "username": username,
                        "display_name": display_name,
                        "is_typing": bool(data.get("is_typing", False)),
                        "conversation_id": conversation_id,
                    }, exclude_user_id=uid)
                finally:
                    await db.close()
                continue

            # ---- mark read: instant receipts the moment the tab is open ----
            if mtype == "mark_read":
                up_to = data.get("up_to_message_id")
                conversation_id = int(data.get("conversation_id") or GLOBAL_CONVERSATION_ID)
                if not up_to:
                    continue
                db = await get_db()
                try:
                    if not await user_in_conversation(db, uid, conversation_id):
                        continue

                    # Keep the per-user watermark monotonic
                    cursor = await db.execute(
                        """SELECT last_read_message_id FROM conversation_members
                           WHERE conversation_id = ? AND user_id = ?""",
                        (conversation_id, uid)
                    )
                    row = await cursor.fetchone()
                    watermark = row["last_read_message_id"] if row else 0
                    if int(up_to) <= watermark:
                        continue

                    now_ms = int(time.time() * 1000)
                    cursor = await db.execute(
                        """SELECT m.id, m.sender_id FROM messages m
                           WHERE m.conversation_id = ? AND m.id > ? AND m.id <= ?
                             AND m.sender_id != ? AND m.is_deleted = 0
                           ORDER BY m.id ASC""",
                        (conversation_id, watermark, up_to, uid)
                    )
                    rows = await cursor.fetchall()
                    # Watermark still advances to up_to, but only the newest batch
                    # needs per-message receipts (bounds write + notification cost).
                    for r in rows[-5000:]:
                        await db.execute(
                            "INSERT OR IGNORE INTO read_receipts (user_id, message_id, read_at) "
                            "VALUES (?,?,?)",
                            (uid, r['id'], now_ms)
                        )
                    new_reads = rows[-5000:]

                    await db.execute(
                        """INSERT INTO conversation_members (conversation_id, user_id, last_read_message_id)
                           VALUES (?,?,?)
                           ON CONFLICT(conversation_id, user_id)
                           DO UPDATE SET last_read_message_id = excluded.last_read_message_id""",
                        (conversation_id, uid, int(up_to))
                    )
                    await db.commit()
                    if new_reads:
                        schedule_db_sync()

                    # Notify each author the instant one of their messages is read
                    for r in new_reads:
                        await manager.send_to_user(r['sender_id'], {
                            "type": "message_read",
                            "message_id": r['id'],
                            "conversation_id": conversation_id,
                            "reader": {
                                "user_id": uid,
                                "username": username,
                                "display_name": display_name,
                                "avatar_path": avatar_path,
                            },
                            "read_at": now_ms,
                        })
                    await _push_conversation_updates(uid, conversation_id, [])
                finally:
                    await db.close()
                continue

            # ---- online users ----
            if mtype == "get_online_users":
                await ws.send_json({
                    "type": "online_users",
                    "users": manager.get_online_users()
                })
                continue

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"WS error for {username}: {e}", exc_info=True)
    finally:
        # Remove this socket first. If the user still has another connection
        # (multi-tab), they stay online; otherwise they are now fully offline.
        manager.disconnect(ws, uid)
        if uid not in manager.active:
            db = await get_db()
            try:
                await db.execute(
                    "UPDATE users SET status = 'offline', last_seen = ? WHERE id = ?",
                    (int(time.time()), uid)
                )
                await db.commit()
                schedule_db_sync()
            finally:
                await db.close()
            await manager.broadcast({
                "type": "user_status",
                "user_id": uid,
                "username": username,
                "display_name": display_name,
                "avatar_path": avatar_path,
                "status": "offline"
            })


@app.get("/")
async def root():
    return FileResponse("static/index.html", headers={"Cache-Control": "no-cache"})
