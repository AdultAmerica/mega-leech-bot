"""
Tiny SQLite layer that stores two things:

  1. chats       — every group/channel the bot has been added to (the
                   auto-tracked dump destinations).
  2. jobs +      — which leech is running and which files of it are already
     done_files    finished, so an interrupted job resumes instead of
                   starting over.

SQLite calls are synchronous, but each one here is tiny (milliseconds), so we
guard writes with a lock and call them directly from the async code.
"""
import sqlite3
import threading
import time

import config

_lock = threading.Lock()
_conn = None  # type: sqlite3.Connection | None


def init_db() -> None:
    global _conn
    _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    with _lock:
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS chats (
                chat_id   INTEGER PRIMARY KEY,
                title     TEXT,
                type      TEXT,
                thread_id INTEGER,            -- optional forum topic id
                active    INTEGER DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS jobs (
                job_id       TEXT PRIMARY KEY,
                mega_url     TEXT,
                status       TEXT,            -- running | done | error | cancelled
                requested_by INTEGER,
                created_at   INTEGER
            );

            CREATE TABLE IF NOT EXISTS done_files (
                job_id    TEXT,
                file_path TEXT,
                PRIMARY KEY (job_id, file_path)
            );
            """
        )
        _conn.commit()


# ----------------------------- chats -----------------------------
def add_or_update_chat(chat_id, title, chat_type) -> None:
    with _lock:
        _conn.execute(
            """INSERT INTO chats (chat_id, title, type, active)
               VALUES (?, ?, ?, 1)
               ON CONFLICT(chat_id) DO UPDATE SET
                 title=excluded.title, type=excluded.type, active=1""",
            (chat_id, title, chat_type),
        )
        _conn.commit()


def deactivate_chat(chat_id) -> None:
    with _lock:
        _conn.execute("UPDATE chats SET active=0 WHERE chat_id=?", (chat_id,))
        _conn.commit()


def get_active_chats():
    with _lock:
        return _conn.execute(
            "SELECT * FROM chats WHERE active=1 ORDER BY title"
        ).fetchall()


def set_chat_thread(chat_id, thread_id) -> None:
    with _lock:
        _conn.execute(
            "UPDATE chats SET thread_id=? WHERE chat_id=?", (thread_id, chat_id)
        )
        _conn.commit()


# ------------------------- jobs / resume -------------------------
def create_job(job_id, mega_url, requested_by) -> None:
    with _lock:
        _conn.execute(
            """INSERT OR REPLACE INTO jobs
               (job_id, mega_url, status, requested_by, created_at)
               VALUES (?, ?, 'running', ?, ?)""",
            (job_id, mega_url, requested_by, int(time.time())),
        )
        _conn.commit()


def set_job_status(job_id, status) -> None:
    with _lock:
        _conn.execute("UPDATE jobs SET status=? WHERE job_id=?", (status, job_id))
        _conn.commit()


def get_running_jobs():
    with _lock:
        return _conn.execute(
            "SELECT * FROM jobs WHERE status='running' ORDER BY created_at"
        ).fetchall()


def mark_file_done(job_id, file_path) -> None:
    with _lock:
        _conn.execute(
            "INSERT OR IGNORE INTO done_files (job_id, file_path) VALUES (?, ?)",
            (job_id, file_path),
        )
        _conn.commit()


def is_file_done(job_id, file_path) -> bool:
    with _lock:
        row = _conn.execute(
            "SELECT 1 FROM done_files WHERE job_id=? AND file_path=?",
            (job_id, file_path),
        ).fetchone()
    return row is not None
