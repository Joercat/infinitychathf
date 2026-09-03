import os
import base64
import sqlite3
import tempfile
import logging
from typing import Optional, BinaryIO, Dict, Any, List
import time
import threading

from huggingface_hub import HfFileSystem, HfApi
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger("InfinityChat.Storage")

# ------------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------------
HF_TOKEN = os.environ.get("HF_TOKEN")
BUCKET_NAME = os.environ.get("INFINITY_CHAT_BUCKET", "infinitychat-data")
FILE_ENCRYPTION_KEY_B64 = os.environ.get("FILE_ENCRYPTION_KEY", None)

# Offline mode: lets the app run without a Hugging Face token (dev/tests).
# File storage endpoints will return a clear error instead of crashing boot.
_OFFLINE = not HF_TOKEN or HF_TOKEN.strip() in ("", "none", "offline", "0")

if FILE_ENCRYPTION_KEY_B64 is None:
    if _OFFLINE:
        # Deterministic dev-only key so local databases stay readable between runs.
        FILE_ENCRYPTION_KEY_B64 = base64.urlsafe_b64encode(b"0" * 32).decode()
        logger.warning("⚠️  FILE_ENCRYPTION_KEY not set and running offline - using dev-only key")
    else:
        from cryptography.fernet import Fernet
        FILE_ENCRYPTION_KEY_B64 = Fernet.generate_key().decode()
        logger.warning(
            "⚠️  FILE_ENCRYPTION_KEY not set - generated a random key for this run. "
            "Files uploaded now will NOT be readable after restart unless you persist it!"
        )

try:
    FILE_ENCRYPTION_KEY = base64.urlsafe_b64decode(FILE_ENCRYPTION_KEY_B64.encode())
except Exception as e:
    raise ValueError(f"Invalid FILE_ENCRYPTION_KEY format: {e}")

assert len(FILE_ENCRYPTION_KEY) == 32, "FILE_ENCRYPTION_KEY must decode to exactly 32 bytes for AES-256"

# ------------------------------------------------------------------------
# Storage availability
# ------------------------------------------------------------------------
class StorageUnavailableError(IOError):
    """Raised when a storage operation is attempted without a valid HF_TOKEN."""


def _require_available():
    if _OFFLINE:
        raise StorageUnavailableError(
            "File storage is unavailable: HF_TOKEN is not configured."
        )


def _get_api():
    _require_available()
    return HfApi(token=HF_TOKEN)


def _get_fs():
    _require_available()
    return HfFileSystem(token=HF_TOKEN)


# ------------------------------------------------------------------------
# Get bucket owner (HF username)
# ------------------------------------------------------------------------
OWNER = None
BUCKET_ID = BUCKET_NAME
BUCKET_URI = None

if not _OFFLINE:
    try:
        owner_info = _get_api().whoami()
        OWNER = owner_info["name"]
    except Exception as e:
        logger.warning(f"Could not determine HF username: {e}")
        OWNER = None

    if OWNER:
        if "/" in BUCKET_NAME:
            OWNER, BUCKET_ID = BUCKET_NAME.split("/", 1)
        else:
            BUCKET_ID = BUCKET_NAME
        BUCKET_URI = f"hf://buckets/{OWNER}/{BUCKET_ID}"
        logger.info(f"📦 Bucket URI: {BUCKET_URI}")


# ------------------------------------------------------------------------
# Bucket Initialization
# ------------------------------------------------------------------------
def ensure_bucket():
    """Create the private bucket if it doesn't exist."""
    if _OFFLINE:
        return
    global OWNER, BUCKET_URI, BUCKET_ID
    api = _get_api()
    try:
        api.create_bucket(
            bucket_id=f"{OWNER}/{BUCKET_ID}",
            private=True,
            exist_ok=True
        )
        logger.info(f"✅ Bucket '{OWNER}/{BUCKET_ID}' is ready (private)")
    except Exception as e:
        error_str = str(e).lower()
        if "already exists" in error_str:
            logger.info(f"📦 Bucket '{OWNER}/{BUCKET_ID}' already exists")
        elif "403" in error_str or "401" in error_str:
            logger.warning(f"⚠️  Cannot create bucket - permission issue: {e}")
        else:
            try:
                api.create_bucket(
                    bucket_id=BUCKET_ID,
                    private=True,
                    exist_ok=True
                )
                OWNER = ""
                BUCKET_URI = f"hf://buckets/{BUCKET_ID}"
                logger.info(f"✅ Bucket '{BUCKET_ID}' is ready (alternative format)")
            except Exception as e2:
                error_str2 = str(e2).lower()
                if "already exists" in error_str2:
                    logger.info(f"📦 Bucket '{BUCKET_ID}' already exists")
                else:
                    logger.error(f"❌ Failed to create bucket: {e2}")


if not _OFFLINE:
    ensure_bucket()

# ------------------------------------------------------------------------
# Heartbeat sync
# ------------------------------------------------------------------------
def _sync_bucket_interval():
    if _OFFLINE:
        return

    def _sync():
        while True:
            time.sleep(60)
            try:
                stats = get_storage_stats()
                logger.info(
                    f"📦 Bucket heartbeat — "
                    f"{stats['file_count']} files / {stats['total_size_mb']} MB"
                )
            except Exception as e:
                logger.warning(f"⚠️  Bucket heartbeat failed: {e}")
    t = threading.Thread(target=_sync, daemon=True)
    t.start()


_sync_bucket_interval()

# ------------------------------------------------------------------------
# Encryption / Decryption
# ------------------------------------------------------------------------
def encrypt_bytes(data: bytes, aad: Optional[bytes] = None) -> bytes:
    aesgcm = AESGCM(FILE_ENCRYPTION_KEY)
    nonce = os.urandom(12)
    ciphertext = aesgcm.encrypt(nonce, data, aad or b"")
    return nonce + ciphertext


def decrypt_bytes(encrypted_blob: bytes, aad: Optional[bytes] = None) -> bytes:
    aesgcm = AESGCM(FILE_ENCRYPTION_KEY)
    nonce = encrypted_blob[:12]
    ciphertext = encrypted_blob[12:]
    return aesgcm.decrypt(nonce, ciphertext, aad or b"")


def verify_file_integrity(encrypted_blob: bytes) -> bool:
    return len(encrypted_blob) >= 29

# ------------------------------------------------------------------------
# Path Utilities
# ------------------------------------------------------------------------
def _bucket_path(remote_path: str) -> str:
    _require_available()
    if not BUCKET_URI:
        raise StorageUnavailableError("Bucket is not configured (offline mode).")
    return f"{BUCKET_URI}/{remote_path.lstrip('/')}"


def _validate_path(remote_path: str) -> None:
    if not remote_path:
        raise ValueError("Path cannot be empty")
    if ".." in remote_path.split("/"):
        raise ValueError("Path traversal detected")
    if remote_path.startswith("/") or remote_path.startswith("\\"):
        raise ValueError("Path cannot start with separator")
    if len(remote_path) > 1024:
        raise ValueError("Path too long (max 1024 characters)")

# ------------------------------------------------------------------------
# Core File Operations
# ------------------------------------------------------------------------
def store_file(remote_path: str, data: bytes, encrypt: bool = True) -> str:
    _validate_path(remote_path)
    _require_available()
    fs = _get_fs()
    try:
        payload = encrypt_bytes(data, remote_path.encode('utf-8')) if encrypt else data
        full_uri = _bucket_path(remote_path)
        with fs.open(full_uri, "wb") as f:
            f.write(payload)
        if not fs.exists(full_uri):
            raise IOError(f"Failed to verify file was stored: {full_uri}")
        logger.debug(f"💾 Stored: {remote_path} ({len(data)} bytes)")
        return remote_path
    except Exception as e:
        logger.error(f"❌ Failed to store {remote_path}: {e}")
        raise IOError(f"Storage failed: {e}")


def retrieve_file(remote_path: str, decrypt: bool = True) -> bytes:
    _validate_path(remote_path)
    _require_available()
    fs = _get_fs()
    full_uri = _bucket_path(remote_path)
    try:
        if not fs.exists(full_uri):
            raise FileNotFoundError(f"File not found: {remote_path}")
        with fs.open(full_uri, "rb") as f:
            payload = f.read()
        if not payload:
            raise IOError(f"Empty file: {remote_path}")
        if decrypt:
            if not verify_file_integrity(payload):
                raise IOError(f"Corrupted file: {remote_path}")
            data = decrypt_bytes(payload, remote_path.encode('utf-8'))
            logger.debug(f"📂 Retrieved: {remote_path} ({len(data)} bytes)")
            return data
        return payload
    except FileNotFoundError:
        raise
    except Exception as e:
        logger.error(f"❌ Failed to retrieve {remote_path}: {e}")
        raise IOError(f"Retrieval failed: {e}")


def delete_file(remote_path: str) -> bool:
    _validate_path(remote_path)
    if _OFFLINE:
        return False
    fs = _get_fs()
    full_uri = _bucket_path(remote_path)
    try:
        if fs.exists(full_uri):
            fs.rm(full_uri)
            logger.debug(f"🗑️  Deleted: {remote_path}")
            return True
        logger.warning(f"⚠️  Not found for deletion: {remote_path}")
        return False
    except Exception as e:
        logger.error(f"❌ Failed to delete {remote_path}: {e}")
        raise IOError(f"Deletion failed: {e}")


def file_exists(remote_path: str) -> bool:
    if _OFFLINE:
        return False
    try:
        _validate_path(remote_path)
        return _get_fs().exists(_bucket_path(remote_path))
    except Exception:
        return False


def list_files(prefix: str = "", recursive: bool = True) -> List[str]:
    if _OFFLINE:
        return []
    try:
        fs = _get_fs()
        search_path = _bucket_path(prefix) if prefix else BUCKET_URI
        items = fs.ls(search_path, detail=False, recursive=recursive)
        prefix_len = len(BUCKET_URI) + 1
        return [item[prefix_len:] for item in items if not fs.isdir(item)]
    except FileNotFoundError:
        return []
    except Exception as e:
        logger.error(f"Failed to list files: {e}")
        return []


def get_file_size(remote_path: str) -> Optional[int]:
    if _OFFLINE:
        return None
    try:
        _validate_path(remote_path)
        info = _get_fs().info(_bucket_path(remote_path))
        return info.get("size")
    except Exception:
        return None


def get_file_info(remote_path: str) -> Optional[Dict[str, Any]]:
    if _OFFLINE:
        return None
    try:
        _validate_path(remote_path)
        fs = _get_fs()
        info = fs.info(_bucket_path(remote_path))
        return {
            "name": remote_path,
            "size": info.get("size"),
            "created": info.get("created"),
            "modified": info.get("last_modified"),
            "type": info.get("type", "file")
        }
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.error(f"Failed to get file info: {remote_path}: {e}")
        return None


def store_file_stream(remote_path: str, data_stream: BinaryIO, encrypt: bool = True) -> str:
    return store_file(remote_path, data_stream.read(), encrypt=encrypt)


def retrieve_file_stream(remote_path: str, decrypt: bool = True) -> BinaryIO:
    import io
    return io.BytesIO(retrieve_file(remote_path, decrypt=decrypt))


def get_storage_stats() -> Dict[str, Any]:
    if _OFFLINE:
        return {
            "bucket": None, "file_count": 0, "total_size": 0,
            "total_size_mb": 0, "error": "Storage offline (HF_TOKEN not configured)"
        }
    try:
        fs = _get_fs()
        items = fs.ls(BUCKET_URI, detail=True, recursive=True)
        total_size = 0
        file_count = 0
        for item in items:
            if item.get("type") == "file":
                total_size += item.get("size") or 0
                file_count += 1
        return {
            "bucket": f"{OWNER}/{BUCKET_ID}",
            "file_count": file_count,
            "total_size": total_size,
            "total_size_mb": round(total_size / (1024 * 1024), 2)
        }
    except Exception as e:
        logger.error(f"Failed to get storage stats: {e}")
        return {
            "bucket": f"{OWNER}/{BUCKET_ID}",
            "file_count": 0,
            "total_size": 0,
            "total_size_mb": 0,
            "error": str(e)
        }


def create_backup(backup_prefix: str = "backups") -> str:
    _require_available()
    timestamp = int(time.time())
    backup_path = f"{backup_prefix}/backup_{timestamp}"
    try:
        files = list_files()
        for file_path in files:
            try:
                data = retrieve_file(file_path)
                store_file(f"{backup_path}/{file_path}", data, encrypt=True)
            except Exception as e:
                logger.error(f"Failed to backup {file_path}: {e}")
        logger.info(f"💾 Backup created at: {backup_path}")
        return backup_path
    except Exception as e:
        logger.error(f"❌ Backup failed: {e}")
        raise

# ------------------------------------------------------------------------
# Database Sync to/from Bucket
# ------------------------------------------------------------------------
DB_BUCKET_PATH = "database/infinitychat.db"
_db_sync_lock = threading.Lock()
_last_db_sync = 0
DB_SYNC_INTERVAL = 30  # seconds


def download_database(local_path: str) -> bool:
    """
    Download database from bucket to local path.
    Returns True if database was found and downloaded.
    """
    if _OFFLINE:
        logger.info("📭 Storage offline - skipping database download")
        return False
    try:
        if file_exists(DB_BUCKET_PATH):
            logger.info("📥 Downloading database from bucket...")
            data = retrieve_file(DB_BUCKET_PATH)
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            with open(local_path, 'wb') as f:
                f.write(data)
            logger.info(f"✅ Database downloaded ({len(data):,} bytes)")
            return True
        else:
            logger.info("📭 No existing database in bucket - will create fresh")
            return False
    except Exception as e:
        logger.error(f"❌ Failed to download database: {e}")
        return False


def upload_database(local_path: str) -> bool:
    """
    Upload a consistent snapshot of the local database to the bucket.

    The app runs SQLite in WAL mode, so the main .db file can lag behind
    committed transactions (they live in the -wal file). Copying the raw file
    could persist stale or torn data - instead we take a proper snapshot with
    the sqlite backup API, which is safe even while writers are active.
    Returns True on success.
    """
    global _last_db_sync
    if _OFFLINE:
        return False
    with _db_sync_lock:
        tmp_path = None
        try:
            if not os.path.exists(local_path):
                logger.warning(f"⚠️  Database not found at {local_path}, skipping upload")
                return False

            # snapshot to a temp file via the online backup API
            fd, tmp_path = tempfile.mkstemp(suffix=".db",
                                            dir=os.path.dirname(os.path.abspath(local_path)) or ".")
            os.close(fd)
            os.unlink(tmp_path)  # backup() needs the destination to not exist
            src = sqlite3.connect(f"file:{local_path}?mode=ro", uri=True)
            dst = sqlite3.connect(tmp_path)
            try:
                src.backup(dst)
            finally:
                src.close()
                dst.close()

            with open(tmp_path, 'rb') as f:
                data = f.read()
            if not data:
                logger.warning("⚠️  Database snapshot is empty, skipping upload")
                return False
            store_file(DB_BUCKET_PATH, data)
            _last_db_sync = time.time()
            logger.debug(f"☁️  Database synced to bucket ({len(data):,} bytes)")
            return True
        except Exception as e:
            logger.error(f"❌ Failed to upload database: {e}")
            return False
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass


def start_db_sync(local_path: str):
    """
    Start background thread that periodically uploads the database to bucket.
    """
    if _OFFLINE:
        logger.info("🔄 Database auto-sync skipped (storage offline)")
        return

    def _sync_loop():
        while True:
            time.sleep(DB_SYNC_INTERVAL)
            try:
                upload_database(local_path)
            except Exception as e:
                logger.warning(f"⚠️  Periodic DB sync failed: {e}")

    t = threading.Thread(target=_sync_loop, daemon=True)
    t.start()
    logger.info(f"🔄 Database auto-sync started (every {DB_SYNC_INTERVAL}s)")


def close():
    try:
        if not _OFFLINE:
            _get_fs().close()
    except Exception:
        pass

# ------------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------------
__all__ = [
    'store_file', 'retrieve_file', 'delete_file', 'file_exists',
    'list_files', 'get_file_size', 'get_file_info',
    'store_file_stream', 'retrieve_file_stream',
    'get_storage_stats', 'create_backup', 'close',
    'download_database', 'upload_database', 'start_db_sync',
    'DB_BUCKET_PATH', 'StorageUnavailableError'
]
