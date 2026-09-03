#!/usr/bin/env python3
"""Verify the v1 -> v2 migration upgrades an existing database without data loss."""
import asyncio
import os
import shutil
import sqlite3
import sys
import tempfile

WORK = tempfile.mkdtemp(prefix="ichat_migrate_test_")
DB = os.path.join(WORK, "infinitychat.db")

# ------------------------------------------------------------------
# 1. Build a database exactly as the OLD app (v1) created it
# ------------------------------------------------------------------
con = sqlite3.connect(DB)
con.executescript("""
CREATE TABLE users (
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
);
CREATE TABLE messages (
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
);
CREATE TABLE read_receipts (
    user_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    read_at INTEGER DEFAULT (strftime('%s','now')),
    PRIMARY KEY (user_id, message_id)
);
CREATE INDEX idx_messages_sender_time ON messages(sender_id, timestamp_ms);
CREATE INDEX idx_messages_time_id ON messages(timestamp_ms, id);
CREATE INDEX idx_messages_not_deleted ON messages(is_deleted, id);
CREATE INDEX idx_receipts_user_msg ON read_receipts(user_id, message_id);
""")

# legacy data: 2 users, 3 messages, receipts in *seconds*
con.executemany(
    "INSERT INTO users (username, display_name, password_hash, salt, token, status) VALUES (?,?,?,?,?,?)",
    [
        ("alice", "Alice", "hash1", "salt1", "tok_alice", "online"),
        ("bob", "Bobby", "hash2", "salt2", "tok_bob", "offline"),
    ]
)
# bob sends message (encrypted_content is opaque to migration)
con.execute(
    "INSERT INTO messages (sender_id, encrypted_content, timestamp_ms) VALUES (2, 'CIPHERTEXT_HELLO', 1700000000000)"
)
con.execute(
    "INSERT INTO messages (sender_id, encrypted_content, timestamp_ms) VALUES (1, 'CIPHERTEXT_WORLD', 1700000001000)"
)
# alice read bob's message at second-precision epoch 1700000002
con.execute(
    "INSERT INTO read_receipts (user_id, message_id, read_at) VALUES (1, 1, 1700000002)"
)
con.commit()
con.close()

before_rows = {}

def dump(label):
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    tables = [r["name"] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    print(f"--- {label} tables:", tables)
    for t in ("users", "messages", "read_receipts", "conversations", "conversation_members"):
        try:
            rows = [dict(r) for r in c.execute(f"SELECT * FROM {t} ORDER BY 1")]
            print(f"   {t}: {len(rows)} row(s)")
            for r in rows[:6]:
                print("     ", r)
        except sqlite3.Error as e:
            print(f"   {t}: ERROR {e}")
    c.close()

dump("BEFORE MIGRATION")
before_messages = sqlite3.connect(DB).execute("SELECT COUNT(*) FROM messages").fetchone()[0]
before_users = sqlite3.connect(DB).execute("SELECT COUNT(*) FROM users").fetchone()[0]

# ------------------------------------------------------------------
# 2. Run the v2 app initializer against the legacy DB
# ------------------------------------------------------------------
os.environ["DATABASE_URL"] = DB
os.environ["HF_TOKEN"] = ""            # offline mode
os.environ["SECRET_KEY"] = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="  # 32 bytes
os.environ["FILE_ENCRYPTION_KEY"] = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
os.environ["TEMP_DIR"] = os.path.join(WORK, "tmp")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

# import storage_handler module needs clean reload w/ env; import app fresh
import importlib
import storage_handler
importlib.reload(storage_handler)

import app as app_module
importlib.reload(app_module)

asyncio.run(app_module.init_database())
print("\ninit_database() completed without data loss errors")

dump("AFTER MIGRATION")

# ------------------------------------------------------------------
# 3. Assertions
# ------------------------------------------------------------------
con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row

failures = []

# data preserved
n_msgs = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
n_users = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
if n_msgs != before_messages:
    failures.append(f"messages lost: {before_messages} -> {n_msgs}")
if n_users != before_users:
    failures.append(f"users lost: {before_users} -> {n_users}")

# new schema pieces exist
cols = {r["name"] for r in con.execute("PRAGMA table_info(messages)")}
for need in ("conversation_id", "client_id"):
    if need not in cols:
        failures.append(f"messages missing column {need}")
convs = con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
if convs != 1:
    failures.append(f"expected seeded global conversation, found {convs}")
conv = con.execute("SELECT * FROM conversations WHERE id=1").fetchone()
if conv["type"] != "global":
    failures.append("conversation 1 is not global")

# legacy messages backfilled into the global conversation
bad_conv = con.execute(
    "SELECT COUNT(*) FROM messages WHERE conversation_id != 1 OR conversation_id IS NULL"
).fetchone()[0]
if bad_conv:
    failures.append(f"{bad_conv} messages not in global conversation")

# receipts upgraded from seconds to milliseconds
rc = con.execute("SELECT read_at FROM read_receipts WHERE user_id=1 AND message_id=1").fetchone()
if not rc or rc["read_at"] != 1700000002000:
    failures.append(f"read_at not converted to ms: {rc and rc['read_at']}")

# encrypted content untouched
msg = con.execute("SELECT encrypted_content FROM messages WHERE id=1").fetchone()
if msg["encrypted_content"] != "CIPHERTEXT_HELLO":
    failures.append("message content was modified!")

# indexes exist
idx = {r["name"] for r in con.execute("PRAGMA index_list(messages)")}
for need in ("idx_messages_client_dedupe", "idx_messages_conversation"):
    if need not in idx:
        failures.append(f"missing index {need}")

con.close()

# ------------------------------------------------------------------
# 4. Idempotency: run migration a second time (like a second deploy)
# ------------------------------------------------------------------
asyncio.run(app_module.init_database())
con = sqlite3.connect(DB)
n2 = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
c2 = con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
u2 = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
rc2 = con.execute("SELECT read_at FROM read_receipts WHERE user_id=1 AND message_id=1").fetchone()[0]
con.close()
if n2 != n_msgs or c2 != convs or u2 != n_users:
    failures.append(f"second run duplicated/lost data: msgs {n_msgs}->{n2}, convs {convs}->{c2}, users {n_users}->{u2}")
if rc2 != 1700000002000:
    failures.append(f"read_at re-multiplied on second run: {rc2}")

print("\n==== RESULT ====")
if failures:
    print("FAILURES:")
    for f in failures:
        print("  ✗", f)
    sys.exit(1)
print("✓ Migration upgraded v1 DB in place with zero data loss")
print("✓ Idempotent (second run changes nothing)")
shutil.rmtree(WORK, ignore_errors=True)
sys.exit(0)
