"""
Configuration loader.

Every secret and hard setting comes from an environment variable. For local
testing, put them in a .env file (which is gitignored so it never reaches
GitHub). On the VPS, docker-compose passes the same file to the container
with `env_file:`, so the identical code runs in both places.

Soft settings (the ones you can flip at runtime from the bot's /settings
panel) live in SQLite instead — the DEFAULT_SETTINGS dict below is only the
starting value used the first time the bot ever runs.
"""
import os

from dotenv import load_dotenv

# Loads variables from a .env file if one is present (local development, or
# when compose passes one into the container).
load_dotenv()


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"Set it in your .env file."
        )
    return value


def _int_set(name: str):
    """Parse a comma/space separated list of ids, e.g. EXTRA_OWNERS=1,2 3."""
    out = set()
    for chunk in os.environ.get(name, "").replace(",", " ").split():
        try:
            out.add(int(chunk))
        except ValueError:
            continue
    return out


# --- Telegram app credentials (https://my.telegram.org -> API development tools) ---
API_ID = int(_require("API_ID"))
API_HASH = _require("API_HASH")

# --- Bot token (from @BotFather) ---
BOT_TOKEN = _require("BOT_TOKEN")

# --- Telegram user ids allowed to control the bot.
#     OWNER_ID is the primary owner: full control, including managing users
#     and restarting. EXTRA_OWNERS is an optional comma-separated list of
#     additional ids with the same day-to-day powers. SUDO_USERS is a softer
#     tier that can run jobs and change settings but not manage users; more
#     can be added at runtime with /users, which stores them in the database. ---
OWNER_ID = int(_require("OWNER_ID"))
OWNER_IDS = {OWNER_ID} | _int_set("EXTRA_OWNERS")
SUDO_USERS = _int_set("SUDO_USERS")

# --- MEGA account credentials ---
MEGA_EMAIL = _require("MEGA_EMAIL")
MEGA_PASSWORD = _require("MEGA_PASSWORD")

# --- Working directory.
#     compose mounts a named volume here, so the database, the MEGAcmd
#     session and any in-progress downloads survive restarts/redeploys. ---
DATA_DIR = os.environ.get("DATA_DIR", "/data")
DOWNLOAD_DIR = os.path.join(DATA_DIR, "downloads")
DB_PATH = os.path.join(DATA_DIR, "leech.db")
LOG_PATH = os.path.join(DATA_DIR, "bot.log")
THUMB_PATH = os.path.join(DATA_DIR, "thumb.jpg")

# A Pyrogram/Kurigram bot can upload up to 2 GB per file over MTProto.
# We split a little under that to stay safe. (Runtime-overridable in /settings.)
MAX_PART_SIZE = 1900 * 1024 * 1024          # 1900 MB
SPLIT_READ_CHUNK = 16 * 1024 * 1024         # read 16 MB at a time while splitting

# Port for the tiny health-check web server. compose doesn't publish it, so
# it's only reachable from inside the container unless you map it.
PORT = int(os.environ.get("PORT", "8080"))

# Bot display name used in panel headers. Handy when you run two bots.
BRAND = os.environ.get("BRAND", "MEGA LEECH")

# --- Runtime settings: editable live from /settings, persisted in SQLite. ----
# Keys are also the storage keys; values here are only first-run defaults.
DEFAULT_SETTINGS = {
    # visual
    "bar_style": "blocks",         # see ui.BAR_STYLES
    "bar_length": 14,              # characters in a progress bar
    "refresh_secs": 6,             # how often the live dashboard redraws
    "animate": 1,                  # spinner animation in the dashboard header
    # upload behaviour
    "upload_mode": "media",        # media (video/audio get players) | document
    "split_size": MAX_PART_SIZE,   # bytes per part for oversized files
    "silent": 0,                   # send with notifications disabled
    "fanout": 1,                   # copy the message to every other chat
    "use_thumb": 0,                # attach the saved thumbnail when present
    # captions
    "caption_prefix": "",
    "caption_suffix": "",
    "caption_style": "mono",       # mono | bold | plain
    # reliability
    "retries": 3,                  # per-file retry attempts before giving up
    "notify_done": 1,              # DM the requester when a job finishes
}

# Make sure the working folders exist (this also creates DATA_DIR).
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
