"""
MEGA -> Telegram leech bot.

Everything the user touches lives here: commands, inline-button callbacks, and
the membership tracker that keeps the destination list current. The actual
work happens elsewhere — tasks.py runs the pipeline, ui.py draws the panels,
keyboards.py builds the buttons — so this file stays a routing table.

How it works:
  - The bot AUTO-TRACKS destinations. When it is added to (or made admin in) a
    group or channel it records that chat; when it is removed it stops dumping
    there. /chats lists them and lets you pause one without removing the bot,
    and /addchat / /remove cover the cases auto-tracking can't see.
  - Each file is uploaded ONCE to the first chat and then copied to the other
    chats, so upload bandwidth is paid a single time no matter how many chats
    you dump to.
  - Files larger than the configured split size are split into parts.
  - Progress is saved in SQLite, so if the container restarts mid-job the bot
    resumes where it left off instead of starting over.
"""
import asyncio
import html
import logging
import os
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from logging.handlers import RotatingFileHandler

from pyrogram import Client, filters, idle
from pyrogram.enums import ChatMemberStatus, ParseMode
from pyrogram.errors import MessageNotModified
from pyrogram.types import BotCommand, ChatMemberUpdated

import config
import db
import keyboards
import mega_client
import tasks
import ui
import utils

VERSION = "2.0"

# ----------------------------------------------------------------------
# Logging: rotating file (served by /log) plus stdout for `docker compose logs`
# ----------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        RotatingFileHandler(config.LOG_PATH, maxBytes=2_000_000, backupCount=2),
        logging.StreamHandler(sys.stdout),
    ],
)
logging.getLogger("pyrogram").setLevel(logging.WARNING)
log = logging.getLogger("bot")

app = Client(
    "leech_bot",
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    bot_token=config.BOT_TOKEN,
    workdir=config.DATA_DIR,        # keep the .session file on the volume
)

BOT_ID = None                       # filled in at startup
START_TIME = time.time()
RESTART_FLAG = os.path.join(config.DATA_DIR, "restart.json")

# Folder previews from /list, keyed by a short token so the callback data for
# "Start leech" stays inside Telegram's 64-byte limit.
_previews = {}
PREVIEW_PAGE = 12
CHATS_PAGE = 6

ACTIVE_STATUSES = {
    ChatMemberStatus.MEMBER,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.OWNER,
}

COMMANDS = [
    ("start", "Open the main menu"),
    ("leech", "Queue a MEGA folder"),
    ("list", "Preview a MEGA folder before leeching"),
    ("status", "Live dashboard for the running task"),
    ("queue", "Show queued tasks"),
    ("cancel", "Cancel a task"),
    ("chats", "Manage destination chats"),
    ("settings", "Look and behaviour"),
    ("stats", "Lifetime totals and machine health"),
    ("disk", "Disk space and downloads folder"),
    ("cleanup", "Delete leftover downloads"),
    ("ping", "Round-trip latency"),
    ("id", "Show chat and user ids"),
    ("log", "Download the log file"),
    ("help", "Full command reference"),
]

OTHER_COMMANDS = [
    "probe", "topic", "users", "restart", "sys", "addchat", "remove",
    "prefix", "suffix", "setthumb", "delthumb", "mirror",
]


# ----------------------------------------------------------------------
# Health-check web server (lets an external monitor confirm the bot is up)
# ----------------------------------------------------------------------
def _start_health_server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            running = tasks.manager.current
            body = (
                f"OK\nuptime={int(time.time() - START_TIME)}s\n"
                f"running={running.id if running else 'none'}\n"
                f"queued={len(tasks.manager.pending)}\n"
            ).encode()
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    HTTPServer(("0.0.0.0", config.PORT), Handler).serve_forever()


# ----------------------------------------------------------------------
# Auth: owners can do everything, sudo users can run jobs and change settings
# ----------------------------------------------------------------------
def _is_owner(user_id) -> bool:
    return user_id in config.OWNER_IDS


def _is_authorized(user_id) -> bool:
    if _is_owner(user_id):
        return True
    return user_id in config.SUDO_USERS or user_id in db.get_user_ids()


def _auth_filter(_, __, update):
    user = getattr(update, "from_user", None)
    return user is not None and _is_authorized(user.id)


AUTH = filters.create(_auth_filter)
ALL_COMMANDS = [c for c, _ in COMMANDS] + OTHER_COMMANDS


@app.on_message(filters.command(ALL_COMMANDS) & filters.private & ~AUTH)
async def deny(_, message):
    """
    Anyone who isn't an owner or a sudo user gets a single clear no.

    Private chats only: the bot sits in the dump channels too, and a stranger
    typing /start there shouldn't make it talk in front of an audience.
    """
    await message.reply_text(
        ui.panel(
            "ACCESS DENIED",
            [
                f"{ui.E['key']} This bot is private.",
                f"{ui.E['user']} Your id: "
                f"<code>{message.from_user.id if message.from_user else '?'}</code>",
                "",
                "Ask the owner to add you with "
                "<code>/users add &lt;your id&gt;</code>.",
            ],
            ui.E["stop"],
        ),
        parse_mode=ParseMode.HTML,
    )


# ----------------------------------------------------------------------
# Reply helpers — every outgoing message goes through these so parse mode,
# link previews and keyboards stay consistent.
# ----------------------------------------------------------------------
async def reply(message, text, kb=None):
    return await message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
        disable_web_page_preview=True,
        quote=False,
    )


async def edit(message, text, kb=None):
    try:
        return await message.edit_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
            disable_web_page_preview=True,
        )
    except MessageNotModified:
        return message


def _arg(message, default=""):
    """Everything after the command word, as one string."""
    parts = message.text.split(None, 1) if message.text else []
    return parts[1].strip() if len(parts) > 1 else default


# ----------------------------------------------------------------------
# Core commands
# ----------------------------------------------------------------------
@app.on_message(filters.command(["start"]) & AUTH)
async def start_cmd(_, message):
    name = message.from_user.first_name if message.from_user else "there"
    chats = len(db.get_active_chats())
    await reply(
        message,
        ui.welcome(name, config.BRAND, chats, VERSION),
        keyboards.main_menu(has_task=tasks.manager.current is not None),
    )


@app.on_message(filters.command(["help"]) & AUTH)
async def help_cmd(_, message):
    await reply(message, ui.help_panel("main"), keyboards.help_menu("main"))


@app.on_message(filters.command(["leech", "mirror"]) & AUTH)
async def leech_cmd(_, message):
    raw = _arg(message)
    if not raw and message.reply_to_message:
        raw = (message.reply_to_message.text or "").strip()
    if not raw:
        await reply(
            message,
            ui.panel(
                "USAGE",
                [
                    f"{ui.E['arrow']} <code>/leech &lt;mega folder url&gt;</code>",
                    f"{ui.E['arrow']} <code>/leech &lt;url&gt; | Custom title</code>",
                    "",
                    "You can also reply to a message containing the link.",
                ],
                ui.E["rocket"],
            ),
        )
        return

    url, _, title = raw.partition("|")
    url, title = url.strip(), title.strip()
    if not mega_client.is_mega_link(url):
        await reply(
            message,
            ui.panel(
                "NOT A MEGA LINK",
                [
                    f"{ui.E['fail']} <code>{ui.esc(ui.trunc(url, 60))}</code>",
                    "",
                    "Expected something like "
                    "<code>https://mega.nz/folder/AbCdEf#key</code>.",
                ],
                ui.E["warn"],
            ),
        )
        return

    await _enqueue(message, url, title or None)


async def _enqueue(message, url, title=None):
    task = tasks.Task(url, message.from_user.id, title=title)
    settings = db.get_settings()
    task.status_msg = await reply(
        message, ui.task_panel(task, settings),
        keyboards.task_controls(task.id, task.state),
    )
    position = tasks.manager.add(task)
    log.info("queued task %s (%s) at position %s", task.id, url, position)


@app.on_message(filters.command(["list"]) & AUTH)
async def list_cmd(_, message):
    url = _arg(message)
    if not mega_client.is_mega_link(url):
        await reply(message, ui.panel(
            "USAGE", [f"{ui.E['arrow']} <code>/list &lt;mega folder url&gt;</code>"],
            ui.E["folder"]))
        return

    status = await reply(
        message,
        ui.panel("FOLDER PREVIEW",
                 [f"{ui.E['hourglass']} Importing the folder and reading it…"],
                 ui.E["folder"]),
    )
    try:
        await mega_client.login()
        files = await mega_client.preview_folder(url)
    except Exception as e:
        await edit(status, ui.panel("PREVIEW FAILED",
                                    [f"{ui.E['fail']} <code>{ui.esc(e)}</code>"],
                                    ui.E["warn"]))
        return

    if not files:
        await edit(status, ui.panel(
            "EMPTY", [f"{ui.E['info']} Nothing found. Try <code>/probe</code> for the "
                      f"raw listing."], ui.E["folder"]))
        return

    token = uuid.uuid4().hex[:6]
    _previews[token] = {
        "url": url,
        "title": mega_client.link_label(url),
        "files": files,
        "total": sum(f.get("size") or 0 for f in files),
    }
    while len(_previews) > 20:                 # keep only the recent previews
        _previews.pop(next(iter(_previews)))
    await edit(status, _render_preview(token, 0), _preview_kb(token, 0))


def _preview_pages(preview):
    return max(1, (len(preview["files"]) + PREVIEW_PAGE - 1) // PREVIEW_PAGE)


def _render_preview(token, page):
    preview = _previews.get(token)
    if preview is None:
        return ui.panel("EXPIRED",
                        [f"{ui.E['info']} That preview is gone — run /list again."],
                        ui.E["warn"])
    pages = _preview_pages(preview)
    page = max(0, min(page, pages - 1))
    start = page * PREVIEW_PAGE
    return ui.listing_panel(
        preview["title"], preview["files"][start:start + PREVIEW_PAGE],
        preview["total"], page, pages, start,
    )


def _preview_kb(token, page):
    preview = _previews.get(token)
    if preview is None:
        return keyboards.back_home()
    return keyboards.listing_controls(token, page, _preview_pages(preview))


@app.on_message(filters.command(["probe"]) & AUTH)
async def probe_cmd(_, message):
    url = _arg(message)
    if not url:
        await reply(message, ui.panel(
            "USAGE", [f"{ui.E['arrow']} <code>/probe &lt;mega folder url&gt;</code>"],
            ui.E["info"]))
        return
    status = await reply(message, f"{ui.E['hourglass']} Importing and listing…")
    try:
        await mega_client.login()
        raw = await mega_client.raw_ls(url)
    except Exception as e:
        await edit(status, ui.panel("PROBE FAILED",
                                    [f"{ui.E['fail']} <code>{ui.esc(e)}</code>"],
                                    ui.E["warn"]))
        return
    raw = (raw or "(empty)")[:3500]
    await edit(status, f"{ui.header('RAW LISTING', ui.E['folder'])}\n"
                       f"<pre>{html.escape(raw)}</pre>")


@app.on_message(filters.command(["status"]) & AUTH)
async def status_cmd(_, message):
    await reply(message, *_status_view())


def _status_view():
    task = tasks.manager.current
    settings = db.get_settings()
    if task is None:
        pending = list(tasks.manager.pending)
        if pending:
            return (ui.queue_panel(None, pending), keyboards.queue_controls(pending))
        info = utils.sysinfo()
        return (
            ui.panel(
                "IDLE",
                [
                    f"{ui.E['info']} Nothing is running.",
                    "",
                    "Send <code>/leech &lt;mega url&gt;</code> to start one.",
                    ui.RULE,
                    ui.kv("Free disk", ui.human(utils.free_space()), ui.E["disk"]),
                ] + (ui.sysinfo_rows(info)[:2] if info else []),
                ui.E["chart"],
            ),
            keyboards.main_menu(),
        )
    text = ui.task_panel(task, settings)
    if task.failed:
        text += "\n" + ui.RULE + "\n" + "\n".join(
            f"{ui.E['fail']} <code>{ui.esc(ui.trunc(path, 34))}</code> — "
            f"{ui.esc(ui.trunc(err, 60))}"
            for path, err in task.failed[-5:]
        )
    return (text, keyboards.task_controls(task.id, task.state))


@app.on_message(filters.command(["queue"]) & AUTH)
async def queue_cmd(_, message):
    pending = list(tasks.manager.pending)
    await reply(
        message,
        ui.queue_panel(tasks.manager.current, pending),
        keyboards.queue_controls(pending),
    )


@app.on_message(filters.command(["cancel"]) & AUTH)
async def cancel_cmd(_, message):
    target = _arg(message).lstrip("#")
    if target.lower() == "all":
        dropped = tasks.manager.clear_queue()
        stopped = tasks.manager.cancel_current()
        await reply(message, ui.panel(
            "CANCELLED",
            [f"{ui.E['stop']} Running task: "
             f"{'stopping now' if stopped else 'none'}",
             f"{ui.E['queue']} Dropped <b>{dropped}</b> queued task(s)"],
            ui.E["stop"]))
        return

    if not target:
        if tasks.manager.cancel_current():
            await reply(message, ui.panel(
                "CANCELLING",
                [f"{ui.E['stop']} The running task will stop shortly.",
                 f"{ui.E['info']} Use <code>/cancel all</code> to clear the queue too."],
                ui.E["stop"]))
        else:
            await reply(message, ui.panel(
                "NOTHING RUNNING", [f"{ui.E['info']} The queue is empty."], ui.E["info"]))
        return

    if tasks.manager.cancel(target):
        await reply(message, ui.panel(
            "CANCELLING",
            [f"{ui.E['stop']} Task <code>#{ui.esc(target)}</code> cancelled."],
            ui.E["stop"]))
    else:
        await reply(message, ui.panel(
            "NOT FOUND", [f"{ui.E['warn']} No task <code>#{ui.esc(target)}</code>."],
            ui.E["warn"]))


# ----------------------------------------------------------------------
# Destinations
# ----------------------------------------------------------------------
def _chats_view(page=0):
    chats = [dict(c) for c in db.get_all_chats()]
    pages = max(1, (len(chats) + CHATS_PAGE - 1) // CHATS_PAGE)
    page = max(0, min(page, pages - 1))
    slice_ = chats[page * CHATS_PAGE:(page + 1) * CHATS_PAGE]
    return (
        ui.chats_panel(slice_, page, pages, len(chats)),
        keyboards.chats_controls(slice_, page, pages),
    )


@app.on_message(filters.command(["chats"]) & AUTH)
async def chats_cmd(_, message):
    text, kb = _chats_view(0)
    await reply(message, text, kb)


@app.on_message(filters.command(["addchat"]) & AUTH)
async def addchat_cmd(client, message):
    arg = _arg(message)
    if not arg:
        await reply(message, ui.panel("USAGE", [
            f"{ui.E['arrow']} <code>/addchat &lt;chat_id&gt;</code>",
            "",
            "For chats auto-tracking missed. I must already be a member.",
        ], ui.E["chat"]))
        return
    try:
        chat_id = int(arg.split()[0])
    except ValueError:
        await reply(message, ui.panel("BAD ID", [
            f"{ui.E['warn']} chat_id must be a number."], ui.E["warn"]))
        return
    try:
        chat = await client.get_chat(chat_id)
        db.add_or_update_chat(chat.id, chat.title, str(chat.type).split(".")[-1].lower())
        await reply(message, ui.panel("CHAT ADDED", [
            ui.kv("Title", chat.title, ui.E["chat"]),
            ui.kv("Id", chat.id, ui.E["key"]),
        ], ui.E["ok"]))
    except Exception as e:
        await reply(message, ui.panel("FAILED", [
            f"{ui.E['fail']} <code>{ui.esc(e)}</code>",
            "",
            "Make sure I'm a member (admin in channels) first.",
        ], ui.E["warn"]))


@app.on_message(filters.command(["remove"]) & AUTH)
async def remove_cmd(_, message):
    arg = _arg(message)
    if not arg:
        await reply(message, ui.panel("USAGE", [
            f"{ui.E['arrow']} <code>/remove &lt;chat_id&gt;</code>",
            "",
            "Get the id from <code>/chats</code>. To pause a chat instead of "
            "dropping it, tap it in <code>/chats</code>.",
        ], ui.E["chat"]))
        return
    try:
        chat_id = int(arg.split()[0])
    except ValueError:
        await reply(message, ui.panel("BAD ID", [
            f"{ui.E['warn']} chat_id must be a number."], ui.E["warn"]))
        return
    db.deactivate_chat(chat_id)
    await reply(message, ui.panel("CHAT REMOVED", [
        f"{ui.E['ok']} <code>{chat_id}</code> is off the dump list."], ui.E["ok"]))


@app.on_message(filters.command(["topic"]) & AUTH)
async def topic_cmd(_, message):
    parts = _arg(message).split()
    if len(parts) != 2:
        await reply(message, ui.panel("USAGE", [
            f"{ui.E['arrow']} <code>/topic &lt;chat_id&gt; &lt;topic_id&gt;</code>",
            f"{ui.E['arrow']} <code>/topic &lt;chat_id&gt; 0</code> (or "
            f"<code>off</code>) to clear",
            "",
            "To find the topic id: open the topic in Telegram Web — the URL "
            "ends with <code>/123</code>, and that number is the topic id.",
        ], ui.E["chat"]))
        return
    chat_id, thread = parts
    if db.get_chat(chat_id) is None:
        await reply(message, ui.panel("UNKNOWN CHAT", [
            f"{ui.E['warn']} <code>{ui.esc(chat_id)}</code> isn't tracked. "
            f"See <code>/chats</code>."], ui.E["warn"]))
        return
    try:
        value = None if thread.lower() in ("off", "none", "0") else int(thread)
    except ValueError:
        await reply(message, ui.panel("BAD ID", [
            f"{ui.E['warn']} topic_id must be a number, or <code>0</code>/"
            f"<code>off</code> to clear."], ui.E["warn"]))
        return
    db.set_chat_thread(chat_id, value)
    await reply(message, ui.panel("TOPIC UPDATED", [
        ui.kv("Chat", chat_id, ui.E["chat"]),
        ui.kv("Topic", value if value else "cleared", ui.E["arrow"]),
    ], ui.E["ok"]))


# ----------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------
@app.on_message(filters.command(["settings"]) & AUTH)
async def settings_cmd(_, message):
    s = db.get_settings()
    await reply(message, ui.settings_panel(s), keyboards.settings_controls(s))


@app.on_message(filters.command(["prefix", "suffix"]) & AUTH)
async def caption_cmd(_, message):
    key = "caption_prefix" if message.command[0] == "prefix" else "caption_suffix"
    value = _arg(message)
    if value.lower() in ("off", "clear", "none"):
        value = ""
    db.set_setting(key, value)
    s = db.get_settings()
    await reply(message, ui.settings_panel(s), keyboards.settings_controls(s))


@app.on_message(filters.command(["setthumb"]) & AUTH)
async def setthumb_cmd(client, message):
    target = message.reply_to_message
    if not target or not (target.photo or target.document):
        await reply(message, ui.panel("USAGE", [
            f"{ui.E['arrow']} Reply to a photo with <code>/setthumb</code>."], "🖼"))
        return
    await client.download_media(target, file_name=config.THUMB_PATH)
    db.set_setting("use_thumb", 1)
    await reply(message, ui.panel("THUMBNAIL SAVED", [
        f"{ui.E['ok']} It will be attached to future uploads.",
        f"{ui.E['info']} Remove it with <code>/delthumb</code>."], "🖼"))


@app.on_message(filters.command(["delthumb"]) & AUTH)
async def delthumb_cmd(_, message):
    existed = os.path.exists(config.THUMB_PATH)
    if existed:
        os.remove(config.THUMB_PATH)
    db.set_setting("use_thumb", 0)
    await reply(message, ui.panel(
        "THUMBNAIL", [f"{ui.E['ok']} Cleared." if existed
                      else f"{ui.E['info']} There wasn't one."], "🖼"))


# ----------------------------------------------------------------------
# Diagnostics
# ----------------------------------------------------------------------
@app.on_message(filters.command(["stats"]) & AUTH)
async def stats_cmd(_, message):
    await reply(
        message,
        ui.stats_panel(db.get_stats(), utils.sysinfo(), time.time() - START_TIME, VERSION),
        keyboards.back_home(),
    )


@app.on_message(filters.command(["sys", "disk"]) & AUTH)
async def sys_cmd(_, message):
    info = utils.sysinfo()
    rows = ui.sysinfo_rows(info) or [f"{ui.E['info']} No metrics available."]
    rows += [
        ui.RULE,
        ui.kv("Free disk", ui.human(utils.free_space()), ui.E["disk"]),
        ui.kv("Downloads folder", ui.human(utils.downloads_size()), ui.E["box"]),
        ui.kv("Uptime", ui.human_time(time.time() - START_TIME), ui.E["clock"]),
    ]
    await reply(message, ui.panel("SYSTEM", rows, ui.E["cpu"]), keyboards.back_home())


@app.on_message(filters.command(["cleanup"]) & AUTH)
async def cleanup_cmd(_, message):
    if tasks.manager.current is not None:
        await reply(message, ui.panel("BUSY", [
            f"{ui.E['warn']} A task is running — cancel it before cleaning."],
            ui.E["warn"]))
        return
    freed = utils.cleanup_downloads()
    await reply(message, ui.panel("CLEANED", [
        ui.kv("Reclaimed", ui.human(freed), ui.E["disk"]),
        ui.kv("Free now", ui.human(utils.free_space()), ui.E["disk"]),
    ], ui.E["ok"]))


@app.on_message(filters.command(["ping"]) & AUTH)
async def ping_cmd(_, message):
    t0 = time.time()
    sent = await reply(message, f"{ui.E['ping']} Pinging…")
    delta = (time.time() - t0) * 1000
    await edit(sent, ui.panel("PONG", [
        ui.kv("Round trip", f"{delta:.0f} ms", ui.E["speed"]),
        ui.kv("Uptime", ui.human_time(time.time() - START_TIME), ui.E["clock"]),
        ui.kv("Queue", f"{len(tasks.manager.pending)} waiting", ui.E["queue"]),
    ], ui.E["ping"]))


@app.on_message(filters.command(["id"]))
async def id_cmd(_, message):
    """Public on purpose: it's how someone finds the id to ask for access."""
    rows = [
        ui.kv("Chat id", message.chat.id, ui.E["chat"]),
        ui.kv("Chat type", str(message.chat.type).split(".")[-1].lower(), ui.E["info"]),
    ]
    if message.from_user:
        rows.append(ui.kv("Your id", message.from_user.id, ui.E["user"]))
    if message.message_thread_id:
        rows.append(ui.kv("Topic id", message.message_thread_id, ui.E["arrow"]))
    if message.reply_to_message and message.reply_to_message.from_user:
        rows.append(ui.kv("Replied user id",
                          message.reply_to_message.from_user.id, ui.E["user"]))
    await reply(message, ui.panel("IDENTIFIERS", rows, ui.E["key"]))


@app.on_message(filters.command(["log"]) & AUTH)
async def log_cmd(client, message):
    if not os.path.exists(config.LOG_PATH):
        await reply(message, ui.panel("NO LOG YET",
                                      [f"{ui.E['info']} Nothing has been written."],
                                      ui.E["log"]))
        return
    await client.send_document(
        message.chat.id, config.LOG_PATH,
        caption=f"{ui.E['log']} <code>bot.log</code>",
        parse_mode=ParseMode.HTML,
    )


@app.on_message(filters.command(["users"]) & AUTH)
async def users_cmd(_, message):
    args = _arg(message).split()
    is_owner = _is_owner(message.from_user.id)

    if args and not is_owner:
        await reply(message, ui.panel("OWNER ONLY", [
            f"{ui.E['key']} Only an owner can change the user list."], ui.E["warn"]))
        return

    if args and args[0] in ("add", "del", "remove") and len(args) > 1:
        try:
            uid = int(args[1])
        except ValueError:
            await reply(message, ui.panel("BAD ID", [
                f"{ui.E['warn']} <code>{ui.esc(args[1])}</code> isn't a numeric id."],
                ui.E["warn"]))
            return
        if args[0] == "add":
            db.add_user(uid, message.from_user.id)
            note = f"{ui.E['ok']} <code>{uid}</code> can now use the bot."
        else:
            note = (f"{ui.E['ok']} <code>{uid}</code> removed."
                    if db.remove_user(uid)
                    else f"{ui.E['info']} <code>{uid}</code> wasn't on the list.")
        await reply(message, ui.panel("USERS", [note], ui.E["user"]))
        return

    rows = [ui.kv("Owner", config.OWNER_ID, ui.E["key"])]
    for uid in sorted(config.OWNER_IDS - {config.OWNER_ID}):
        rows.append(ui.kv("Owner (EXTRA_OWNERS)", uid, ui.E["key"]))
    for uid in sorted(config.SUDO_USERS):
        rows.append(ui.kv("Sudo (env)", uid, ui.E["user"]))
    for row in db.get_users():
        rows.append(ui.kv("Sudo", row["user_id"], ui.E["user"]))
    if is_owner:
        rows += [ui.RULE,
                 f"{ui.E['arrow']} <code>/users add &lt;id&gt;</code>",
                 f"{ui.E['arrow']} <code>/users del &lt;id&gt;</code>"]
    await reply(message, ui.panel("AUTHORIZED USERS", rows, ui.E["user"]))


@app.on_message(filters.command(["restart"]) & AUTH)
async def restart_cmd(_, message):
    if not _is_owner(message.from_user.id):
        await reply(message, ui.panel("OWNER ONLY", [
            f"{ui.E['key']} Only an owner can restart the bot."], ui.E["warn"]))
        return
    if tasks.manager.current is not None:
        await reply(
            message,
            ui.panel("A TASK IS RUNNING", [
                f"{ui.E['warn']} Task <code>#{tasks.manager.current.id}</code> is active.",
                "It will resume from the next unfinished file after the restart.",
            ], ui.E["warn"]),
            keyboards.confirm("restart", label="Restart anyway"),
        )
        return
    await _do_restart(message)


async def _do_restart(message):
    sent = await reply(message, ui.panel(
        "RESTARTING", [f"{ui.E['restart']} Back in a moment…"], ui.E["restart"]))
    with open(RESTART_FLAG, "w") as fh:
        fh.write(f"{sent.chat.id}:{sent.id}")
    await app.stop()
    os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)])


# ----------------------------------------------------------------------
# Callback router — `<namespace>:<action>[:args]`, see keyboards.py
# ----------------------------------------------------------------------
@app.on_callback_query()
async def on_callback(client, query):
    if not _is_authorized(query.from_user.id):
        await query.answer("This bot is private.", show_alert=True)
        return

    ns, action, args = keyboards.parse(query.data)
    try:
        handler = {
            "nav": _cb_nav, "help": _cb_help, "task": _cb_task,
            "chats": _cb_chats, "set": _cb_settings, "ls": _cb_listing,
            "ok": _cb_confirm,
        }.get(ns)
        if handler is None:
            await query.answer()
            return
        await handler(client, query, action, args)
    except MessageNotModified:
        await query.answer()
    except Exception as e:
        log.exception("callback %s failed", query.data)
        await query.answer(f"Failed: {e}"[:180], show_alert=True)


async def _cb_nav(_, query, action, args):
    if action == "noop":
        await query.answer()
        return
    if action == "close":
        await query.answer("Dismissed")
        await query.message.delete()
        return
    if action == "home":
        chats = len(db.get_active_chats())
        name = query.from_user.first_name
        await edit(query.message, ui.welcome(name, config.BRAND, chats, VERSION),
                   keyboards.main_menu(tasks.manager.current is not None))
    elif action == "status":
        text, kb = _status_view()
        await edit(query.message, text, kb)
    elif action == "queue":
        pending = list(tasks.manager.pending)
        await edit(query.message, ui.queue_panel(tasks.manager.current, pending),
                   keyboards.queue_controls(pending))
    elif action == "stats":
        await edit(
            query.message,
            ui.stats_panel(db.get_stats(), utils.sysinfo(),
                           time.time() - START_TIME, VERSION),
            keyboards.back_home(),
        )
    await query.answer()


async def _cb_help(_, query, action, args):
    page = args[0] if args else "main"
    await edit(query.message, ui.help_panel(page), keyboards.help_menu(page))
    await query.answer()


async def _cb_task(_, query, action, args):
    if action == "refresh":
        task = tasks.manager.get(args[0]) if args else None
        if task is None:
            await query.answer("That task is gone.", show_alert=True)
            return
        await edit(query.message, ui.task_panel(task, db.get_settings()),
                   keyboards.task_controls(task.id, task.state))
        await query.answer("Refreshed")
    elif action == "cancel":
        ok = tasks.manager.cancel(args[0]) if args else False
        await query.answer("Cancelling…" if ok else "Task not found",
                           show_alert=not ok)
    elif action == "cancelcur":
        ok = tasks.manager.cancel_current()
        await query.answer("Cancelling…" if ok else "Nothing is running",
                           show_alert=not ok)
    elif action == "clearq":
        dropped = tasks.manager.clear_queue()
        pending = list(tasks.manager.pending)
        await edit(query.message, ui.queue_panel(tasks.manager.current, pending),
                   keyboards.queue_controls(pending))
        await query.answer(f"Dropped {dropped} task(s)")


async def _cb_chats(client, query, action, args):
    page = int(args[-1]) if args else 0
    if action == "toggle":
        chat_id = int(args[0])
        enabled = db.toggle_chat(chat_id)
        await query.answer("Enabled" if enabled else "Paused")
    elif action == "sync":
        await _sync_chat_titles(client)
        await query.answer("Titles refreshed")
    else:
        await query.answer()
    text, kb = _chats_view(page)
    await edit(query.message, text, kb)


async def _sync_chat_titles(client):
    """Re-read titles for tracked chats, and drop the ones we've lost access to."""
    for row in db.get_all_chats():
        try:
            chat = await client.get_chat(row["chat_id"])
            if chat.title and chat.title != row["title"]:
                db.set_chat_title(row["chat_id"], chat.title)
        except Exception:
            db.deactivate_chat(row["chat_id"])


async def _cb_settings(_, query, action, args):
    if action == "toggle":
        key = args[0]
        s = db.get_settings()
        db.set_setting(key, 0 if s.get(key) else 1)
        await query.answer("Updated")
    elif action == "cycle":
        key = args[0]
        s = db.get_settings()
        db.set_setting(key, keyboards.next_value(key, s.get(key)))
        await query.answer("Updated")
    elif action == "reset":
        db.reset_settings()
        await query.answer("Defaults restored")
    else:
        await query.answer()
    s = db.get_settings()
    await edit(query.message, ui.settings_panel(s), keyboards.settings_controls(s))


async def _cb_listing(_, query, action, args):
    token = args[0]
    preview = _previews.get(token)
    if preview is None:
        await query.answer("That preview expired — run /list again.", show_alert=True)
        return
    if action == "page":
        page = int(args[1])
        await edit(query.message, _render_preview(token, page), _preview_kb(token, page))
        await query.answer()
    elif action == "start":
        task = tasks.Task(preview["url"], query.from_user.id, title=preview["title"])
        settings = db.get_settings()
        await edit(query.message, ui.task_panel(task, settings),
                   keyboards.task_controls(task.id, task.state))
        task.status_msg = query.message
        tasks.manager.add(task)
        await query.answer("Queued")


async def _cb_confirm(_, query, action, args):
    if action == "restart":
        await query.answer("Restarting")
        await _do_restart(query.message)
    else:
        await query.answer()


# ----------------------------------------------------------------------
# Auto-track: record/forget chats as the bot is added or removed
# ----------------------------------------------------------------------
@app.on_chat_member_updated()
async def track_membership(_, update: ChatMemberUpdated):
    member = update.new_chat_member or update.old_chat_member
    if member is None or member.user is None or member.user.id != BOT_ID:
        return  # this update is about someone else, not the bot
    chat = update.chat
    new = update.new_chat_member
    if new and new.status in ACTIVE_STATUSES:
        db.add_or_update_chat(chat.id, chat.title, str(chat.type).split(".")[-1].lower())
        log.info("tracking chat %s (%s)", chat.id, chat.title)
        await _announce(f"{ui.E['ok']} Now dumping to <b>{ui.esc(chat.title)}</b>\n"
                        f"<code>{chat.id}</code>")
    else:
        db.deactivate_chat(chat.id)
        log.info("dropped chat %s (%s)", chat.id, chat.title)
        await _announce(f"{ui.E['stop']} Removed from <b>{ui.esc(chat.title)}</b>\n"
                        f"<code>{chat.id}</code>")


async def _announce(text):
    try:
        await app.send_message(config.OWNER_ID, text, parse_mode=ParseMode.HTML)
    except Exception:
        pass


# ----------------------------------------------------------------------
# Startup
# ----------------------------------------------------------------------
async def _resume_jobs():
    """Re-queue anything that was mid-flight when the process last died."""
    for job in db.get_running_jobs():
        task = tasks.Task(
            job["mega_url"], job["requested_by"],
            title=job["title"], job_id=job["job_id"],
        )
        try:
            task.status_msg = await app.send_message(
                job["requested_by"],
                ui.panel("RESUMING", [
                    f"{ui.E['restart']} Task <code>#{task.id}</code> picked up where "
                    f"it left off.",
                    ui.kv("Link", ui.trunc(job["mega_url"], 40), ui.E["link"]),
                ], ui.E["restart"]),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            log.warning("could not message %s about resumed job", job["requested_by"])
        tasks.manager.add(task)
        log.info("resumed job %s", task.id)


async def _announce_restart():
    if not os.path.exists(RESTART_FLAG):
        return
    try:
        with open(RESTART_FLAG) as fh:
            chat_id, msg_id = fh.read().strip().split(":")
        await app.edit_message_text(
            int(chat_id), int(msg_id),
            ui.panel("RESTARTED", [
                f"{ui.E['ok']} Back online.",
                ui.kv("Version", VERSION, ui.E["info"]),
            ], ui.E["restart"]),
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass
    finally:
        try:
            os.remove(RESTART_FLAG)
        except OSError:
            pass


async def main():
    global BOT_ID
    db.init_db()
    threading.Thread(target=_start_health_server, daemon=True).start()

    await app.start()
    me = await app.get_me()
    BOT_ID = me.id
    tasks.manager.bind(app)
    tasks.manager.start()

    try:
        await app.set_bot_commands([BotCommand(c, d) for c, d in COMMANDS])
    except Exception:
        log.warning("could not publish the command menu")

    log.info("Bot @%s started (v%s).", me.username, VERSION)
    await _announce_restart()
    await _resume_jobs()

    await idle()
    await app.stop()


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
