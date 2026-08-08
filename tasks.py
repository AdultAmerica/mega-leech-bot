"""
The task queue and the pipeline that runs one.

A Task is the unit the UI talks about: it owns the live state that ui.py
renders (state, current file, bytes, speed, ETA) and the cancel switch the
buttons flip. The Manager runs exactly one task at a time — a leech is disk
and bandwidth bound, so overlapping two of them makes both slower — and keeps
the rest in a queue the user can inspect and drop entries from.

Every running task gets a *refresher*: a background coroutine that redraws its
status message on a fixed interval. Progress callbacks only mutate state; they
never touch Telegram. That separation is what keeps a fast transfer from
generating hundreds of message edits (and the FloodWaits that follow).

The MEGA side follows mega_client's contract: a job imports the public link
once, works against the resulting cloud path, and removes the import when it
finishes — including when it fails or is cancelled.
"""
import asyncio
import json
import logging
import os
import subprocess
import time
import uuid
from collections import deque

from pyrogram import StopTransmission
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait, MessageNotModified

import config
import db
import keyboards
import mega_client
import ui
import utils

log = logging.getLogger("tasks")

LIVE_STATES = {"queued", "listing", "downloading", "splitting", "uploading"}


class Task:
    def __init__(self, url, requested_by, title=None, job_id=None):
        self.id = job_id or uuid.uuid4().hex[:6]
        self.url = url
        self.title = title or mega_client.link_label(url)
        self.requested_by = requested_by
        self.remote_path = None        # MEGA cloud path of the imported folder

        self.state = "queued"
        self.error = None
        self.failed = []

        # current file
        self.file_name = ""
        self.current_done = 0
        self.current_total = 0
        self.speed = 0.0
        self.eta = 0.0

        # whole job
        self.file_total = 0
        self.files_done = 0
        self.bytes_total = 0
        self.bytes_done = 0
        self.bytes_sent = 0
        self.chat_count = 0

        self.created_at = time.time()
        self.started_at = time.time()
        self.finished_at = None
        self.queue_pos = 0

        self.cancel = asyncio.Event()
        self.status_msg = None
        self._meter = utils.SpeedMeter()

    @property
    def duration(self) -> float:
        return (self.finished_at or time.time()) - self.started_at

    @property
    def live(self) -> bool:
        return self.state in LIVE_STATES

    def note_progress(self, done, total):
        """Called from a transfer callback — state only, never Telegram."""
        self.current_done = done
        self.current_total = total
        self.speed, self.eta = self._meter.update(done, total)

    def begin_file(self, name, total=0):
        self.file_name = name
        self.current_done = 0
        self.current_total = total
        self.speed = 0.0
        self.eta = 0.0
        self._meter = utils.SpeedMeter()


class Manager:
    """Owns the queue, the worker loop, and the live refreshers."""

    def __init__(self):
        self.app = None
        self.pending = deque()
        self.current = None
        self._wake = asyncio.Event()
        self._worker = None

    # ---------------------------------------------------------------- wiring
    def bind(self, app):
        self.app = app

    def start(self):
        if self._worker is None:
            self._worker = asyncio.ensure_future(self._worker_loop())

    # ------------------------------------------------------------ queue API
    def add(self, task: Task) -> int:
        """Queue a task; returns its position (0 = will start immediately)."""
        self.pending.append(task)
        self._renumber()
        self._wake.set()
        return task.queue_pos

    def _renumber(self):
        for i, t in enumerate(self.pending, start=1):
            t.queue_pos = i if self.current else i - 1

    def get(self, task_id):
        if self.current and self.current.id == task_id:
            return self.current
        for t in self.pending:
            if t.id == task_id:
                return t
        return None

    def all_tasks(self):
        return ([self.current] if self.current else []) + list(self.pending)

    def cancel(self, task_id) -> bool:
        if self.current and self.current.id == task_id:
            self.current.cancel.set()
            return True
        for t in list(self.pending):
            if t.id == task_id:
                self.pending.remove(t)
                t.state = "cancelled"
                t.finished_at = time.time()
                db.set_job_status(t.id, "cancelled")
                asyncio.ensure_future(self._final_draw(t))
                self._renumber()
                return True
        return False

    def cancel_current(self) -> bool:
        if self.current:
            self.current.cancel.set()
            return True
        return False

    def clear_queue(self) -> int:
        dropped = len(self.pending)
        for t in list(self.pending):
            t.state = "cancelled"
            t.finished_at = time.time()
            db.set_job_status(t.id, "cancelled")
            asyncio.ensure_future(self._final_draw(t))
        self.pending.clear()
        return dropped

    # ----------------------------------------------------------- worker loop
    async def _worker_loop(self):
        while True:
            if not self.pending:
                self._wake.clear()
                await self._wake.wait()
                continue
            task = self.pending.popleft()
            self.current = task
            self._renumber()
            refresher = asyncio.ensure_future(self._refresh_loop(task))
            try:
                await self._run(task)
            except Exception as e:                     # never kill the worker
                log.exception("task %s crashed", task.id)
                task.state = "error"
                task.error = str(e)
                db.set_job_status(task.id, "error", str(e))
                db.bump("jobs_failed")
            finally:
                # Always give the imported folder back, however the job ended.
                if task.remote_path:
                    try:
                        await mega_client.cleanup(task.remote_path)
                    except Exception:
                        log.warning("could not remove import %s", task.remote_path)
                task.finished_at = time.time()
                refresher.cancel()
                await self._final_draw(task)
                await self._notify_done(task)
                self.current = None
                self._renumber()

    # ------------------------------------------------------- live rendering
    async def _refresh_loop(self, task):
        """Redraw the task's status message until it stops being live."""
        last = None
        while task.live:
            settings = db.get_settings()
            interval = max(3, int(settings.get("refresh_secs", 6)))
            text = ui.task_panel(task, settings)
            if text != last:
                await self._edit(task, text)
                last = text
            await asyncio.sleep(interval)

    async def _final_draw(self, task):
        settings = db.get_settings()
        await self._edit(task, ui.task_panel(task, settings, live=False))

    async def _edit(self, task, text):
        if task.status_msg is None:
            return
        try:
            await task.status_msg.edit_text(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboards.task_controls(task.id, task.state),
                link_preview_options=keyboards.NO_PREVIEW,
            )
        except MessageNotModified:
            pass
        except FloodWait as e:
            await asyncio.sleep(e.value)
        except Exception:
            pass                                   # a dead status message is not fatal

    async def _notify_done(self, task):
        settings = db.get_settings()
        if not settings.get("notify_done"):
            return
        if task.status_msg is not None and task.status_msg.chat.id == task.requested_by:
            return                                 # they're already looking at it
        icon = {"done": ui.E["ok"], "error": ui.E["fail"]}.get(task.state, ui.E["stop"])
        try:
            await self.app.send_message(
                task.requested_by,
                f"{icon} Task <code>#{task.id}</code> "
                f"<b>{task.state}</b> — {ui.esc(ui.trunc(task.title, 40))}\n"
                f"{ui.E['file']} {task.files_done}/{task.file_total} files · "
                f"{ui.human(task.bytes_sent)} uploaded",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    # ------------------------------------------------------------- pipeline
    async def _run(self, task):
        settings = db.get_settings()
        db.create_job(task.id, task.url, task.requested_by, task.title)
        task.started_at = time.time()
        task.state = "listing"

        await mega_client.login()
        # Imports the public link into the account; the worker's finally block
        # removes it again once the job is over.
        task.remote_path, paths = await mega_client.list_folder(task.url)
        if not paths:
            raise RuntimeError(
                "No files found in that link. Try /probe <url> to see the raw listing."
            )

        chats = [dict(c) for c in db.get_active_chats()]
        if not chats:
            raise RuntimeError(
                "No destinations. Add me to a group, or make me an admin in a "
                "channel, then run the task again."
            )

        sizes = await mega_client.folder_sizes(task.remote_path)
        files = [{"path": p, "size": sizes.get(p, 0)} for p in paths]

        task.chat_count = len(chats)
        task.file_total = len(files)
        task.bytes_total = sum(f["size"] for f in files)
        db.set_job_totals(task.id, task.file_total)

        # Files finished by an earlier run of this same job id count as done.
        task.files_done = db.done_file_count(task.id)

        for entry in files:
            if task.cancel.is_set():
                task.state = "cancelled"
                db.set_job_status(task.id, "cancelled")
                return
            rel = entry["path"]
            if db.is_file_done(task.id, rel):
                continue
            try:
                await self._process_file(task, entry, chats, settings)
                task.files_done += 1
            except mega_client.Cancelled:
                task.state = "cancelled"
                db.set_job_status(task.id, "cancelled")
                return
            except Exception as e:
                log.exception("file failed: %s", rel)
                task.failed.append((rel, str(e)))
                db.bump("files_failed")

        task.state = "done"
        db.set_job_status(task.id, "done")
        db.bump("jobs_done")

    async def _process_file(self, task, entry, chats, settings):
        rel = entry["path"]
        name = os.path.basename(rel)
        retries = max(1, int(settings.get("retries", 3)))

        local = None
        for attempt in range(1, retries + 1):
            try:
                task.state = "downloading"
                task.begin_file(name, entry.get("size") or 0)
                local = await mega_client.download_file(
                    task.remote_path, rel, config.DOWNLOAD_DIR,
                    on_progress=task.note_progress,
                    cancel=task.cancel,
                    expected_size=entry.get("size") or 0,
                )
                break
            except mega_client.Cancelled:
                raise
            except Exception:
                if attempt == retries:
                    raise
                await asyncio.sleep(min(5 * attempt, 30))

        size = os.path.getsize(local)
        task.bytes_done += size
        db.bump("bytes_down", size)

        try:
            split_size = int(settings.get("split_size", config.MAX_PART_SIZE))
            if size > split_size:
                task.state = "splitting"
                parts = utils.part_count(size, split_size)
                for index, part in enumerate(utils.split_file(local, split_size), start=1):
                    if task.cancel.is_set():
                        if os.path.exists(part):
                            os.remove(part)
                        raise mega_client.Cancelled("cancelled while splitting")
                    task.state = "uploading"
                    task.begin_file(f"{os.path.basename(part)}  ({index}/{parts})",
                                    os.path.getsize(part))
                    await self._upload_and_fanout(task, part, chats, settings)
                    try:
                        os.remove(part)
                    except OSError:
                        pass
            else:
                task.state = "uploading"
                task.begin_file(name, size)
                await self._upload_and_fanout(task, local, chats, settings)
        finally:
            if local and os.path.exists(local):
                try:
                    os.remove(local)
                except OSError:
                    pass

        db.mark_file_done(task.id, rel, size)
        db.bump("files_done")

    # ------------------------------------------------- upload + fan-out
    async def _upload_and_fanout(self, task, path, chats, settings):
        """
        Upload once to the first destination, then copy the message to the
        rest. Telegram already holds the bytes after the first send, so the
        fan-out costs no additional upload bandwidth.
        """
        name = os.path.basename(path)
        caption = utils.build_caption(name, settings)
        silent = bool(settings.get("silent"))
        thumb = (
            config.THUMB_PATH
            if settings.get("use_thumb") and os.path.exists(config.THUMB_PATH)
            else None
        )
        media = (
            _media_type(path)
            if settings.get("upload_mode") == "media"
            else "document"
        )

        def _progress(current, total):
            # StopTransmission is Pyrogram's sanctioned way to abort a transfer
            # from inside a progress callback; send_* then returns None.
            if task.cancel.is_set():
                raise StopTransmission
            task.note_progress(current, total)

        first = chats[0]
        while True:
            try:
                sent = await self._send_media(
                    first, path, media, caption, silent, thumb, _progress
                )
            except FloodWait as e:
                task.state = "uploading"
                await asyncio.sleep(e.value)
                continue
            if sent is None:
                if task.cancel.is_set():
                    raise mega_client.Cancelled("cancelled during upload")
                raise RuntimeError(f"upload of {name} returned no message")
            break

        size = os.path.getsize(path)
        task.bytes_sent += size
        db.bump("bytes_sent", size)

        if not settings.get("fanout"):
            return

        for chat in chats[1:]:
            for _ in range(3):
                try:
                    await sent.copy(
                        chat_id=chat["chat_id"],
                        message_thread_id=chat.get("thread_id") or None,
                        disable_notification=silent,
                    )
                    break
                except FloodWait as e:
                    await asyncio.sleep(e.value)
                except Exception as e:
                    # One bad destination must not stop the whole job.
                    log.warning("fan-out to %s failed: %s", chat["chat_id"], e)
                    break

    async def _send_media(self, chat, path, media, caption, silent, thumb, progress):
        """
        Send as the richest type the file supports, falling back to a plain
        document if Telegram rejects it — a .mkv it can't parse must still
        arrive rather than failing the file.
        """
        kwargs = dict(
            chat_id=chat["chat_id"],
            caption=caption,
            parse_mode=ParseMode.HTML,
            disable_notification=silent,
            message_thread_id=chat.get("thread_id") or None,
            progress=progress,
        )
        name = os.path.basename(path)
        try:
            if media == "video":
                meta = _video_meta(path)
                return await self.app.send_video(
                    video=path, supports_streaming=True, file_name=name,
                    width=meta.get("width"), height=meta.get("height"),
                    duration=meta.get("duration"),
                    **({"thumb": thumb} if thumb else {}), **kwargs,
                )
            if media == "photo":
                kwargs.pop("parse_mode", None)
                return await self.app.send_photo(photo=path, **kwargs)
            if media == "audio":
                return await self.app.send_audio(
                    audio=path, file_name=name,
                    **({"thumb": thumb} if thumb else {}), **kwargs,
                )
        except StopTransmission:
            raise
        except FloodWait:
            raise
        except Exception as e:
            log.warning("send as %s failed (%s), falling back to document", media, e)
        return await self.app.send_document(
            document=path, file_name=name,
            **({"thumb": thumb} if thumb else {}), **kwargs,
        )


# ----------------------------------------------------------------------
# Media typing (kept identical to the shipped behaviour)
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


def _video_meta(path):
    """Width/height/duration via ffprobe, so Telegram shows the right aspect."""
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


manager = Manager()
