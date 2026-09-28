import os
import sys
import json
import time
import signal
import sqlite3
import subprocess
import threading
import ast
import shutil
import hashlib
import re
from pathlib import Path
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from concurrent.futures import ThreadPoolExecutor

import requests


# ============================================================
# KRUTIK CYBER EXPERT
# MULTI CLIENT TELEGRAM PYTHON HOSTING MANAGER
# VERSION 7.0
# ============================================================

APP_NAME = "KRUTIK CYBER EXPERT"
VERSION = "7.0"

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_CHAT_ID_RAW = os.getenv("OWNER_CHAT_ID", "").strip()

try:
    OWNER_CHAT_ID = int(OWNER_CHAT_ID_RAW)
except Exception:
    OWNER_CHAT_ID = 0


BASE_DIR = Path(__file__).resolve().parent

DATA_DIR = Path(
    os.getenv(
        "DATA_DIR",
        str(BASE_DIR / "host_data")
    )
).expanduser()

CLIENTS_DIR = DATA_DIR / "clients"
DB_FILE = DATA_DIR / "hosting.db"

DATA_DIR.mkdir(parents=True, exist_ok=True)
CLIENTS_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# RUNTIME
# ============================================================

processes = {}
process_lock = threading.RLock()

# Bot IDs intentionally stopped.
# Watcher checks this before auto restart.
stop_requested = set()

# One operation lock per bot.
bot_operation_locks = {}
bot_operation_locks_lock = threading.RLock()

# Owner input mode:
# None
# "grant"
# "revoke"
owner_input_mode = None
owner_input_lock = threading.RLock()

telegram_offset = 0

telegram_session = requests.Session()

MAX_LOG_CHARS = 12000


# ============================================================
# IMPORT -> PYPI PACKAGE MAP
# ============================================================

IMPORT_TO_PACKAGE = {
    "telegram": "python-telegram-bot==22.5",
    "telegram.ext": "python-telegram-bot==22.5",

    "openai": "openai>=1.50.0,<2",

    "requests": "requests>=2.31.0",
    "httpx": "httpx",
    "aiohttp": "aiohttp",

    "flask": "Flask",
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",

    "bs4": "beautifulsoup4",
    "PIL": "Pillow",
    "cv2": "opencv-python",

    "dotenv": "python-dotenv",
    "yaml": "PyYAML",

    "Crypto": "pycryptodome",

    "numpy": "numpy",
    "pandas": "pandas",

    "qrcode": "qrcode",
    "schedule": "schedule",
    "rich": "rich",
    "colorama": "colorama",

    "selenium": "selenium",
    "jwt": "PyJWT",

    "google": "google-api-python-client",

    "discord": "discord.py",

    "psutil": "psutil",

    "dateutil": "python-dateutil",
    "sklearn": "scikit-learn",
    "matplotlib": "matplotlib",
    "bs4": "beautifulsoup4",
}


# ============================================================
# STANDARD LIBRARY
# ============================================================

STDLIB_MODULES = set(
    getattr(sys, "stdlib_module_names", set())
)

STDLIB_MODULES.update({
    "os",
    "sys",
    "re",
    "json",
    "time",
    "math",
    "random",
    "datetime",
    "calendar",
    "sqlite3",
    "subprocess",
    "threading",
    "signal",
    "pathlib",
    "typing",
    "asyncio",
    "logging",
    "traceback",
    "collections",
    "itertools",
    "functools",
    "statistics",
    "hashlib",
    "secrets",
    "uuid",
    "base64",
    "urllib",
    "http",
    "email",
    "socket",
    "ssl",
    "csv",
    "io",
    "tempfile",
    "shutil",
    "zipfile",
    "glob",
    "inspect",
    "dataclasses",
    "enum",
    "argparse",
    "configparser",
    "copy",
    "pickle",
    "struct",
    "string",
    "textwrap",
    "warnings",
    "platform",
    "timeit",
    "decimal",
    "fractions",
    "functools",
    "operator",
    "weakref",
    "queue",
    "concurrent",
    "multiprocessing",
    "unittest",
})


# ============================================================
# DATABASE
# ============================================================

db_lock = threading.RLock()


def get_db():
    conn = sqlite3.connect(
        DB_FILE,
        timeout=30,
        check_same_thread=False
    )

    conn.row_factory = sqlite3.Row

    return conn


def now():
    return datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def init_db():
    with db_lock:
        conn = get_db()

        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS clients (
                    chat_id INTEGER PRIMARY KEY,
                    username TEXT DEFAULT '',
                    first_name TEXT DEFAULT '',
                    last_name TEXT DEFAULT '',
                    enabled INTEGER DEFAULT 1,
                    created_at TEXT DEFAULT '',
                    last_seen TEXT DEFAULT ''
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS bots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_chat_id INTEGER NOT NULL,
                    name TEXT DEFAULT '',
                    filename TEXT DEFAULT '',
                    folder TEXT DEFAULT '',
                    status TEXT DEFAULT 'stopped',
                    pid INTEGER DEFAULT 0,
                    auto_restart INTEGER DEFAULT 1,
                    created_at TEXT DEFAULT '',
                    updated_at TEXT DEFAULT ''
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS hosting_access (
                    chat_id INTEGER PRIMARY KEY,
                    username TEXT DEFAULT '',
                    first_name TEXT DEFAULT '',
                    last_name TEXT DEFAULT '',
                    enabled INTEGER DEFAULT 1,
                    created_at TEXT DEFAULT '',
                    updated_at TEXT DEFAULT ''
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS global_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT DEFAULT ''
                )
            """)

            conn.execute("""
                INSERT OR IGNORE INTO global_settings
                (key, value)
                VALUES ('global_client_lock', '0')
            """)

            conn.commit()

        finally:
            conn.close()


def is_global_client_lock():
    with db_lock:
        conn = get_db()

        try:
            row = conn.execute("""
                SELECT value
                FROM global_settings
                WHERE key=?
            """, (
                "global_client_lock",
            )).fetchone()

            if not row:
                return False

            return str(row["value"]) == "1"

        finally:
            conn.close()


def set_global_client_lock(locked):
    with db_lock:
        conn = get_db()

        try:
            conn.execute("""
                INSERT INTO global_settings
                (key, value)
                VALUES (?, ?)
                ON CONFLICT(key)
                DO UPDATE SET value=excluded.value
            """, (
                "global_client_lock",
                "1" if locked else "0"
            ))

            conn.commit()

        finally:
            conn.close()


# ============================================================
# TELEGRAM
# ============================================================

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"


def telegram(method, data=None, timeout=60):
    try:
        response = telegram_session.post(
            f"{TG_API}/{method}",
            data=data or {},
            timeout=timeout
        )

        if not response.ok:
            print(
                f"[TELEGRAM HTTP {response.status_code}] "
                f"{method}: {response.text[:500]}"
            )
            return None

        try:
            return response.json()
        except Exception:
            return None

    except Exception as e:
        print(
            f"[TELEGRAM ERROR] {method}: {e}"
        )
        return None


def send_message(
    chat_id,
    text,
    reply_markup=None
):
    data = {
        "chat_id": int(chat_id),
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }

    if reply_markup is not None:
        data["reply_markup"] = json.dumps(
            reply_markup,
            ensure_ascii=False
        )

    return telegram(
        "sendMessage",
        data,
        timeout=30
    )


def edit_message(
    chat_id,
    message_id,
    text,
    reply_markup=None
):
    data = {
        "chat_id": int(chat_id),
        "message_id": int(message_id),
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }

    if reply_markup is not None:
        data["reply_markup"] = json.dumps(
            reply_markup,
            ensure_ascii=False
        )

    return telegram(
        "editMessageText",
        data,
        timeout=30
    )


def answer_callback(
    callback_id,
    text=""
):
    if not callback_id:
        return

    return telegram(
        "answerCallbackQuery",
        {
            "callback_query_id": callback_id,
            "text": str(text)[:190],
            "show_alert": False
        },
        timeout=20
    )


def escape_html(value):
    if value is None:
        return ""

    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def send_long_message(
    chat_id,
    text,
    reply_markup=None
):
    limit = 3800

    if len(text) <= limit:
        return send_message(
            chat_id,
            text,
            reply_markup
        )

    chunks = [
        text[i:i + limit]
        for i in range(
            0,
            len(text),
            limit
        )
    ]

    for index, chunk in enumerate(chunks):

        send_message(
            chat_id,
            chunk,
            reply_markup
            if index == len(chunks) - 1
            else None
        )


# ============================================================
# USER
# ============================================================

def user_info(user):
    return {
        "chat_id": int(user.get("id", 0)),
        "username": user.get("username", "") or "",
        "first_name": user.get("first_name", "") or "",
        "last_name": user.get("last_name", "") or ""
    }


def display_username(row):
    if not row:
        return "Unknown"

    username = row["username"] or ""

    if username:
        return "@" + str(username)

    name = (
        f"{row['first_name'] or ''} "
        f"{row['last_name'] or ''}"
    ).strip()

    return name or str(row["chat_id"])


def is_owner(chat_id):
    try:
        return int(chat_id) == OWNER_CHAT_ID
    except Exception:
        return False


# ============================================================
# HOSTING ACCESS
# ============================================================

def has_hosting_access(chat_id):
    chat_id = int(chat_id)

    if is_owner(chat_id):
        return True

    if is_global_client_lock():
        return False

    with db_lock:
        conn = get_db()

        try:
            row = conn.execute("""
                SELECT enabled
                FROM hosting_access
                WHERE chat_id=?
            """, (
                chat_id,
            )).fetchone()

            return bool(
                row and
                int(row["enabled"]) == 1
            )

        finally:
            conn.close()


def grant_hosting_access_by_id(
    chat_id,
    username="",
    first_name="",
    last_name=""
):
    chat_id = int(chat_id)

    with db_lock:
        conn = get_db()

        try:
            conn.execute("""
                INSERT INTO hosting_access
                (
                    chat_id,
                    username,
                    first_name,
                    last_name,
                    enabled,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, 1, ?, ?)

                ON CONFLICT(chat_id)
                DO UPDATE SET
                    username=excluded.username,
                    first_name=excluded.first_name,
                    last_name=excluded.last_name,
                    enabled=1,
                    updated_at=excluded.updated_at
            """, (
                chat_id,
                username,
                first_name,
                last_name,
                now(),
                now()
            ))

            conn.commit()

        finally:
            conn.close()


def revoke_hosting_access(chat_id):
    chat_id = int(chat_id)

    if is_owner(chat_id):
        return False

    with db_lock:
        conn = get_db()

        try:
            cursor = conn.execute("""
                UPDATE hosting_access
                SET enabled=0,
                    updated_at=?
                WHERE chat_id=?
            """, (
                now(),
                chat_id
            ))

            conn.commit()

            return cursor.rowcount > 0

        finally:
            conn.close()


def get_access_users():
    with db_lock:
        conn = get_db()

        try:
            return conn.execute("""
                SELECT *
                FROM hosting_access
                ORDER BY updated_at DESC
            """).fetchall()

        finally:
            conn.close()


# ============================================================
# CLIENT DATABASE
# ============================================================

def upsert_client(user):
    info = user_info(user)

    if not info["chat_id"]:
        return False

    created = False

    with db_lock:
        conn = get_db()

        try:
            existing = conn.execute("""
                SELECT chat_id
                FROM clients
                WHERE chat_id=?
            """, (
                info["chat_id"],
            )).fetchone()

            if existing is None:

                created = True

                conn.execute("""
                    INSERT INTO clients
                    (
                        chat_id,
                        username,
                        first_name,
                        last_name,
                        enabled,
                        created_at,
                        last_seen
                    )
                    VALUES (?, ?, ?, ?, 1, ?, ?)
                """, (
                    info["chat_id"],
                    info["username"],
                    info["first_name"],
                    info["last_name"],
                    now(),
                    now()
                ))

            else:

                conn.execute("""
                    UPDATE clients
                    SET username=?,
                        first_name=?,
                        last_name=?,
                        last_seen=?
                    WHERE chat_id=?
                """, (
                    info["username"],
                    info["first_name"],
                    info["last_name"],
                    now(),
                    info["chat_id"]
                ))

            conn.commit()

        finally:
            conn.close()

    if created and not is_owner(
        info["chat_id"]
    ):
        notify_owner_new_client(info)

    return created


def get_client(chat_id):
    with db_lock:
        conn = get_db()

        try:
            return conn.execute("""
                SELECT *
                FROM clients
                WHERE chat_id=?
            """, (
                int(chat_id),
            )).fetchone()

        finally:
            conn.close()


def get_clients():
    with db_lock:
        conn = get_db()

        try:
            return conn.execute("""
                SELECT *
                FROM clients
                ORDER BY created_at DESC
            """).fetchall()

        finally:
            conn.close()


def set_client_enabled(
    chat_id,
    enabled
):
    chat_id = int(chat_id)

    with db_lock:
        conn = get_db()

        try:
            conn.execute("""
                UPDATE clients
                SET enabled=?
                WHERE chat_id=?
            """, (
                1 if enabled else 0,
                chat_id
            ))

            conn.commit()

        finally:
            conn.close()

    if not enabled:
        stop_all_bots_of_client(
            chat_id
        )


# ============================================================
# BOT DATABASE
# ============================================================

def create_bot(
    owner_chat_id,
    name,
    filename,
    folder
):
    with db_lock:
        conn = get_db()

        try:
            cursor = conn.execute("""
                INSERT INTO bots
                (
                    owner_chat_id,
                    name,
                    filename,
                    folder,
                    status,
                    pid,
                    auto_restart,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, 'stopped', 0, 1, ?, ?)
            """, (
                int(owner_chat_id),
                name,
                filename,
                folder,
                now(),
                now()
            ))

            conn.commit()

            return int(cursor.lastrowid)

        finally:
            conn.close()


def get_bot(bot_id):
    with db_lock:
        conn = get_db()

        try:
            return conn.execute("""
                SELECT *
                FROM bots
                WHERE id=?
            """, (
                int(bot_id),
            )).fetchone()

        finally:
            conn.close()


def get_bots(owner_chat_id=None):
    with db_lock:
        conn = get_db()

        try:

            if owner_chat_id is None:

                return conn.execute("""
                    SELECT *
                    FROM bots
                    ORDER BY id DESC
                """).fetchall()

            return conn.execute("""
                SELECT *
                FROM bots
                WHERE owner_chat_id=?
                ORDER BY id DESC
            """, (
                int(owner_chat_id),
            )).fetchall()

        finally:
            conn.close()


def update_bot(
    bot_id,
    **fields
):
    allowed = {
        "name",
        "filename",
        "folder",
        "status",
        "pid",
        "auto_restart",
        "updated_at"
    }

    clean = {}

    for key, value in fields.items():

        if key in allowed:
            clean[key] = value

    if not clean:
        return

    clean["updated_at"] = now()

    assignments = []
    values = []

    for key, value in clean.items():

        assignments.append(
            f"{key}=?"
        )

        values.append(value)

    values.append(int(bot_id))

    with db_lock:
        conn = get_db()

        try:
            conn.execute(
                f"""
                UPDATE bots
                SET {", ".join(assignments)}
                WHERE id=?
                """,
                values
            )

            conn.commit()

        finally:
            conn.close()


# ============================================================
# BOT OPERATION LOCK
# ============================================================

def get_bot_operation_lock(bot_id):
    bot_id = int(bot_id)

    with bot_operation_locks_lock:

        lock = bot_operation_locks.get(
            bot_id
        )

        if lock is None:

            lock = threading.RLock()

            bot_operation_locks[
                bot_id
            ] = lock

        return lock


# ============================================================
# FILE PATHS
# ========
