"""Helpers: human-readable sizes, an upload-progress throttle, and a file
splitter that writes one part at a time so peak disk usage stays low."""
import os
import time

import config


def human(num) -> str:
    num = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024.0:
            return f"{num:.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} PB"


class ProgressThrottle:
    """Limits how often we edit the status message during long uploads:
    at most once every `min_seconds`, and only when progress moved by at
    least `min_percent`. This avoids hammering Telegram (which triggers
    FloodWait)."""

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


def split_file(path: str):
    """
    Generator that splits `path` into parts of up to MAX_PART_SIZE, yielding
    each part's path as soon as it is finished.

    Only one part exists on disk at a time (in addition to the source file),
    so the extra disk needed is ~one part rather than a second full copy. The
    caller must upload and delete each yielded part before requesting the next.
    """
    part_num = 1
    with open(path, "rb") as src:
        while True:
            part_path = f"{path}.part{part_num:03d}"
            written = 0
            with open(part_path, "wb") as dst:
                while written < config.MAX_PART_SIZE:
                    to_read = min(config.SPLIT_READ_CHUNK, config.MAX_PART_SIZE - written)
                    chunk = src.read(to_read)
                    if not chunk:
                        break
                    dst.write(chunk)
                    written += len(chunk)
            if written == 0:
                os.remove(part_path)
                break
            yield part_path
            if written < config.MAX_PART_SIZE:
                break  # reached end of source during this part
            part_num += 1
