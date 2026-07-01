"""Persistência de usuários e mensagens — SQLite local ou Vercel Blob em produção."""

from __future__ import annotations

import os
import sqlite3
import threading
from copy import deepcopy
from datetime import datetime, timezone

from utils import blob_client

_LOCK = threading.Lock()
_BLOB_PATH = blob_client.DEFAULT_PATHNAME

_DATA_ROOT = "/tmp" if os.environ.get("VERCEL") else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SQLITE_DB = os.path.join(_DATA_ROOT, "database", "chat.db")

_store: dict | None = None
_store_etag: str | None = None


def _empty_store() -> dict:
    return {
        "users": [],
        "messages": [],
        "next_user_id": 1,
        "next_message_id": 1,
    }


def _use_blob() -> bool:
    return blob_client.blob_enabled()


def _import_sqlite_into_store(conn: sqlite3.Connection) -> dict:
    store = _empty_store()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('users', 'messages')"
    )
    tables = {row[0] for row in cur.fetchall()}

    if "users" in tables:
        cur.execute("SELECT id, username, password_hash, created_at FROM users ORDER BY id")
        for row in cur.fetchall():
            store["users"].append(
                {
                    "id": row["id"],
                    "username": row["username"],
                    "password_hash": row["password_hash"],
                    "created_at": row["created_at"] or _now_iso(),
                }
            )
        if store["users"]:
            store["next_user_id"] = max(u["id"] for u in store["users"]) + 1

    if "messages" in tables:
        cur.execute("SELECT id, username, message FROM messages ORDER BY id")
        for row in cur.fetchall():
            store["messages"].append(
                {
                    "id": row["id"],
                    "username": row["username"],
                    "message": row["message"],
                }
            )
        if store["messages"]:
            store["next_message_id"] = max(m["id"] for m in store["messages"]) + 1

    return store


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _load_blob_store() -> dict:
    global _store, _store_etag
    data, etag = blob_client.get_json(_BLOB_PATH)
    if data is None:
        if os.path.isfile(SQLITE_DB):
            os.makedirs(os.path.dirname(SQLITE_DB), exist_ok=True)
            with sqlite3.connect(SQLITE_DB) as conn:
                data = _import_sqlite_into_store(conn)
            blob_client.put_json(_BLOB_PATH, data)
            _store = data
            _store_etag = None
            return deepcopy(_store)
        data = _empty_store()
        blob_client.put_json(_BLOB_PATH, data)
    _store = data
    _store_etag = etag
    return deepcopy(_store)


def _save_blob_store(store: dict) -> None:
    global _store, _store_etag
    try:
        result = blob_client.put_json(_BLOB_PATH, store, if_match=_store_etag)
    except RuntimeError as exc:
        if "precondition" in str(exc).lower() or "412" in str(exc):
            fresh, etag = blob_client.get_json(_BLOB_PATH)
            if fresh is None:
                fresh = _empty_store()
            _store = fresh
            _store_etag = etag
            raise
        raise
    _store = store
    _store_etag = result.get("etag")


def _with_store(mutator):
    global _store, _store_etag
    with _LOCK:
        if _use_blob():
            if _store is None:
                _load_blob_store()
            store = deepcopy(_store)
            result = mutator(store)
            _save_blob_store(store)
            return result

        return mutator_sqlite(mutator)


def mutator_sqlite(mutator):
    os.makedirs(os.path.dirname(SQLITE_DB), exist_ok=True)
    init_sqlite()
    conn = sqlite3.connect(SQLITE_DB)
    conn.row_factory = sqlite3.Row
    try:
        store = _import_sqlite_into_store(conn)
        result = mutator(store)
        _write_sqlite_from_store(conn, store)
        conn.commit()
        return result
    finally:
        conn.close()


def _write_sqlite_from_store(conn: sqlite3.Connection, store: dict) -> None:
    cur = conn.cursor()
    cur.execute("DELETE FROM users")
    cur.execute("DELETE FROM messages")
    for user in store["users"]:
        cur.execute(
            "INSERT INTO users (id, username, password_hash, created_at) VALUES (?, ?, ?, ?)",
            (user["id"], user["username"], user["password_hash"], user.get("created_at")),
        )
    for msg in store["messages"]:
        cur.execute(
            "INSERT INTO messages (id, username, message) VALUES (?, ?, ?)",
            (msg["id"], msg["username"], msg["message"]),
        )


def init_sqlite():
    os.makedirs(os.path.dirname(SQLITE_DB), exist_ok=True)
    conn = sqlite3.connect(SQLITE_DB)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            message TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    conn.close()


def init_db():
    if _use_blob():
        with _LOCK:
            _load_blob_store()
    else:
        init_sqlite()


def get_user_by_id(user_id):
    if not user_id:
        return None

    def find(store):
        for user in store["users"]:
            if user["id"] == user_id:
                return dict(user)
        return None

    if _use_blob():
        with _LOCK:
            if _store is None:
                _load_blob_store()
            return find(_store)
    init_sqlite()
    conn = sqlite3.connect(SQLITE_DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT id, username, password_hash FROM users WHERE id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def get_user_by_username(username):
    if not username:
        return None
    username = username.strip()

    if _use_blob():
        with _LOCK:
            if _store is None:
                _load_blob_store()
            for user in _store["users"]:
                if user["username"] == username:
                    return dict(user)
        return None

    init_sqlite()
    conn = sqlite3.connect(SQLITE_DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT id, username, password_hash FROM users WHERE username = ?", (username,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def create_user(username: str, password_hash: str) -> int:
    def mutate(store):
        user_id = store["next_user_id"]
        store["next_user_id"] += 1
        store["users"].append(
            {
                "id": user_id,
                "username": username,
                "password_hash": password_hash,
                "created_at": _now_iso(),
            }
        )
        return user_id

    return _with_store(mutate)


def update_user(user_id: int, *, username: str | None = None, password_hash: str | None = None) -> None:
    def mutate(store):
        for user in store["users"]:
            if user["id"] != user_id:
                continue
            if username is not None:
                user["username"] = username
            if password_hash is not None:
                user["password_hash"] = password_hash
            return
        raise KeyError(user_id)

    _with_store(mutate)


def delete_user(user_id: int) -> None:
    def mutate(store):
        store["users"] = [u for u in store["users"] if u["id"] != user_id]
        return

    _with_store(mutate)


def insert_message(username: str, message: str) -> None:
    def mutate(store):
        msg_id = store["next_message_id"]
        store["next_message_id"] += 1
        store["messages"].append(
            {"id": msg_id, "username": username, "message": message}
        )
        return

    _with_store(mutate)
