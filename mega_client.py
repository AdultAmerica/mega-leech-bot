"""
Thin wrapper around MEGAcmd (the official MEGA command-line tool).

MEGAcmd exposes commands prefixed with "mega-" (mega-login, mega-ls,
mega-get, ...). Any of them auto-starts the background MEGAcmd server, so we
just shell out to them with asyncio subprocesses.

The one piece that can vary between MEGAcmd versions / folder types is how the
recursive listing is printed, which `list_folder()` parses. If your first real
run reports "no files found", use the bot's /probe command to dump the raw
`mega-ls` text — then the parser below can be matched to it exactly.
"""
import asyncio
import os

import config


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


async def raw_ls(link: str) -> str:
    """Return the raw recursive listing for a folder link (used by /probe)."""
    _, out, err = await _run(["mega-ls", "-l", "-R", link], timeout=300)
    return out or err


async def list_folder(link: str):
    """
    Return a list of file paths *relative to the folder link*, recursively.

    MEGAcmd's `mega-ls -R` prints one directory block at a time, e.g.:

        .:
        movie_a.mkv
        subdir

        ./subdir:
        movie_b.mkv

    We track the current directory header (a line ending in ':') and join it
    with each entry under it. Entries that are themselves directories (they
    appear again later as their own header) are skipped, so only leaf files
    are returned.
    """
    code, out, err = await _run(["mega-ls", "-R", link], timeout=600)
    if code != 0:
        raise RuntimeError(f"mega-ls failed: {err.strip() or out.strip()}")

    # First pass: collect every directory header so we can exclude folders.
    headers = set()
    for line in out.splitlines():
        if line.rstrip().endswith(":"):
            headers.add(line.rstrip()[:-1].strip().lstrip("./").rstrip("/"))

    files = []
    current = ""
    for line in out.splitlines():
        line = line.rstrip()
        if not line:
            continue
        if line.endswith(":"):
            current = line[:-1].strip().lstrip("./").rstrip("/")
            continue
        name = line.strip()
        rel = f"{current}/{name}" if current else name
        rel = rel.lstrip("/")
        if rel in headers or name in headers:
            continue  # this entry is a directory, not a file
        files.append(rel)

    return files


async def download_folder(link: str, dest_dir: str) -> None:
    """
    Download an entire MEGA public folder into dest_dir.

    MEGAcmd does not support downloading individual files from a public
    folder link by appending filenames — only the /file/FILEID form works,
    and we don't have those IDs.  Downloading the whole folder is the
    reliable approach.
    """
    os.makedirs(dest_dir, exist_ok=True)
    code, out, err = await _run(["mega-get", link, dest_dir], timeout=None)
    if code != 0:
        raise RuntimeError(f"mega-get failed: {err.strip() or out.strip()}")
