"""
Configuration loader.

Every secret and setting comes from an environment variable. For local
testing, put them in a .env file (which is gitignored so it never reaches
GitHub). On the VPS, the same .env file is passed to `docker run --env-file`,
so the identical code runs in both places.
"""
import os

from dotenv import load_dotenv

# Loads variables from a .env file if one is present (local development or
# when passed into the container via --env-file).
load_dotenv()


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"Set it in your .env file."
        )
    return value


# --- Telegram app credentials (https://my.telegram.org -> API development tools) ---
API_ID = int(_require("API_ID"))
API_HASH = _require("API_HASH")

# --- Bot token (from @BotFather) ---
BOT_TOKEN = _require("BOT_TOKEN")

# --- Your numeric Telegram user id (from @userinfobot).
#     Only this user is allowed to issue commands to the bot. ---
OWNER_ID = int(_require("OWNER_ID"))

# --- MEGA account credentials ---
MEGA_EMAIL = _require("MEGA_EMAIL")
MEGA_PASSWORD = _require("MEGA_PASSWORD")

# --- Working directory.
#     On the VPS, mount a host folder to this path with `docker run -v` so the
#     database and any in-progress downloads survive restarts/redeploys. ---
DATA_DIR = os.environ.get("DATA_DIR", "/data")
DOWNLOAD_DIR = os.path.join(DATA_DIR, "downloads")
DB_PATH = os.path.join(DATA_DIR, "leech.db")

# A Pyrogram/Kurigram bot can upload up to 2 GB per file over MTProto.
# We split a little under that to stay safe.
MAX_PART_SIZE = 1900 * 1024 * 1024          # 1900 MB
SPLIT_READ_CHUNK = 16 * 1024 * 1024         # read 16 MB at a time while splitting

# Port for the tiny health-check web server.
PORT = int(os.environ.get("PORT", "8080"))

# Make sure the working folders exist (this also creates DATA_DIR).
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
