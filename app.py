import os
import json
import time
import uuid
import base64
import logging
import tempfile
import shutil
from typing import Optional, Dict, List, Any
from contextlib import asynccontextmanager

import aiosqlite

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Header, Query, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.responses import Response, JSONResponse, FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.backends import default_backend

from storage_handler import (
    store_file, retrieve_file, delete_file, file_exists,
    download_database, upload_database, start_db_sync
)

# ------------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------------
DATABASE_URL = os.environ.get("DATABASE_URL", "/data/infinitychat.db")
MESSAGE_KEY_B64 = os.environ.get("SECRET_KEY", None)

# GIVE ME THE KEYS NOW!!!!
print(MESSAGE_KEY_B64)
FILE_ENCRYPTION_KEY_B64 = os.environ.get("FILE_ENCRYPTION_KEY", None)
print(FILE_ENCRYPTION_KEY_B64)
# end

if MESSAGE_KEY_B64 is None:
    MESSAGE_KEY_B64 = base64.urlsafe_b64encode(os.urandom(32)).decode()
    print("WARNING: SECRET_KEY not set – generated random key. Set it permanently!")
MESSAGE_KEY = base64.urlsafe_b64decode(MESSAGE_KEY_B64)
assert len(MESSAGE_KEY) == 32

# Temp directory for chunk assembly - uses disk not RAM
TEMP_DIR = os.environ.get("TEMP_DIR", "/tmp/infinitychat_uploads")
os.makedirs(TEMP_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("InfinityChat")

# ------------------------------------------------------------------------
# Database initialization
# ------------------------------------------------------------------------
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
        await db.execute("CREATE INDEX IF NOT EXISTS idx_messages_sender_time ON messages(sender_id, timestamp_ms)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_messages_time_id ON messages(timestamp_ms, id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_messages_not_deleted ON messages(is_deleted, id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_receipts_user_msg ON read_receipts(user_id, message_id)")
        await db.commit()

    upload_database(DATABASE_URL)
    start_db_sync(DATABASE_URL)
    logger.info("✅ Database initialized successfully")

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

app = FastAPI(title="InfinityChat", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
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
def hash_password(password: str, salt: Optional[str] = None) -> tuple[str, str]:
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
# HTTP Endpoints
# ------------------------------------------------------------------------
@app.get("/api/health")
async def health_check():
    return {"status": "healthy", "timestamp": int(time.time()), "version": "1.0.0"}

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
        await db.execute(
            "INSERT INTO users (username, display_name, password_hash, salt, token, status) VALUES (?,?,?,?,?,'online')",
            (username.lower(), display, pwd_hash, salt, token)
        )
        await db.commit()
        schedule_db_sync()
        return {
            "token": token,
            "user": {
                "id": None,
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
        await db.execute(
            "UPDATE users SET token = NULL, status = 'offline', last_seen = ? WHERE token = ?",
            (int(time.time()), token)
        )
        await db.commit()
        schedule_db_sync()
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
    display_name: str = Query(...),
    token: str = Header(..., alias="X-Auth-Token")
):
    user = await authenticate_user(token)
    if not user:
        raise HTTPException(401)
    db = await get_db()
    try:
        await db.execute(
            "UPDATE users SET display_name = ? WHERE id = ?",
            (display_name, user['id'])
        )
        await db.commit()
        schedule_db_sync()
        user['display_name'] = display_name
        return {"user": user}
    finally:
        await db.close()

@app.post("/api/profile/avatar")
async def upload_avatar(
    file: UploadFile = File(...),
    token: str = Header(..., alias="X-Auth-Token")
):
    user = await authenticate_user(token)
    if not user:
        raise HTTPException(401)

    # Write to temp file on disk first, not RAM
    suffix = os.path.splitext(file.filename)[1] if file.filename else '.jpg'
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=TEMP_DIR) as tmp:
        tmp_path = tmp.name
        size = 0
        chunk = await file.read(65536)
        while chunk:
            size += len(chunk)
            if size > 5 * 1024 * 1024:
                os.unlink(tmp_path)
                raise HTTPException(400, "Image too large (max 5MB)")
            tmp.write(chunk)
            chunk = await file.read(65536)

    try:
        with open(tmp_path, 'rb') as f:
            data = f.read()
        remote_path = f"avatars/{user['username']}_{uuid.uuid4().hex}{suffix}"
        store_file(remote_path, data)
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
    return {"avatar_path": remote_path}

# ------------------------------------------------------------------------
# Chunked Upload - assembles chunks on DISK not RAM
# ------------------------------------------------------------------------
# Tracks active uploads: upload_id -> metadata dict (no chunk data in RAM)
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

    # Validate file size limit (500MB)
    MAX_FILE_SIZE = 500 * 1024 * 1024
    if file_size > MAX_FILE_SIZE:
        raise HTTPException(400, "File too large (max 500MB)")

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

    # Write chunk directly to disk
    chunk_path = os.path.join(session['session_dir'], f"chunk_{chunk_index:06d}")
    with open(chunk_path, 'wb') as f:
        # Stream chunk to disk in 64KB pieces to avoid RAM spike
        data = await file.read(65536)
        while data:
            f.write(data)
            data = await file.read(65536)

    session['received_chunks'].add(chunk_index)

    if len(session['received_chunks']) == total_chunks:
        # All chunks received - assemble on disk then stream to bucket
        remote_path = f"uploads/{user['username']}/{uuid.uuid4().hex}/{file_name}"
        session_dir = session['session_dir']

        try:
            # Assemble chunks into single temp file on disk
            assembled_path = os.path.join(session_dir, "assembled")
            with open(assembled_path, 'wb') as out_f:
                for i in range(total_chunks):
                    chunk_file = os.path.join(session_dir, f"chunk_{i:06d}")
                    with open(chunk_file, 'rb') as in_f:
                        # Copy in 1MB pieces
                        buf = in_f.read(1024 * 1024)
                        while buf:
                            out_f.write(buf)
                            buf = in_f.read(1024 * 1024)
                    os.unlink(chunk_file)  # Delete chunk immediately after use

            # Read assembled file and store to bucket
            # Note: this does load into RAM once for encryption
            # For very large files this is unavoidable with AES-GCM
            # as it needs to process the whole file
            with open(assembled_path, 'rb') as f:
                full_data = f.read()
            store_file(remote_path, full_data)
            del full_data  # Explicitly free RAM immediately

        finally:
            # Always clean up temp dir
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
# avoids loading entire file into RAM
# ------------------------------------------------------------------------
DOWNLOAD_CHUNK_SIZE = 1024 * 1024  # 1MB streaming chunks

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
    if '..' in file_path:
        raise HTTPException(400, "Invalid path")

    try:
        # retrieve_file decrypts and returns bytes
        # For true streaming we'd need a streaming decrypt but AES-GCM
        # requires the full ciphertext to verify the auth tag before
        # decrypting - so one full read is required for security.
        # We do however stream the response TO the client in chunks.
        data = retrieve_file(file_path)
    except FileNotFoundError:
        raise HTTPException(404, "File not found")
    except Exception as e:
        logger.error(f"Download error for {file_path}: {e}")
        raise HTTPException(500, "Failed to retrieve file")

    import mimetypes
    mt = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
    file_size = len(data)

    # Stream response to client in chunks to avoid keeping
    # large response in RAM on the server side
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
            "Content-Disposition": f'inline; filename="{os.path.basename(file_path)}"'
        }
    )

# ------------------------------------------------------------------------
# WebSocket Manager
# ------------------------------------------------------------------------
class ConnectionManager:
    def __init__(self):
        self.active: Dict[int, Dict[str, Any]] = {}

    async def connect(self, ws: WebSocket, user_id: int, username: str):
        await ws.accept()
        if user_id not in self.active:
            self.active[user_id] = {"username": username, "connections": set()}
        self.active[user_id]["connections"].add(ws)

    def disconnect(self, ws: WebSocket, user_id: int):
        if user_id in self.active:
            self.active[user_id]["connections"].discard(ws)
            if not self.active[user_id]["connections"]:
                del self.active[user_id]

    async def broadcast(self, msg: dict, exclude_user_id: Optional[int] = None):
        for uid, info in list(self.active.items()):
            if uid == exclude_user_id:
                continue
            for ws in list(info["connections"]):
                try:
                    await ws.send_json(msg)
                except Exception:
                    self.disconnect(ws, uid)

    async def send_to_user(self, user_id: int, msg: dict):
        if user_id in self.active:
            for ws in list(self.active[user_id]["connections"]):
                try:
                    await ws.send_json(msg)
                except Exception:
                    self.disconnect(ws, user_id)

    def get_online_users(self) -> List[Dict]:
        return [
            {"id": uid, "username": info["username"]}
            for uid, info in self.active.items()
        ]

    def is_online(self, user_id: int) -> bool:
        return user_id in self.active

manager = ConnectionManager()

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

    await manager.connect(ws, uid, username)

    await ws.send_json({
        "type": "connection_established",
        "user_id": uid,
        "username": username,
        "display_name": display_name,
        "avatar_path": avatar_path,
        "online_users": manager.get_online_users()
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

            if mtype == "send_message":
                content = data.get("content", "").strip()
                reply_to_id = data.get("reply_to_id")
                file_path = data.get("file_path")
                file_type = data.get("file_type")
                file_name = data.get("file_name")
                file_size = data.get("file_size", 0)
                client_id = data.get("client_id")

                if not content and not file_path:
                    await ws.send_json({"type": "error", "code": "EMPTY", "message": "Message cannot be empty"})
                    continue
                if len(content) > 10000:
                    await ws.send_json({"type": "error", "code": "TOO_LONG", "message": "Message too long"})
                    continue

                encrypted = encrypt_message(content)
                ts = int(time.time() * 1000)

                db = await get_db()
                try:
                    cursor = await db.execute(
                        """INSERT INTO messages
                           (sender_id, encrypted_content, timestamp_ms, reply_to_id,
                            file_path, file_type, file_name, file_size)
                           VALUES (?,?,?,?,?,?,?,?)""",
                        (uid, encrypted, ts, reply_to_id, file_path, file_type, file_name, file_size)
                    )
                    mid = cursor.lastrowid
                    await db.commit()
                    schedule_db_sync()
                finally:
                    await db.close()

                msg_obj = {
                    "id": mid,
                    "sender_id": uid,
                    "sender_username": username,
                    "sender_display_name": display_name,
                    "sender_avatar_path": avatar_path,
                    "content": content,
                    "timestamp_ms": ts,
                    "reply_to_id": reply_to_id,
                    "file_path": file_path,
                    "file_type": file_type,
                    "file_name": file_name,
                    "file_size": file_size,
                    "is_edited": False,
                    "is_deleted": False,
                    "status": "sent"
                }

                await ws.send_json({
                    "type": "new_message",
                    "message": {**msg_obj, "status": "delivered"},
                    "client_id": client_id
                })

                await manager.broadcast({
                    "type": "new_message",
                    "message": msg_obj
                }, exclude_user_id=uid)

            elif mtype == "load_messages":
                cursor_id = data.get("cursor")
                limit = min(data.get("limit", 50), 100)
                db = await get_db()
                try:
                    if cursor_id:
                        rows = await db.execute(
                            """SELECT m.*, u.username, u.display_name, u.avatar_path
                               FROM messages m JOIN users u ON m.sender_id = u.id
                               WHERE m.is_deleted = 0 AND m.id < ?
                               ORDER BY m.id DESC LIMIT ?""",
                            (cursor_id, limit)
                        )
                    else:
                        rows = await db.execute(
                            """SELECT m.*, u.username, u.display_name, u.avatar_path
                               FROM messages m JOIN users u ON m.sender_id = u.id
                               WHERE m.is_deleted = 0
                               ORDER BY m.id DESC LIMIT ?""",
                            (limit,)
                        )

                    msgs = []
                    for row in reversed(await rows.fetchall()):
                        try:
                            decrypted = decrypt_message(row['encrypted_content'])
                        except Exception:
                            decrypted = "[Decryption Error]"

                        read_cursor = await db.execute(
                            "SELECT COUNT(*) as cnt FROM read_receipts WHERE message_id = ?",
                            (row['id'],)
                        )
                        read_count = (await read_cursor.fetchone())['cnt']

                        msgs.append({
                            "id": row['id'],
                            "sender_id": row['sender_id'],
                            "sender_username": row['username'],
                            "sender_display_name": row['display_name'],
                            "sender_avatar_path": row['avatar_path'],
                            "content": decrypted,
                            "timestamp_ms": row['timestamp_ms'],
                            "reply_to_id": row['reply_to_id'],
                            "file_path": row['file_path'],
                            "file_type": row['file_type'],
                            "file_name": row['file_name'],
                            "file_size": row['file_size'],
                            "is_edited": bool(row['is_edited']),
                            "is_deleted": False,
                            "status": "read" if read_count > 0 else "sent"
                        })

                    next_cursor = msgs[0]['id'] if msgs else None
                    await ws.send_json({
                        "type": "messages_loaded",
                        "messages": msgs,
                        "next_cursor": next_cursor,
                        "has_more": len(msgs) == limit
                    })
                finally:
                    await db.close()

            elif mtype == "edit_message":
                mid = data.get("message_id")
                new_content = data.get("content", "").strip()
                if not mid or not new_content:
                    continue
                db = await get_db()
                try:
                    async with db.execute(
                        "SELECT * FROM messages WHERE id = ? AND sender_id = ? AND is_deleted = 0",
                        (mid, uid)
                    ) as cursor:
                        if not await cursor.fetchone():
                            await ws.send_json({"type": "error", "code": "NOT_FOUND", "message": "Message not found"})
                            continue
                    new_enc = encrypt_message(new_content)
                    await db.execute(
                        "UPDATE messages SET encrypted_content = ?, is_edited = 1 WHERE id = ?",
                        (new_enc, mid)
                    )
                    await db.commit()
                    schedule_db_sync()
                    await manager.broadcast({
                        "type": "message_edited",
                        "message_id": mid,
                        "content": new_content,
                        "editor_id": uid
                    })
                finally:
                    await db.close()

            elif mtype == "delete_message":
                mid = data.get("message_id")
                if not mid:
                    continue
                db = await get_db()
                try:
                    async with db.execute(
                        "SELECT * FROM messages WHERE id = ? AND sender_id = ?",
                        (mid, uid)
                    ) as cursor:
                        msg = await cursor.fetchone()
                    if not msg:
                        await ws.send_json({"type": "error", "code": "NOT_FOUND", "message": "Message not found"})
                        continue
                    if msg['file_path']:
                        try:
                            delete_file(msg['file_path'])
                        except Exception:
                            pass
                    await db.execute("DELETE FROM messages WHERE id = ?", (mid,))
                    await db.execute("DELETE FROM read_receipts WHERE message_id = ?", (mid,))
                    await db.commit()
                    schedule_db_sync()
                    await manager.broadcast({"type": "message_deleted", "message_id": mid})
                finally:
                    await db.close()

            elif mtype == "typing":
                await manager.broadcast({
                    "type": "typing_indicator",
                    "user_id": uid,
                    "username": username,
                    "display_name": display_name,
                    "is_typing": bool(data.get("is_typing", False))
                }, exclude_user_id=uid)

            elif mtype == "mark_read":
                up_to = data.get("up_to_message_id")
                if not up_to:
                    continue
                db = await get_db()
                try:
                    rows = await db.execute(
                        """SELECT m.id, m.sender_id FROM messages m
                           WHERE m.id <= ? AND m.sender_id != ? AND m.is_deleted = 0
                           AND m.id NOT IN (
                               SELECT message_id FROM read_receipts WHERE user_id = ?
                           )""",
                        (up_to, uid, uid)
                    )
                    new_reads = []
                    async for r in rows:
                        await db.execute(
                            "INSERT OR IGNORE INTO read_receipts (user_id, message_id) VALUES (?,?)",
                            (uid, r['id'])
                        )
                        new_reads.append(r)
                    await db.commit()
                    if new_reads:
                        schedule_db_sync()
                    for r in new_reads:
                        await manager.send_to_user(r['sender_id'], {
                            "type": "message_read",
                            "message_id": r['id'],
                            "reader_username": username
                        })
                finally:
                    await db.close()

            elif mtype == "get_online_users":
                await ws.send_json({
                    "type": "online_users",
                    "users": manager.get_online_users()
                })

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"WS error for {username}: {e}")
    finally:
        manager.disconnect(ws, uid)
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
    return FileResponse("static/index.html")