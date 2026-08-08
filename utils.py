"""
Helpers that aren't Telegram, MEGA, or storage: the file splitter, a moving
speed/ETA meter, machine health, and caption building.
"""
import html
import os
import shutil
import time

import config
import ui

VIDEO_EXT = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".ts", ".mpg", ".mpeg"}
AUDIO_EXT = {".mp3", ".m4a", ".flac", ".ogg", ".opus", ".wav", ".aac"}
PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp"}


def human(num) -> str:
    """Kept for callers outside the UI layer; ui.human is the canonical one."""
    return ui.human(num)


def kind_of(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in PHOTO_EXT:
        return "photo"
    return "document"


def build_caption(name: str, settings: dict) -> str:
    """Wrap a filename in the configured prefix/suffix and caption style."""
    style = settings.get("caption_style", "mono")
    safe = html.escape(name, quote=False)
    if style == "mono":
        body = f"<code>{safe}</code>"
    elif style == "bold":
        body = f"<b>{safe}</b>"
    else:
        body = safe
    prefix = settings.get("caption_prefix") or ""
    suffix = settings.get("caption_suffix") or ""
    parts = [p for p in (html.escape(prefix, quote=False), body,
                         html.escape(suffix, quote=False)) if p]
    caption = "\n".join(parts)
    return caption[:1024]          # Telegram's caption limit


class SpeedMeter:
    """
    Rolling speed and ETA from progress callbacks.

    Uses a short window (the last few seconds) rather than the whole-transfer
    average, so the number on screen reacts to a stall instead of hiding it.
    """

    def __init__(self, window: float = 12.0):
        self.window = window
        self.samples = []          # (timestamp, bytes_done)
        self.start = time.time()

    def update(self, done: float, total: float):
        now = time.time()
        self.samples.append((now, done))
        cutoff = now - self.window
        while len(self.samples) > 2 and self.samples[0][0] < cutoff:
            self.samples.pop(0)
        return self.speed(), self.eta(done, total)

    def speed(self) -> float:
        if len(self.samples) < 2:
            return 0.0
        (t0, b0), (t1, b1) = self.samples[0], self.samples[-1]
        elapsed = t1 - t0
        if elapsed <= 0:
            return 0.0
        return max((b1 - b0) / elapsed, 0.0)

    def eta(self, done: float, total: float) -> float:
        rate = self.speed()
        if rate <= 0 or not total or done >= total:
            return 0.0
        return (total - done) / rate


class ProgressThrottle:
    """Limits how often we edit a status message during long transfers: at
    most once every `min_seconds`, and only when progress moved by at least
    `min_percent`. This avoids hammering Telegram (which triggers FloodWait)."""

    def __init__(self, min_percent: float = 5.0, min_seconds: float = 4.0):
        self.min_percent = min_percent
        self.min_seconds = min_seconds
        self.last_pct = -100.0
        self.last_t = 0.0

    def should_edit(self, current: int, total: int) -> bool:
        if not total:
            return False
        pct = current * 100.0 / total
        now = time.time()
        if (pct - self.last_pct) >= self.min_percent and (now - self.last_t) >= self.min_seconds:
            self.last_pct = pct
            self.last_t = now
            return True
        return False


def split_file(path: str, part_size: int = None):
    """
    Generator that splits `path` into parts of up to `part_size`, yielding
    each part's path as soon as it is finished.

    Only one part exists on disk at a time (in addition to the source file),
    so the extra disk needed is ~one part rather than a second full copy. The
    caller must upload and delete each yielded part before requesting the next.
    """
    part_size = int(part_size or config.MAX_PART_SIZE)
    part_num = 1
    with open(path, "rb") as src:
        while True:
            part_path = f"{path}.part{part_num:03d}"
            written = 0
            with open(part_path, "wb") as dst:
                while written < part_size:
                    to_read = min(config.SPLIT_READ_CHUNK, part_size - written)
                    chunk = src.read(to_read)
                    if not chunk:
                        break
                    dst.write(chunk)
                    written += len(chunk)
            if written == 0:
                os.remove(part_path)
                break
            yield part_path
            if written < part_size:
                break              # reached end of source during this part
            part_num += 1


def part_count(size: int, part_size: int) -> int:
    if not part_size or size <= part_size:
        return 1
    return (size + part_size - 1) // part_size


def sysinfo() -> dict:
    """
    Machine health for the /stats and /sys panels.

    psutil gives the full picture; without it we still report disk usage and
    the load average, so the panel degrades instead of disappearing.
    """
    info = {}
    try:
        import psutil

        info["cpu"] = psutil.cpu_percent(interval=0.3)
        mem = psutil.virtual_memory()
        info["ram"] = mem.percent
        info["ram_note"] = f"{human(mem.used)} / {human(mem.total)}"
        net = psutil.net_io_counters()
        info["net"] = f"↑ {human(net.bytes_sent)}  ↓ {human(net.bytes_recv)}"
    except Exception:
        pass

    try:
        usage = shutil.disk_usage(config.DATA_DIR)
        info["disk"] = usage.used * 100.0 / usage.total
        info["disk_note"] = f"{human(usage.free)} free"
    except Exception:
        pass

    try:
        load = os.getloadavg()
        info["load"] = " ".join(f"{x:.2f}" for x in load)
    except (OSError, AttributeError):
        pass
    return info


def free_space() -> int:
    try:
        return shutil.disk_usage(config.DATA_DIR).free
    except OSError:
        return 0


def downloads_size() -> int:
    """Bytes currently sitting in the download folder."""
    total = 0
    for root, _dirs, names in os.walk(config.DOWNLOAD_DIR):
        for name in names:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def cleanup_downloads() -> int:
    """
    Delete leftovers in the download dir; returns bytes reclaimed.

    Also prunes the empty directories behind them — mega-get works in a fresh
    temp subfolder per file, so those accumulate even when no data does.
    """
    freed = 0
    for root, _dirs, names in os.walk(config.DOWNLOAD_DIR):
        for name in names:
            path = os.path.join(root, name)
            try:
                freed += os.path.getsize(path)
                os.remove(path)
            except OSError:
                pass
    for root, dirs, _names in os.walk(config.DOWNLOAD_DIR, topdown=False):
        for name in dirs:
            try:
                os.rmdir(os.path.join(root, name))
            except OSError:
                pass
    return freed
