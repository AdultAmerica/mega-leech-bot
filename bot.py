"""
MEGA -> Telegram leech bot.

Commands (owner only):
  /start, /help              show help
  /leech <mega folder url>   download the folder and dump every file to all
                             chats the bot is currently in
  /chats                     list the chats the bot will dump to
  /probe <mega folder url>   dump the raw MEGA listing (for debugging the parser)
  /cancel                    stop the current job after the file in progress

How it works:
  - The bot AUTO-TRACKS destinations. When it is added to (or made admin in) a
    group or channel it records that chat; when it is removed it stops dumping
    there. (You can list them with /chats.)
  - Each file is uploaded ONCE to the first chat and then copied to the other
    chats by file id, so upload bandwidth (Railway egress) is paid a single
    time no matter how many chats you dump to.
  - Files larger than ~2 GB are split into parts automatically.
  - Progress is saved in SQLite, so if Railway restarts mid-job the bot resumes
    where it left off instead of starting over.
"""
import asyncio
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

from pyrogram import Client, filters, idle
from pyrogram.enums import ChatMemberStatus
from pyrogram.errors import FloodWait
from pyrogram.types import ChatMemberUpdated

import config
import db
import mega_client
import utils

app = Client(
    "leech_bot",
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    bot_token=config.BOT_TOKEN,
    workdir=config.DATA_DIR,        # keep the .session file on the volume
)

BOT_ID = None                       # filled in at startup
_busy = asyncio.Lock()              # ensures only one leech job runs at a time
_cancel = asyncio.Event()           # set by /cancel to stop the current job

ACTIVE_STATUSES = {
    ChatMemberStatus.MEMBER,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.OWNER,
}


# ----------------------------------------------------------------------
# Health-check web server (so Railway sees an open port and stays happy)
# ----------------------------------------------------------------------
def _start_health_server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")

        def log_message(self, *_):
            pass

    HTTPServer(("0.0.0.0", config.PORT), Handler).serve_forever()


# ----------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------
def _owner_only(_, __, message):
    return message.from_user is not None and message.from_user.id == config.OWNER_ID


OWNER = filters.create(_owner_only)


async def _safe_edit(message, text):
    try:
        await message.edit_text(text)
    except Exception:
        pass


# ----------------------------------------------------------------------
# Upload one file, then fan it out to the other chats with no re-upload
# ----------------------------------------------------------------------
_VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv", ".m4v"}
_PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
_AUDIO_EXTS = {".mp3", ".flac", ".ogg", ".wav", ".m4a", ".aac", ".opus", ".wma"}


def _media_type(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in _VIDEO_EXTS:
        return "video"
    if ext in _PHOTO_EXTS:
        return "photo"
    if ext in _AUDIO_EXTS:
        return "audio"
    return "document"


import json
import subprocess


def _video_meta(path):
    """Extract width, height, duration from a video file using ffprobe."""
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_streams", "-select_streams", "v:0", path,
            ],
            capture_output=True, text=True, timeout=30,
        )
        info = json.loads(r.stdout)
        s = info["streams"][0]
        return {
            "width": int(s.get("width", 0)) or None,
            "height": int(s.get("height", 0)) or None,
            "duration": int(float(s.get("duration", 0))) or None,
        }
    except Exception:
        return {}


async def _send_media(chat_id, path, media, thread_id, progress):
    kwargs = dict(chat_id=chat_id, message_thread_id=thread_id or None, progress=progress)
    try:
        if media == "video":
            meta = _video_meta(path)
            return await app.send_video(
                video=path, supports_streaming=True,
                file_name=os.path.basename(path),
                width=meta.get("width"), height=meta.get("height"),
                duration=meta.get("duration"),
                **kwargs,
            )
        elif media == "photo":
            return await app.send_photo(photo=path, **kwargs)
        elif media == "audio":
            return await app.send_audio(audio=path, file_name=os.path.basename(path), **kwargs)
    except Exception as e:
        print(f"Media send as {media} failed ({e}), falling back to document")
    return await app.send_document(document=path, file_name=os.path.basename(path), **kwargs)


async def _upload_and_fanout(path, chats, status_msg):
    name = os.path.basename(path)
    throttle = utils.ProgressThrottle()
    media = _media_type(path)
    print(f"Uploading {name} as {media}")

    async def _progress(current, total):
        if throttle.should_edit(current, total):
            pct = current * 100 // total if total else 0
            await _safe_edit(
                status_msg,
                f"Uploading `{name}`\n{utils.human(current)} / {utils.human(total)} ({pct}%)",
            )

    first = chats[0]
    sent = None
    while True:
        try:
            sent = await _send_media(first["chat_id"], path, media, first["thread_id"], _progress)
            break
        except FloodWait as e:
            await _safe_edit(status_msg, f"Rate limited, waiting {e.value}s...")
            await asyncio.sleep(e.value)

    if sent is None:
        return

    for chat in chats[1:]:
        while True:
            try:
                await sent.copy(
                    chat_id=chat["chat_id"],
                    message_thread_id=chat["thread_id"] or None,
                )
                break
            except FloodWait as e:
                await asyncio.sleep(e.value)
            except Exception:
                break


# ----------------------------------------------------------------------
# Process one file end to end: download -> (split) -> upload -> clean up
# ----------------------------------------------------------------------
async def _process_file(remote_path, rel_path, dest_dir, chats, status_msg, job_id):
    await _safe_edit(status_msg, f"Downloading `{rel_path}` from MEGA...")
    local = await mega_client.download_file(remote_path, rel_path, dest_dir)
    size = os.path.getsize(local)

    try:
        if size > config.MAX_PART_SIZE:
            await _safe_edit(
                status_msg, f"`{rel_path}` is {utils.human(size)} — splitting..."
            )
            for part in utils.split_file(local):
                await _upload_and_fanout(part, chats, status_msg)
                try:
                    os.remove(part)
                except OSError:
                    pass
        else:
            await _upload_and_fanout(local, chats, status_msg)
    finally:
        if os.path.exists(local):
            try:
                os.remove(local)
            except OSError:
                pass

    db.mark_file_done(job_id, rel_path)


# ----------------------------------------------------------------------
# The leech job
# ----------------------------------------------------------------------
async def run_leech(link, status_msg, requested_by, job_id=None):
    if job_id is None:
        job_id = uuid.uuid4().hex[:12]
        db.create_job(job_id, link, requested_by)

    _cancel.clear()
    remote_path = None
    try:
        await mega_client.login()

        await _safe_edit(status_msg, "Importing folder into MEGA account...")
        remote_path, files = await mega_client.list_folder(link)
        if not files:
            await _safe_edit(
                status_msg,
                "No files found. Run `/probe <link>` so I can check the raw listing.",
            )
            db.set_job_status(job_id, "error")
            return

        chats = [dict(c) for c in db.get_active_chats()]
        if not chats:
            await _safe_edit(
                status_msg,
                "I'm not in any groups or channels yet. Add me (as admin in "
                "channels), then run /leech again.",
            )
            db.set_job_status(job_id, "error")
            return

        total = len(files)
        for index, rel_path in enumerate(files, start=1):
            if _cancel.is_set():
                await _safe_edit(status_msg, "Cancelled.")
                db.set_job_status(job_id, "cancelled")
                return
            if db.is_file_done(job_id, rel_path):
                continue
            await _safe_edit(status_msg, f"[{index}/{total}] {rel_path}")
            try:
                await _process_file(
                    remote_path, rel_path, config.DOWNLOAD_DIR, chats, status_msg, job_id
                )
            except Exception as e:
                await app.send_message(requested_by, f"Failed on `{rel_path}`: {e}")

        db.set_job_status(job_id, "done")
        await _safe_edit(
            status_msg, f"Done — {total} files dumped to {len(chats)} chat(s)."
        )
    except Exception as e:
        db.set_job_status(job_id, "error")
        await _safe_edit(status_msg, f"Job failed: {e}")
    finally:
        if remote_path:
            await mega_client.cleanup(remote_path)


# ----------------------------------------------------------------------
# Command handlers (all owner-only)
# ----------------------------------------------------------------------
@app.on_message(filters.command(["start", "help"]) & OWNER)
async def help_cmd(_, message):
    await message.reply_text(
        "**MEGA → Telegram leech bot**\n\n"
        "`/leech <mega folder url>` — download a folder and dump every file to "
        "all chats I'm in\n"
        "`/chats` — show where I'll dump\n"
        "`/topic <chat_id> <topic_id>` — set a forum topic for a group\n"
        "`/remove <chat_id>` — remove a chat from the dump list\n"
        "`/probe <mega folder url>` — show the raw MEGA listing (debug)\n"
        "`/cancel` — stop after the current file\n\n"
        "Add me to a group or channel (as admin in channels) and I'll start "
        "dumping there automatically."
    )


@app.on_message(filters.command("chats") & OWNER)
async def chats_cmd(_, message):
    chats = db.get_active_chats()
    if not chats:
        await message.reply_text("I'm not in any chats yet.")
        return
    lines = []
    for c in chats:
        topic = f" → topic `{c['thread_id']}`" if c["thread_id"] else ""
        lines.append(f"• {c['title'] or c['chat_id']} ({c['type']}) `{c['chat_id']}`{topic}")
    await message.reply_text("I'll dump to:\n" + "\n".join(lines))


@app.on_message(filters.command("topic") & OWNER)
async def topic_cmd(_, message):
    if len(message.command) < 3:
        await message.reply_text(
            "Usage: `/topic <chat_id> <topic_id>`\n"
            "Set topic to `0` to clear it.\n\n"
            "To find the topic ID: open the topic in Telegram Web, "
            "the URL ends with `/123` — that number is the topic ID."
        )
        return
    try:
        chat_id = int(message.command[1])
        topic_id = int(message.command[2])
    except ValueError:
        await message.reply_text("Both chat_id and topic_id must be numbers.")
        return
    db.set_chat_thread(chat_id, topic_id if topic_id != 0 else None)
    if topic_id:
        await message.reply_text(f"Set topic `{topic_id}` for chat `{chat_id}`.")
    else:
        await message.reply_text(f"Cleared topic for chat `{chat_id}`.")


@app.on_message(filters.command("remove") & OWNER)
async def remove_cmd(_, message):
    if len(message.command) < 2:
        await message.reply_text("Usage: `/remove <chat_id>`\nGet the chat_id from `/chats`.")
        return
    try:
        chat_id = int(message.command[1])
    except ValueError:
        await message.reply_text("chat_id must be a number.")
        return
    db.deactivate_chat(chat_id)
    await message.reply_text(f"Removed `{chat_id}` from dump list.")


@app.on_message(filters.command("probe") & OWNER)
async def probe_cmd(_, message):
    if len(message.command) < 2:
        await message.reply_text("Usage: `/probe <mega folder url>`")
        return
    link = message.command[1]
    msg = await message.reply_text("Running mega-ls...")
    try:
        await mega_client.login()
        raw = await mega_client.raw_ls(link)
    except Exception as e:
        await msg.edit_text(f"Error: {e}")
        return
    raw = raw or "(empty)"
    await msg.edit_text("Raw listing (first 3500 chars):\n```\n" + raw[:3500] + "\n```")


@app.on_message(filters.command("cancel") & OWNER)
async def cancel_cmd(_, message):
    if _busy.locked():
        _cancel.set()
        await message.reply_text("Will stop after the current file finishes.")
    else:
        await message.reply_text("Nothing is running.")


@app.on_message(filters.command("leech") & OWNER)
async def leech_cmd(_, message):
    if len(message.command) < 2:
        await message.reply_text("Usage: `/leech <mega folder url>`")
        return
    if _busy.locked():
        await message.reply_text("A job is already running. Use /cancel first.")
        return
    link = message.command[1]
    status = await message.reply_text("Starting...")
    async with _busy:
        await run_leech(link, status, message.from_user.id)


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
        db.add_or_update_chat(chat.id, chat.title, str(chat.type))
    else:
        db.deactivate_chat(chat.id)


# ----------------------------------------------------------------------
# Startup
# ----------------------------------------------------------------------
async def _resume_jobs():
    for job in db.get_running_jobs():
        try:
            msg = await app.send_message(
                config.OWNER_ID, f"Resuming interrupted job for {job['mega_url']}"
            )
        except Exception:
            continue
        async with _busy:
            await run_leech(
                job["mega_url"], msg, job["requested_by"], job_id=job["job_id"]
            )


async def main():
    global BOT_ID
    db.init_db()
    threading.Thread(target=_start_health_server, daemon=True).start()

    await app.start()
    me = await app.get_me()
    BOT_ID = me.id
    print(f"Bot @{me.username} started.")

    await _resume_jobs()
    await idle()
    await app.stop()


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
