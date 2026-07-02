"""
Thin wrapper around MEGAcmd (the official MEGA command-line tool).

MEGAcmd exposes commands prefixed with "mega-" (mega-login, mega-ls,
mega-get, ...). Any of them auto-starts the background MEGAcmd server, so we
just shell out to them with asyncio subprocesses.

This version imports public folder links into the account before listing/
downloading, because MEGAcmd 2.x requires it. After downloading, the
imported folder is removed from the account to keep it clean.
"""
import asyncio
import os
import re

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
    remote_path = await _import_folder(link)
    try:
        _, out, err = await _run(["mega-ls", "-l", "-R", remote_path], timeout=300)
        return out or err
    finally:
        await _remove_imported(remote_path)


async def _import_folder(link: str) -> str:
    """Import a public folder link and return the remote path it was imported to."""
    code, out, err = await _run(["mega-import", link], timeout=300)
    blob = out + err
    m = re.search(r"Imported folder complete:\s*(.+)", blob)
    if m:
        return m.group(1).strip()
    if code != 0:
        raise RuntimeError(f"mega-import failed: {blob.strip()}")
    raise RuntimeError(f"Could not parse imported path from: {blob.strip()}")


async def _remove_imported(remote_path: str) -> None:
    """Remove an imported folder from the account (cleanup)."""
    try:
        await _run(["mega-rm", "-r", "-f", remote_path], timeout=120)
    except Exception:
        pass


async def list_folder(link: str):
    """
    Import a public folder link, then return a list of file paths relative
    to the imported folder, recursively.

    Uses mega-find instead of mega-ls to avoid fragile directory-header
    parsing — mega-find gives one clean full path per line.

    Returns (remote_path, files) where remote_path is the MEGA cloud path
    and files is a list of relative file paths.
    """
    remote_path = await _import_folder(link)

    code, out, err = await _run(
        ["mega-find", remote_path, "--type=f"], timeout=600
    )
    if code != 0:
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


async def download_file(remote_path: str, rel_path: str, dest_dir: str) -> str:
    """
    Download a single file from the imported folder in the MEGA cloud.

    remote_path is the MEGA cloud path returned by list_folder().
    rel_path is the file path relative to that folder.
    """
    os.makedirs(dest_dir, exist_ok=True)
    full_remote = f"{remote_path.rstrip('/')}/{rel_path}"
    code, out, err = await _run(["mega-get", full_remote, dest_dir], timeout=None)
    if code != 0:
        raise RuntimeError(
            f"mega-get failed for {rel_path}: {err.strip() or out.strip()}"
        )

    basename = os.path.basename(rel_path)
    direct = os.path.join(dest_dir, basename)
    if os.path.exists(direct):
        return direct
    for root, _dirs, names in os.walk(dest_dir):
        if basename in names:
            return os.path.join(root, basename)
    raise RuntimeError(f"Downloaded file not found for {rel_path}")


async def cleanup(remote_path: str) -> None:
    """Remove the imported folder from the MEGA account after a job completes."""
    await _remove_imported(remote_path)
