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

# Local filesystem fallback used when HF_TOKEN is not configured. This lets
# previews/dev machines upload files, run backups, and keep data between
# restarts without a Hugging Face account.
LOCAL_STORAGE_DIR = os.environ.get(
    "LOCAL_STORAGE_DIR",
    os.path.join(os.path.expanduser("~"), ".infinitychat_storage"),
)

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



def _local_root() -> str:
    os.makedirs(LOCAL_STORAGE_DIR, exist_ok=True)
    return LOCAL_STORAGE_DIR


def _local_path(remote_path: str) -> str:
    _validate_path(remote_path)
    return os.path.join(_local_root(), *remote_path.split("/"))


def _local_list(prefix: str = "", recursive: bool = True) -> List[str]:
    root = _local_root()
    base = os.path.join(root, *prefix.split("/")) if prefix else root
    if not os.path.isdir(base):
        return []
    out = []
    if recursive:
        for dirpath, _dirnames, filenames in os.walk(base):
            for name in filenames:
                full = os.path.join(dirpath, name)
                out.append(os.path.relpath(full, root).replace(os.sep, "/"))
    else:
        for name in os.listdir(base):
            full = os.path.join(base, name)
            if os.path.isfile(full):
                out.append(os.path.relpath(full, root).replace(os.sep, "/"))
    return sorted(out)

# ------------------------------------------------------------------------
# Core File Operations
# ------------------------------------------------------------------------
def store_file(remote_path: str, data: bytes, encrypt: bool = True) -> str:
    _validate_path(remote_path)
    try:
        payload = encrypt_bytes(data, remote_path.encode('utf-8')) if encrypt else data
        if _OFFLINE:
            path = _local_path(remote_path)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(payload)
            os.replace(tmp, path)
            logger.debug(f"💾 Stored (local): {remote_path} ({len(data)} bytes)")
            return remote_path
        fs = _get_fs()
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
    try:
        if _OFFLINE:
            path = _local_path(remote_path)
            if not os.path.exists(path):
                raise FileNotFoundError(f"File not found: {remote_path}")
            with open(path, "rb") as f:
                payload = f.read()
        else:
            fs = _get_fs()
            full_uri = _bucket_path(remote_path)
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
    try:
        if _OFFLINE:
            path = _local_path(remote_path)
            if os.path.exists(path):
                os.unlink(path)
                logger.debug(f"🗑️  Deleted (local): {remote_path}")
                return True
            logger.warning(f"⚠️  Not found for deletion: {remote_path}")
            return False
        fs = _get_fs()
        full_uri = _bucket_path(remote_path)
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
    try:
        _validate_path(remote_path)
        if _OFFLINE:
            return os.path.exists(_local_path(remote_path))
        return _get_fs().exists(_bucket_path(remote_path))
    except Exception:
        return False


def list_files(prefix: str = "", recursive: bool = True) -> List[str]:
    try:
        if _OFFLINE:
            return _local_list(prefix, recursive)
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
    try:
        _validate_path(remote_path)
        if _OFFLINE:
            path = _local_path(remote_path)
            return os.path.getsize(path) if os.path.exists(path) else None
        info = _get_fs().info(_bucket_path(remote_path))
        return info.get("size")
    except Exception:
        return None


def get_file_info(remote_path: str) -> Optional[Dict[str, Any]]:
    try:
        _validate_path(remote_path)
        if _OFFLINE:
            path = _local_path(remote_path)
            if not os.path.exists(path):
                return None
            st = os.stat(path)
            return {
                "name": remote_path,
                "size": st.st_size,
                "created": st.st_ctime,
                "modified": st.st_mtime,
                "type": "file",
            }
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
        try:
            files = _local_list()
            total = sum(os.path.getsize(_local_path(p)) for p in files)
            return {"bucket": "local", "file_count": len(files), "total_size": total,
                    "total_size_mb": round(total / (1024 * 1024), 2)}
        except Exception as e:
            return {"bucket": "local", "file_count": 0, "total_size": 0,
                    "total_size_mb": 0, "error": str(e)}
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
    Download database from storage to the local path when no local database
    exists yet. The local SQLite file is the working copy; overwriting it on
    every restart would discard newer local changes (DMs/groups/social posts)
    that haven't been synced back yet.
    """
    if os.path.exists(local_path):
        logger.info("📁 Local database already exists - keeping it")
        return False
    try:
        if file_exists(DB_BUCKET_PATH):
            logger.info("📥 Downloading database from storage...")
            data = retrieve_file(DB_BUCKET_PATH)
            dname = os.path.dirname(os.path.abspath(local_path))
            os.makedirs(dname, exist_ok=True)
            with open(local_path, 'wb') as f:
                f.write(data)
            logger.info(f"✅ Database downloaded ({len(data):,} bytes)")
            return True
        else:
            logger.info("📭 No existing database in storage - will create fresh")
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


# ------------------------------------------------------------------------
# Backups (database + all data files) to a separate folder in the bucket
# ------------------------------------------------------------------------
BACKUP_DIR = "backups"
BACKUP_RETENTION_HOURS = int(os.environ.get("BACKUP_RETENTION_HOURS", "48"))
BACKUP_RETENTION_DAILY = int(os.environ.get("BACKUP_RETENTION_DAILY", "7"))


def _utc_slug() -> str:
    return time.strftime("%Y-%m-%d_%H-%M-%S", time.gmtime())


def _snapshot_db_bytes(local_path: str) -> bytes:
    """Take a consistent SQLite snapshot and return its bytes.

    Uses the sqlite online backup API so WAL-mode writes are not lost and the
    copied file is never a torn/corrupt snapshot.
    """
    if not os.path.exists(local_path):
        raise FileNotFoundError(f"Database not found at {local_path}")
    fd, tmp = tempfile.mkstemp(suffix=".db", dir=os.path.dirname(os.path.abspath(local_path)) or ".")
    os.close(fd)
    os.unlink(tmp)
    src = sqlite3.connect(f"file:{local_path}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    with open(tmp, "rb") as f:
        data = f.read()
    try:
        os.unlink(tmp)
    except Exception:
        pass
    if not data:
        raise IOError("Database snapshot was empty")
    return data


def _copy_live_files_to_backup(backup_prefix: str) -> int:
    """Copy current data files (uploads/avatars/database) into a backup folder.

    Existing backups under backups/ are excluded so hourly backups don't grow
    exponentially by copying previous backups.
    """
    copied = 0
    for path in list_files(""):
        if path == DB_BUCKET_PATH or path == "database/infinitychat.db":
            continue  # database is saved separately from the snapshot
        if path.startswith(BACKUP_DIR + "/"):
            continue
        try:
            data = retrieve_file(path)  # decrypted to plaintext
            store_file(f"{backup_prefix}/{path}", data, encrypt=True)
            copied += 1
        except Exception as e:
            logger.warning(f"⚠️  Backup skipped {path}: {e}")
    return copied


def list_backups() -> List[Dict[str, Any]]:
    """Return metadata for all timestamped backups (newest first)."""
    out: Dict[str, Dict[str, Any]] = {}
    try:
        for path in list_files(BACKUP_DIR):
            parts = path.split("/")
            if len(parts) < 4:
                continue
            category, stamp = parts[1], parts[2]
            key = f"{category}/{stamp}"
            if key not in out:
                out[key] = {
                    "path": f"{BACKUP_DIR}/{category}/{stamp}",
                    "category": category,
                    "timestamp": stamp,
                    "file_count": 0,
                    "size": 0,
                    "created": None,
                    "has_database": False,
                }
            info = get_file_info(path)
            out[key]["file_count"] += 1
            out[key]["size"] += int(info.get("size") or 0) if info else 0
            out[key]["created"] = info.get("created") if info else None
            if path.endswith("infinitychat.db"):
                out[key]["has_database"] = True
    except Exception as e:
        logger.error(f"Failed to list backups: {e}")
    return sorted(out.values(), key=lambda x: x["timestamp"], reverse=True)


def create_timestamped_backup(local_path: str, category: str = "hourly") -> Optional[str]:
    """Create a full point-in-time backup of the DB + data files.

    Returns the backup prefix (e.g. backups/hourly/2026-01-01_00-00-00) or None
    when the storage backend is unavailable.
    """
    with _db_sync_lock:
        prefix = f"{BACKUP_DIR}/{category}/{_utc_slug()}"
        try:
            db_bytes = _snapshot_db_bytes(local_path)
            store_file(f"{prefix}/database/infinitychat.db", db_bytes, encrypt=True)
            copied = _copy_live_files_to_backup(prefix)
            logger.info(f"💾 Backup created: {prefix} (db + {copied} files)")
            prune_backups()
            return prefix
        except Exception as e:
            logger.error(f"❌ Backup failed: {e}")
            return None


def prune_backups():
    """Keep the newest N hourly backups and the last daily marker per day."""
    try:
        hourly = sorted([b for b in list_backups() if b["category"] == "hourly"],
                        key=lambda b: b["timestamp"], reverse=True)
        for b in hourly[BACKUP_RETENTION_HOURS:]:
            _delete_tree(b["path"])
        # Keep at most one backup per calendar day (the newest of that day),
        # for a longer daily retention window.
        seen_days = set()
        for b in hourly[:BACKUP_RETENTION_HOURS]:
            day = b["timestamp"][:10]
            if day not in seen_days:
                seen_days.add(day)
        daily = sorted([b for b in hourly if b["timestamp"][:10] not in seen_days],
                       key=lambda b: b["timestamp"], reverse=True)
        for b in daily[BACKUP_RETENTION_DAILY:]:
            _delete_tree(b["path"])
    except Exception as e:
        logger.error(f"Failed to prune backups: {e}")


def _delete_tree(prefix: str):
    try:
        for path in list_files(prefix):
            try:
                delete_file(path)
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"Failed to delete backup tree {prefix}: {e}")


def restore_backup(local_db_path: str, backup_prefix: str) -> int:
    """Restore a database + data files from a backup prefix.

    Returns the number of files restored. WAL/SHM sidecars are removed first so
    the server reads the restored snapshot on the next request.
    """
    if not backup_prefix or backup_prefix.startswith("/") or ".." in backup_prefix.split("/"):
        raise ValueError("Invalid backup prefix")
    files = list_files(backup_prefix)
    if not files:
        raise FileNotFoundError("Backup not found")
    restored = 0
    for path in files:
        data = retrieve_file(path)  # decrypt
        rel = path[len(backup_prefix) + 1:]
        if rel == "database/infinitychat.db":
            os.makedirs(os.path.dirname(local_db_path) or ".", exist_ok=True)
            with open(local_db_path, "wb") as f:
                f.write(data)
            for suffix in ("-wal", "-shm"):
                try:
                    if os.path.exists(local_db_path + suffix):
                        os.unlink(local_db_path + suffix)
                except Exception:
                    pass
        else:
            store_file(rel, data, encrypt=True)  # re-encrypt with current key
        restored += 1
    logger.info(f"♻️  Restored {restored} file(s) from {backup_prefix}")
    return restored


_backup_loop_started = False


def start_backup_loop(local_path: str, interval: int = 3600):
    """Background thread that creates an hourly full backup."""
    global _backup_loop_started
    if _backup_loop_started:
        return

    def _loop():
        while True:
            time.sleep(interval)
            try:
                create_timestamped_backup(local_path, "hourly")
            except Exception as e:
                logger.warning(f"⚠️  Hourly backup failed: {e}")

    _backup_loop_started = True
    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    logger.info(f"🕐 Hourly backup thread started (every {interval}s)")

def start_db_sync(local_path: str):
    """
    Start background thread that periodically uploads the database to bucket.
    """
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
    'list_backups', 'create_timestamped_backup', 'restore_backup',
    'start_backup_loop', 'DB_BUCKET_PATH', 'StorageUnavailableError'
]
