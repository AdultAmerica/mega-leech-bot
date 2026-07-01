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
    chats by file id, so upload bandwidth is paid a single time no matter how
    many chats you dump to.
  - Files larger than ~2 GB are split into parts automatically.
  - Progress is saved in SQLite, so if the container restarts mid-job the bot
    resumes where it left off instead of starting over.
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
# Health-check web server (lets an external monitor confirm the bot is up)
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
async def _upload_and_fanout(path, chats, status_msg):
    name = os.path.basename(path)
    throttle = utils.ProgressThrottle()

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
            sent = await app.send_document(
                chat_id=first["chat_id"],
                document=path,
                message_thread_id=first["thread_id"] or None,
                progress=_progress,
            )
            break
        except FloodWait as e:
            await _safe_edit(status_msg, f"Rate limited, waiting {e.value}s...")
            await asyncio.sleep(e.value)

    file_id = sent.document.file_id if sent and sent.document else None
    if file_id is None:
        return

    # Copy to the remaining chats by file id (Telegram already has the bytes).
    for chat in chats[1:]:
        while True:
            try:
                await app.send_document(
                    chat_id=chat["chat_id"],
                    document=file_id,
                    message_thread_id=chat["thread_id"] or None,
                )
                break
            except FloodWait as e:
                await asyncio.sleep(e.value)
            except Exception:
                break  # a single bad destination must not stop the whole job


# ----------------------------------------------------------------------
# Process one file end to end: download -> (split) -> upload -> clean up
# ----------------------------------------------------------------------
async def _process_file(link, rel_path, dest_dir, chats, status_msg, job_id):
    await _safe_edit(status_msg, f"Downloading `{rel_path}` from MEGA...")
    local = await mega_client.download_file(link, rel_path, dest_dir)
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
    try:
        await mega_client.login()

        await _safe_edit(status_msg, "Listing folder contents...")
        files = await mega_client.list_folder(link)
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
                    link, rel_path, config.DOWNLOAD_DIR, chats, status_msg, job_id
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
    lines = [
        f"• {c['title'] or c['chat_id']} ({c['type']}) `{c['chat_id']}`" for c in chats
    ]
    await message.reply_text("I'll dump to:\n" + "\n".join(lines))


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
    app.run(main())
