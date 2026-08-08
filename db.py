"""
SQLite storage. Five things live here:

  1. chats       — every group/channel the bot has been added to (the
                   auto-tracked dump destinations), plus a per-chat pause
                   switch and optional forum topic id.
  2. jobs +      — which leech is running and which files of it are already
     done_files    finished, so an interrupted job resumes instead of
                   starting over.
  3. settings    — the runtime-editable /settings values.
  4. users       — sudo users added at runtime.
  5. stats       — lifetime counters shown by /stats.

SQLite calls are synchronous, but each one here is tiny (milliseconds), so we
guard them with a lock and call them directly from the async code.

Schema changes are additive: `_add_column` quietly upgrades a database
created by an older version of the bot, so redeploying never needs a wipe.
"""
import sqlite3
import threading
import time

import config

_lock = threading.Lock()
_conn = None  # type: sqlite3.Connection | None


def _add_column(table: str, column: str, decl: str) -> None:
    """Add a column if this database predates it. Safe to run every boot."""
    existing = {r["name"] for r in _conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        _conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


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
                active    INTEGER DEFAULT 1   -- bot is still a member
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

            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS users (
                user_id  INTEGER PRIMARY KEY,
                added_by INTEGER,
                added_at INTEGER
            );

            CREATE TABLE IF NOT EXISTS stats (
                key   TEXT PRIMARY KEY,
                value INTEGER DEFAULT 0
            );
            """
        )
        # --- additive migrations for databases created by earlier versions ---
        _add_column("chats", "enabled", "INTEGER DEFAULT 1")   # paused by the user
        _add_column("chats", "added_at", "INTEGER")
        _add_column("jobs", "title", "TEXT")
        _add_column("jobs", "total_files", "INTEGER DEFAULT 0")
        _add_column("jobs", "done_count", "INTEGER DEFAULT 0")
        _add_column("jobs", "bytes_sent", "INTEGER DEFAULT 0")
        _add_column("jobs", "finished_at", "INTEGER")
        _add_column("jobs", "error", "TEXT")
        _add_column("done_files", "size", "INTEGER DEFAULT 0")
        _conn.commit()

        # First-run defaults for anything the settings table doesn't have yet.
        for key, value in config.DEFAULT_SETTINGS.items():
            _conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, str(value)),
            )
        _conn.commit()


# ----------------------------- settings -----------------------------
def _coerce(key, raw):
    """Settings are stored as text; give ints back as ints."""
    default = config.DEFAULT_SETTINGS.get(key)
    if isinstance(default, int):
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default
    return raw


def get_settings() -> dict:
    with _lock:
        rows = _conn.execute("SELECT key, value FROM settings").fetchall()
    out = dict(config.DEFAULT_SETTINGS)
    for r in rows:
        out[r["key"]] = _coerce(r["key"], r["value"])
    return out


def get_setting(key):
    return get_settings().get(key)


def set_setting(key, value) -> None:
    with _lock:
        _conn.execute(
            """INSERT INTO settings (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (key, str(value)),
        )
        _conn.commit()


def reset_settings() -> None:
    with _lock:
        for key, value in config.DEFAULT_SETTINGS.items():
            _conn.execute(
                """INSERT INTO settings (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (key, str(value)),
            )
        _conn.commit()


# ------------------------------ users -------------------------------
def add_user(user_id, added_by) -> None:
    with _lock:
        _conn.execute(
            "INSERT OR REPLACE INTO users (user_id, added_by, added_at) VALUES (?, ?, ?)",
            (int(user_id), int(added_by), int(time.time())),
        )
        _conn.commit()


def remove_user(user_id) -> bool:
    with _lock:
        cur = _conn.execute("DELETE FROM users WHERE user_id=?", (int(user_id),))
        _conn.commit()
    return cur.rowcount > 0


def get_users():
    with _lock:
        return _conn.execute("SELECT * FROM users ORDER BY added_at").fetchall()


def get_user_ids():
    return {r["user_id"] for r in get_users()}


# ----------------------------- chats -----------------------------
def add_or_update_chat(chat_id, title, chat_type) -> None:
    with _lock:
        _conn.execute(
            """INSERT INTO chats (chat_id, title, type, active, enabled, added_at)
               VALUES (?, ?, ?, 1, 1, ?)
               ON CONFLICT(chat_id) DO UPDATE SET
                 title=excluded.title, type=excluded.type, active=1""",
            (chat_id, title, chat_type, int(time.time())),
        )
        _conn.commit()


def deactivate_chat(chat_id) -> None:
    with _lock:
        _conn.execute("UPDATE chats SET active=0 WHERE chat_id=?", (chat_id,))
        _conn.commit()


def get_active_chats():
    """Chats the bot is in AND that the user hasn't paused — the dump list."""
    with _lock:
        return _conn.execute(
            "SELECT * FROM chats WHERE active=1 AND enabled=1 ORDER BY title"
        ).fetchall()


def get_all_chats():
    """Every chat the bot is still a member of, paused ones included."""
    with _lock:
        return _conn.execute(
            "SELECT * FROM chats WHERE active=1 ORDER BY title"
        ).fetchall()


def get_chat(chat_id):
    with _lock:
        return _conn.execute(
            "SELECT * FROM chats WHERE chat_id=?", (int(chat_id),)
        ).fetchone()


def toggle_chat(chat_id) -> bool:
    """Flip a chat's pause switch; returns the new enabled state."""
    with _lock:
        row = _conn.execute(
            "SELECT enabled FROM chats WHERE chat_id=?", (int(chat_id),)
        ).fetchone()
        if row is None:
            return False
        new = 0 if row["enabled"] else 1
        _conn.execute(
            "UPDATE chats SET enabled=? WHERE chat_id=?", (new, int(chat_id))
        )
        _conn.commit()
    return bool(new)


def set_chat_thread(chat_id, thread_id) -> None:
    with _lock:
        _conn.execute(
            "UPDATE chats SET thread_id=? WHERE chat_id=?", (thread_id, int(chat_id))
        )
        _conn.commit()


def set_chat_title(chat_id, title) -> None:
    with _lock:
        _conn.execute(
            "UPDATE chats SET title=? WHERE chat_id=?", (title, int(chat_id))
        )
        _conn.commit()


# ------------------------- jobs / resume -------------------------
def create_job(job_id, mega_url, requested_by, title=None) -> None:
    with _lock:
        _conn.execute(
            """INSERT OR REPLACE INTO jobs
               (job_id, mega_url, title, status, requested_by, created_at)
               VALUES (?, ?, ?, 'running', ?, ?)""",
            (job_id, mega_url, title, requested_by, int(time.time())),
        )
        _conn.commit()


def set_job_status(job_id, status, error=None) -> None:
    with _lock:
        _conn.execute(
            "UPDATE jobs SET status=?, error=?, finished_at=? WHERE job_id=?",
            (status, error, int(time.time()), job_id),
        )
        _conn.commit()


def set_job_totals(job_id, total_files) -> None:
    with _lock:
        _conn.execute(
            "UPDATE jobs SET total_files=? WHERE job_id=?", (total_files, job_id)
        )
        _conn.commit()


def get_job(job_id):
    with _lock:
        return _conn.execute(
            "SELECT * FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()


def get_running_jobs():
    with _lock:
        return _conn.execute(
            "SELECT * FROM jobs WHERE status='running' ORDER BY created_at"
        ).fetchall()


def get_recent_jobs(limit=10):
    with _lock:
        return _conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()


def mark_file_done(job_id, file_path, size=0) -> None:
    with _lock:
        _conn.execute(
            "INSERT OR IGNORE INTO done_files (job_id, file_path, size) VALUES (?, ?, ?)",
            (job_id, file_path, int(size)),
        )
        _conn.execute(
            """UPDATE jobs
                  SET done_count = (SELECT COUNT(*) FROM done_files WHERE job_id=?),
                      bytes_sent = COALESCE(bytes_sent, 0) + ?
                WHERE job_id=?""",
            (job_id, int(size), job_id),
        )
        _conn.commit()


def is_file_done(job_id, file_path) -> bool:
    with _lock:
        row = _conn.execute(
            "SELECT 1 FROM done_files WHERE job_id=? AND file_path=?",
            (job_id, file_path),
        ).fetchone()
    return row is not None


def done_file_count(job_id) -> int:
    with _lock:
        row = _conn.execute(
            "SELECT COUNT(*) AS n FROM done_files WHERE job_id=?", (job_id,)
        ).fetchone()
    return row["n"] if row else 0


# ------------------------------ stats -------------------------------
def bump(key, amount=1) -> None:
    with _lock:
        _conn.execute(
            """INSERT INTO stats (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = value + excluded.value""",
            (key, int(amount)),
        )
        _conn.commit()


def get_stats() -> dict:
    with _lock:
        rows = _conn.execute("SELECT key, value FROM stats").fetchall()
    return {r["key"]: r["value"] for r in rows}
