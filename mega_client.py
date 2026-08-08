"""
Thin wrapper around MEGAcmd (the official MEGA command-line tool).

MEGAcmd exposes commands prefixed with "mega-" (mega-login, mega-ls,
mega-get, ...). Any of them auto-starts the background MEGAcmd server, so we
just shell out to them with asyncio subprocesses.

Three behaviours here were learned the hard way and must not be "simplified":

  * Public folder links are IMPORTED into the account before listing or
    downloading. MEGAcmd 2.x cannot walk a raw public link — it fails with
    "Couldn't find". The imported folder is removed again when the job ends.

  * Listing uses `mega-find --type=f`, which prints one clean full path per
    line, instead of parsing `mega-ls -R`'s directory-header blocks. The
    header parsing was fragile and got paths wrong.

  * `mega-get` is treated as successful whenever the file landed with a
    non-zero size, even if it exited non-zero — it reports "Already exists"
    after a completed transfer. Each download also gets its own temp
    directory, which is what stops that error appearing in the first place.

On top of that, `download_file` streams mega-get's output rather than waiting
for it, which is what drives the live progress bar and lets /cancel kill a
transfer mid-file.
"""
import asyncio
import os
import re
import shutil
import uuid

import config

# MEGA links look like:
#   https://mega.nz/folder/<id>#<key>       (folder)
#   https://mega.nz/file/<id>#<key>         (single file)
#   https://mega.nz/#F!<id>!<key>           (legacy folder)
#   https://mega.nz/#!<id>!<key>            (legacy file)
LINK_RE = re.compile(
    r"^https?://mega(?:\.co)?\.nz/(?:folder/|file/|#F?!)[\w\-]+[#!][\w\-,]+",
    re.IGNORECASE,
)

# "(1234/5678 MB: 45.67 %)" or "(45.67 %)" depending on version/verbosity.
PROGRESS_RE = re.compile(
    r"\((?:(?P<done>[\d.]+)\s*/\s*(?P<total>[\d.]+)\s*(?P<unit>[KMGT]?B)\s*:\s*)?"
    r"(?P<pct>[\d.]+)\s*%\)",
    re.IGNORECASE,
)
UNIT_SCALE = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}


class Cancelled(Exception):
    """Raised when a transfer was killed by the user's /cancel."""


def is_mega_link(text: str) -> bool:
    return bool(LINK_RE.match((text or "").strip()))


def link_label(link: str) -> str:
    """A short human label for a link, used as a task title fallback."""
    tail = link.split("/")[-1]
    return tail.split("#")[0][:24] or "MEGA link"


async def _run(cmd, timeout=None):
    """Run a command; return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise
    return (
        proc.returncode,
        out.decode(errors="replace"),
        err.decode(errors="replace"),
    )


async def login() -> None:
    """Log in to the MEGA account. Safe to call on every startup."""
    code, out, err = await _run(
        ["mega-login", config.MEGA_EMAIL, config.MEGA_PASSWORD], timeout=180
    )
    blob = (out + err).lower()
    if code != 0 and "already logged in" not in blob:
        raise RuntimeError(f"mega-login failed: {err.strip() or out.strip()}")


async def whoami() -> str:
    _, out, err = await _run(["mega-whoami"], timeout=60)
    return (out or err).strip()


# ----------------------------------------------------------------------
# Importing public links into the account
# ----------------------------------------------------------------------
async def _import_folder(link: str) -> str:
    """Import a public folder link and return the remote path it landed on."""
    code, out, err = await _run(["mega-import", link], timeout=300)
    blob = out + err
    match = re.search(r"Imported folder complete:\s*(.+)", blob)
    if match:
        return match.group(1).strip()
    if code != 0:
        raise RuntimeError(f"mega-import failed: {blob.strip()}")
    raise RuntimeError(f"Could not parse imported path from: {blob.strip()}")


async def _remove_imported(remote_path: str) -> None:
    """Remove an imported folder from the account (cleanup)."""
    try:
        await _run(["mega-rm", "-r", "-f", remote_path], timeout=120)
    except Exception:
        pass


async def cleanup(remote_path: str) -> None:
    """Remove the imported folder from the MEGA account after a job completes."""
    if remote_path:
        await _remove_imported(remote_path)


async def raw_ls(link: str) -> str:
    """Return the raw recursive listing for a folder link (used by /probe)."""
    remote_path = await _import_folder(link)
    try:
        _, out, err = await _run(["mega-ls", "-l", "-R", remote_path], timeout=300)
        return out or err
    finally:
        await _remove_imported(remote_path)


async def list_folder(link: str):
    """
    Import a public folder link, then return `(remote_path, files)` where
    files are paths relative to the imported folder, recursively.

    The caller owns `remote_path` from here on and must pass it to
    `cleanup()` when the job ends, successfully or not.
    """
    remote_path = await _import_folder(link)

    code, out, err = await _run(["mega-find", remote_path, "--type=f"], timeout=600)
    if code != 0:
        await _remove_imported(remote_path)
        raise RuntimeError(f"mega-find failed: {err.strip() or out.strip()}")

    prefix = remote_path.rstrip("/") + "/"
    files = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(prefix):
            files.append(line[len(prefix):])

    return remote_path, files


async def preview_folder(link: str):
    """
    Read a folder's contents for /list without starting a job: import, walk,
    then hand the import straight back. Returns [{"path", "size"}, ...].
    """
    remote_path = await _import_folder(link)
    try:
        code, out, err = await _run(["mega-find", remote_path, "--type=f"], timeout=600)
        if code != 0:
            raise RuntimeError(f"mega-find failed: {err.strip() or out.strip()}")
        prefix = remote_path.rstrip("/") + "/"
        paths = [
            line.strip()[len(prefix):]
            for line in out.splitlines()
            if line.strip().startswith(prefix)
        ]
        sizes = await folder_sizes(remote_path)
        return [{"path": p, "size": sizes.get(p, 0)} for p in paths]
    finally:
        await _remove_imported(remote_path)


async def folder_sizes(remote_path: str) -> dict:
    """
    Best-effort {relative path: size} map for an imported folder.

    Used only to show byte totals in the dashboard, so a parse miss costs
    nothing — `mega-find` remains the source of truth for which files exist.
    """
    sizes = {}
    try:
        code, out, _ = await _run(["mega-ls", "-l", "-R", remote_path], timeout=300)
        if code != 0:
            return sizes
        current = ""
        base = remote_path.rstrip("/")
        for line in out.splitlines():
            stripped = line.rstrip()
            if stripped.endswith(":"):
                header = stripped[:-1].strip()
                current = header[len(base):].strip("/") if header.startswith(base) else ""
                continue
            match = re.match(r"^\s*\S+\s+\S*\s*(\d+)\s+\S+\s+\S+\s+(.+?)\s*$", line)
            if match:
                rel = f"{current}/{match.group(2).strip()}" if current else match.group(2).strip()
                sizes[rel] = int(match.group(1))
    except Exception:
        pass
    return sizes


# ----------------------------------------------------------------------
# Downloading
# ----------------------------------------------------------------------
async def download_file(remote_path, rel_path, dest_dir,
                        on_progress=None, cancel=None, expected_size=0):
    """
    Download a single file from the imported folder in the MEGA cloud.

    `remote_path` is the MEGA cloud path returned by list_folder(), and
    `rel_path` is the file's path relative to it.

    `on_progress(done_bytes, total_bytes)` is called as the transfer streams,
    and `cancel` (an asyncio.Event) kills mega-get when set, so /cancel takes
    effect mid-file instead of waiting for a 40 GB download to finish.

    Some MEGAcmd builds print a bare percentage instead of a byte count. In
    that case `expected_size` turns the percentage back into bytes; without
    it we stay quiet rather than reporting a meaningless "50 B of 100 B".
    """
    os.makedirs(dest_dir, exist_ok=True)
    # A fresh temp directory per download avoids MEGAcmd's "Already exists"
    # error from leftover files or temp artifacts.
    tmp_dir = os.path.join(dest_dir, f"_tmp_{uuid.uuid4().hex[:8]}")
    os.makedirs(tmp_dir, exist_ok=True)

    full_remote = f"{remote_path.rstrip('/')}/{rel_path}"
    proc = await asyncio.create_subprocess_exec(
        "mega-get", full_remote, tmp_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    tail = ""          # last chunk of output, kept for the error message
    killed = False

    async def _pump():
        nonlocal tail
        buf = ""
        while True:
            chunk = await proc.stdout.read(512)
            if not chunk:
                break
            text = chunk.decode(errors="replace")
            tail = (tail + text)[-2000:]
            buf += text
            # mega-get redraws its progress line with \r, so split on both.
            pieces = re.split(r"[\r\n]", buf)
            buf = pieces.pop()             # keep the incomplete tail
            for piece in pieces:
                match = None
                for match in PROGRESS_RE.finditer(piece):
                    pass                   # keep only the last match on the line
                if match and on_progress:
                    done_raw, total_raw = match.group("done"), match.group("total")
                    if done_raw and total_raw:
                        scale = UNIT_SCALE.get((match.group("unit") or "B").upper(), 1)
                        on_progress(float(done_raw) * scale, float(total_raw) * scale)
                    elif expected_size:
                        fraction = float(match.group("pct")) / 100.0
                        on_progress(fraction * expected_size, expected_size)

    async def _watch_cancel():
        nonlocal killed
        while proc.returncode is None:
            if cancel is not None and cancel.is_set():
                killed = True
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                return
            await asyncio.sleep(1)

    watcher = asyncio.ensure_future(_watch_cancel()) if cancel is not None else None
    try:
        await _pump()
        await proc.wait()
    finally:
        if watcher is not None:
            watcher.cancel()

    if killed:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise Cancelled("download cancelled by user")

    basename = os.path.basename(rel_path)
    found = None
    for root, _dirs, names in os.walk(tmp_dir):
        if basename in names:
            candidate = os.path.join(root, basename)
            if os.path.getsize(candidate) > 0:
                found = candidate
                break

    # MEGAcmd sometimes reports "Already exists" even after a 100% transfer.
    # If the file is there with non-zero size, treat it as success.
    if not found:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise RuntimeError(f"mega-get failed for {rel_path}: {tail.strip()[-400:]}")

    final = os.path.join(dest_dir, basename)
    if os.path.exists(final):
        os.remove(final)
    shutil.move(found, final)
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return final
