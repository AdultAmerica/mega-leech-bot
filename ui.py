"""
The visual layer.

Everything the user sees is built here so the whole bot shares one look:
the same progress bars, the same section rules, the same icon vocabulary.
All output is HTML (Telegram's HTML parse mode), so any value that came from
outside — a filename, a chat title, an error string — must go through `esc()`
before it is interpolated.

Nothing in this module touches Telegram or the database; it turns state into
strings, which makes every panel here trivial to eyeball in a REPL.
"""
import html
import time

# ----------------------------------------------------------------------
# Icon vocabulary — one place to restyle the entire bot
# ----------------------------------------------------------------------
E = {
    "brand": "🌩",
    "rocket": "🚀",
    "down": "📥",
    "up": "📤",
    "split": "✂️",
    "file": "📄",
    "folder": "📁",
    "box": "📦",
    "ok": "✅",
    "fail": "❌",
    "warn": "⚠️",
    "info": "ℹ️",
    "clock": "⏱",
    "hourglass": "⏳",
    "speed": "⚡️",
    "queue": "🗂",
    "chat": "💬",
    "gear": "⚙️",
    "chart": "📊",
    "cpu": "🧠",
    "ram": "💾",
    "disk": "🗄",
    "net": "🌐",
    "user": "👤",
    "key": "🔑",
    "link": "🔗",
    "stop": "🛑",
    "pause": "⏸",
    "bullet": "•",
    "arrow": "➤",
    "spark": "✨",
    "log": "📜",
    "restart": "🔄",
    "ping": "📡",
}

# Progress-bar character sets, selectable at runtime from /settings.
BAR_STYLES = {
    "blocks": ("█", "░"),
    "shades": ("▰", "▱"),
    "classic": ("■", "□"),
    "dots": ("●", "○"),
    "circles": ("🔵", "⚪️"),
    "fire": ("🟧", "⬛️"),
}

# Frames for the little "still alive" spinner in live dashboards.
SPINNER = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

RULE = "━━━━━━━━━━━━━━━━━━━━━"

# Per-state header decoration for a task dashboard.
STATE_LOOK = {
    "queued": ("🕐", "QUEUED"),
    "listing": ("🔍", "READING FOLDER"),
    "downloading": ("📥", "DOWNLOADING"),
    "splitting": ("✂️", "SPLITTING"),
    "uploading": ("📤", "UPLOADING"),
    "done": ("✅", "COMPLETED"),
    "error": ("❌", "FAILED"),
    "cancelled": ("🛑", "CANCELLED"),
}


# ----------------------------------------------------------------------
# Primitives
# ----------------------------------------------------------------------
def esc(text) -> str:
    """HTML-escape any value headed for a message body."""
    return html.escape(str(text), quote=False)


def human(num) -> str:
    """Bytes as a compact human string: 1.4 GB."""
    try:
        num = float(num)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024.0:
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.2f} {unit}"
        num /= 1024.0
    return f"{num:.2f} PB"


def human_time(seconds) -> str:
    """Seconds as `2h 3m 4s`, trimmed to the two largest useful units."""
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return "-"
    if seconds < 0:
        return "-"
    if seconds < 1:
        return "0s"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:
        parts += [f"{days}d", f"{hours}h"]
    elif hours:
        parts += [f"{hours}h", f"{minutes}m"]
    elif minutes:
        parts += [f"{minutes}m", f"{secs}s"]
    else:
        parts = [f"{secs}s"]
    return " ".join(parts)


def speed(bytes_per_sec) -> str:
    return f"{human(bytes_per_sec)}/s"


def bar(fraction: float, length: int = 14, style: str = "blocks") -> str:
    """Render a progress bar for a 0..1 fraction."""
    filled_ch, empty_ch = BAR_STYLES.get(style, BAR_STYLES["blocks"])
    fraction = min(max(float(fraction or 0.0), 0.0), 1.0)
    length = max(4, min(int(length or 14), 24))
    filled = int(round(fraction * length))
    return filled_ch * filled + empty_ch * (length - filled)


def pct(current, total) -> float:
    if not total:
        return 0.0
    return min(max(current / total, 0.0), 1.0)


def spinner(seconds=None) -> str:
    idx = int((seconds if seconds is not None else time.time()) * 4) % len(SPINNER)
    return SPINNER[idx]


def trunc(text, limit: int = 44) -> str:
    """Shorten a filename in the middle so the extension stays visible."""
    text = str(text)
    if len(text) <= limit:
        return text
    keep = (limit - 3) // 2
    return f"{text[:keep]}…{text[-keep:]}"


def header(title: str, icon: str = "") -> str:
    icon = f"{icon} " if icon else ""
    return f"<b>{icon}{esc(title)}</b>\n{RULE}"


def kv(label: str, value, icon: str = E["bullet"]) -> str:
    """One aligned `icon label · value` row."""
    return f"{icon} <b>{esc(label)}</b>  <code>{esc(value)}</code>"


def panel(title: str, rows, icon: str = "", footer: str = "") -> str:
    """A titled block with a rule under the header and optional footer note."""
    body = "\n".join(r for r in rows if r is not None)
    out = f"{header(title, icon)}\n{body}"
    if footer:
        out += f"\n{RULE}\n<i>{footer}</i>"
    return out


# ----------------------------------------------------------------------
# Panels
# ----------------------------------------------------------------------
def welcome(name: str, brand: str, chats: int, version: str) -> str:
    return (
        f"{E['brand']} <b>{esc(brand)}</b>\n"
        f"{RULE}\n"
        f"Hey <b>{esc(name)}</b> {E['spark']}\n\n"
        f"I pull whole folders out of MEGA and drop every file into the "
        f"chats I'm in — uploading each file <b>once</b> and fanning it out "
        f"by file id, so bandwidth is paid a single time.\n\n"
        f"{kv('Destinations', f'{chats} chat(s)', E['chat'])}\n"
        f"{kv('Version', version, E['info'])}\n"
        f"{RULE}\n"
        f"<i>Tap a button below, or send</i> <code>/leech &lt;mega url&gt;</code>"
    )


def task_panel(t, settings, live: bool = True) -> str:
    """
    The live dashboard for one task. `t` is a tasks.Task; only attributes are
    read here so the renderer stays independent of the queue internals.
    """
    icon, label = STATE_LOOK.get(t.state, ("🔹", t.state.upper()))
    style = settings.get("bar_style", "blocks")
    length = int(settings.get("bar_length", 14))
    spin = ""
    if live and settings.get("animate") and t.state in (
        "listing", "downloading", "splitting", "uploading"
    ):
        spin = f" {spinner()}"

    lines = [f"<b>{icon} {label}</b>{spin}   <code>#{esc(t.id)}</code>", RULE]

    if t.title:
        lines.append(f"{E['folder']} <b>{esc(trunc(t.title, 48))}</b>")

    # Current file + its own bar
    if t.file_name:
        lines.append(f"{E['file']} <code>{esc(trunc(t.file_name, 46))}</code>")
    if t.state in ("downloading", "uploading", "splitting") and t.current_total:
        frac = pct(t.current_done, t.current_total)
        lines.append(f"<code>{bar(frac, length, style)}</code> <b>{frac * 100:.1f}%</b>")
        lines.append(
            f"{E['box']} {human(t.current_done)} / {human(t.current_total)}"
            f"   {E['speed']} {speed(t.speed)}"
        )
        lines.append(
            f"{E['hourglass']} ETA {human_time(t.eta)}"
            f"   {E['clock']} {human_time(time.time() - t.started_at)}"
        )
    elif t.state == "queued":
        pos = f"#{t.queue_pos}" if t.queue_pos else "next"
        lines.append(f"{E['queue']} Waiting in queue — position <b>{esc(pos)}</b>")
    elif t.state == "listing":
        lines.append(f"{E['hourglass']} Asking MEGA for the folder tree…")

    # Overall job progress
    if t.file_total:
        overall = pct(t.files_done, t.file_total)
        lines.append(RULE)
        lines.append(
            f"{E['chart']} <b>Overall</b>  "
            f"<code>{bar(overall, length, style)}</code> "
            f"{t.files_done}/{t.file_total} files"
        )
        if t.bytes_total:
            lines.append(
                f"{E['box']} {human(t.bytes_done)} / {human(t.bytes_total)}"
                f"   {E['up']} {human(t.bytes_sent)} sent"
            )
    if t.failed:
        lines.append(f"{E['warn']} <b>{len(t.failed)}</b> file(s) failed — see /status")

    if t.state == "done":
        lines.append(RULE)
        lines.append(
            f"{E['ok']} <b>{t.files_done}</b> file(s) → <b>{t.chat_count}</b> chat(s) "
            f"in <b>{human_time(t.duration)}</b>"
        )
        if t.bytes_sent:
            lines.append(f"{E['up']} {human(t.bytes_sent)} uploaded")
    elif t.state == "error" and t.error:
        lines.append(RULE)
        lines.append(f"{E['fail']} <code>{esc(trunc(t.error, 220))}</code>")

    return "\n".join(lines)


def queue_panel(current, pending) -> str:
    rows = []
    if current:
        icon, label = STATE_LOOK.get(current.state, ("🔹", current.state))
        rows.append(
            f"{icon} <b>Running</b> <code>#{esc(current.id)}</code> — "
            f"{esc(trunc(current.title or current.url, 34))}"
        )
        if current.file_total:
            rows.append(
                f"    {E['chart']} {current.files_done}/{current.file_total} files"
            )
    else:
        rows.append(f"{E['info']} Nothing is running right now.")

    if pending:
        rows.append("")
        rows.append(f"<b>{E['queue']} Queued ({len(pending)})</b>")
        for i, t in enumerate(pending, start=1):
            rows.append(
                f"  <b>{i}.</b> <code>#{esc(t.id)}</code> "
                f"{esc(trunc(t.title or t.url, 34))}"
            )
    else:
        rows.append("")
        rows.append(f"{E['bullet']} Queue is empty.")
    return panel("TASK QUEUE", rows, E["queue"])


def chats_panel(chats, page: int, pages: int, total: int) -> str:
    if not chats:
        return panel(
            "DESTINATIONS",
            [
                f"{E['info']} I'm not in any chats yet.",
                "",
                "Add me to a group, or make me an <b>admin</b> in a channel, "
                "and I'll track it automatically.",
            ],
            E["chat"],
        )
    rows = []
    for c in chats:
        mark = E["ok"] if c["enabled"] else E["pause"]
        thread = f" · topic <code>{c['thread_id']}</code>" if c["thread_id"] else ""
        rows.append(
            f"{mark} <b>{esc(trunc(c['title'] or c['chat_id'], 32))}</b>{thread}\n"
            f"    <code>{esc(c['chat_id'])}</code> · {esc(c['type'])}"
        )
    return panel(
        "DESTINATIONS",
        rows,
        E["chat"],
        footer=f"Page {page + 1}/{pages} · {total} tracked · tap a chat to toggle it",
    )


def settings_panel(s) -> str:
    style = s.get("bar_style", "blocks")
    preview = bar(0.62, int(s.get("bar_length", 14)), style)
    rows = [
        kv("Upload mode", s.get("upload_mode"), E["up"]),
        kv("Split size", human(int(s.get("split_size", 0))), E["split"]),
        kv("Fan-out copies", "on" if s.get("fanout") else "off", E["chat"]),
        kv("Silent delivery", "on" if s.get("silent") else "off", "🔕"),
        kv("Thumbnail", "on" if s.get("use_thumb") else "off", "🖼"),
        kv("Retries per file", s.get("retries"), "♻️"),
        kv("Finish DM", "on" if s.get("notify_done") else "off", "🔔"),
        RULE,
        kv("Bar style", style, E["chart"]),
        kv("Refresh", f"{s.get('refresh_secs')}s", E["clock"]),
        kv("Animation", "on" if s.get("animate") else "off", E["spark"]),
        f"{E['bullet']} <b>Preview</b>  <code>{preview}</code> <b>62.0%</b>",
        RULE,
        kv("Caption style", s.get("caption_style"), "🏷"),
        kv("Prefix", s.get("caption_prefix") or "—", "🔤"),
        kv("Suffix", s.get("caption_suffix") or "—", "🔡"),
    ]
    return panel("SETTINGS", rows, E["gear"], footer="Tap any row below to change it")


def stats_panel(st, sysinfo, uptime, version) -> str:
    rows = [
        kv("Jobs completed", st.get("jobs_done", 0), E["ok"]),
        kv("Jobs failed", st.get("jobs_failed", 0), E["fail"]),
        kv("Files delivered", st.get("files_done", 0), E["file"]),
        kv("Data uploaded", human(st.get("bytes_sent", 0)), E["up"]),
        kv("Data downloaded", human(st.get("bytes_down", 0)), E["down"]),
        RULE,
        kv("Uptime", human_time(uptime), E["clock"]),
        kv("Version", version, E["info"]),
    ]
    if sysinfo:
        rows.append(RULE)
        rows.extend(sysinfo_rows(sysinfo))
    return panel("STATISTICS", rows, E["chart"])


def sysinfo_rows(info):
    rows = []
    for key, label, icon in (
        ("cpu", "CPU", E["cpu"]),
        ("ram", "RAM", E["ram"]),
        ("disk", "Disk", E["disk"]),
    ):
        value = info.get(key)
        if value is None:
            continue
        rows.append(
            f"{icon} <b>{label}</b> <code>{bar(value / 100.0, 10)}</code> "
            f"{value:.0f}%" + (f"  <i>{esc(info.get(key + '_note'))}</i>"
                               if info.get(key + "_note") else "")
        )
    if info.get("net"):
        rows.append(f"{E['net']} <b>Net</b>  <code>{esc(info['net'])}</code>")
    if info.get("load"):
        rows.append(f"{E['speed']} <b>Load</b>  <code>{esc(info['load'])}</code>")
    return rows


def listing_panel(title, entries, total_bytes, page, pages, shown_from) -> str:
    rows = []
    for i, f in enumerate(entries, start=shown_from + 1):
        rows.append(
            f"<b>{i}.</b> <code>{esc(trunc(f['path'], 40))}</code>"
            + (f"  <i>{human(f['size'])}</i>" if f.get("size") else "")
        )
    return panel(
        "FOLDER PREVIEW",
        [f"{E['folder']} <b>{esc(trunc(title, 46))}</b>", RULE] + rows,
        E["folder"],
        footer=f"Page {page + 1}/{pages} · {human(total_bytes)} total",
    )


HELP_PAGES = {
    "main": (
        "HELP",
        E["info"],
        [
            "Pick a section below, or use these every day:",
            "",
            f"{E['rocket']} <code>/leech &lt;mega url&gt;</code> — queue a folder or file",
            f"{E['folder']} <code>/list &lt;mega url&gt;</code> — preview before leeching",
            f"{E['chart']} <code>/status</code> — the live dashboard",
            f"{E['queue']} <code>/queue</code> — everything waiting",
            f"{E['chat']} <code>/chats</code> — manage destinations",
            f"{E['gear']} <code>/settings</code> — look and behaviour",
        ],
    ),
    "leech": (
        "LEECHING",
        E["rocket"],
        [
            f"{E['arrow']} <code>/leech &lt;url&gt;</code>",
            "   Queue a MEGA folder or file link. Multiple links queue up and "
            "run one after another.",
            "",
            f"{E['arrow']} <code>/leech &lt;url&gt; | My Title</code>",
            "   Same, but labels the task so dashboards read nicely.",
            "",
            f"{E['arrow']} <code>/list &lt;url&gt;</code>",
            "   Paginated preview with per-file sizes and a <b>Start</b> button.",
            "",
            f"{E['arrow']} <code>/probe &lt;url&gt;</code>",
            "   Raw <code>mega-ls</code> text, for when a listing looks wrong.",
            "",
            f"{E['arrow']} <code>/cancel [id|all]</code>",
            "   Stop the running task after the current file, or drop a queued one.",
        ],
    ),
    "chats": (
        "DESTINATIONS",
        E["chat"],
        [
            "I track chats automatically — add me to a group, or make me an "
            "<b>admin</b> in a channel, and it appears in <code>/chats</code>.",
            "",
            f"{E['arrow']} Tap a chat in <code>/chats</code> to pause or resume it "
            "without removing me.",
            f"{E['arrow']} <code>/addchat &lt;chat_id&gt;</code> for the ones "
            "auto-tracking missed.",
            f"{E['arrow']} <code>/remove &lt;chat_id&gt;</code> drops one entirely.",
            f"{E['arrow']} <code>/topic &lt;chat_id&gt; &lt;topic_id&gt;</code> pins "
            "delivery to one forum topic (<code>0</code> clears it).",
            "",
            "The first enabled chat gets the real upload; the rest receive a "
            "copy by file id, which costs no extra bandwidth.",
        ],
    ),
    "settings": (
        "SETTINGS",
        E["gear"],
        [
            f"{E['arrow']} <b>Upload mode</b> — <code>document</code> keeps files "
            "pristine; <code>media</code> gives video/audio a player.",
            f"{E['arrow']} <b>Split size</b> — parts for files above Telegram's limit.",
            f"{E['arrow']} <b>Captions</b> — <code>/prefix</code>, <code>/suffix</code> "
            "and caption style shape every delivered file.",
            f"{E['arrow']} <b>Thumbnail</b> — reply to a photo with "
            "<code>/setthumb</code>; clear it with <code>/delthumb</code>.",
            f"{E['arrow']} <b>Bar style, refresh, animation</b> — how the live "
            "dashboard looks.",
        ],
    ),
    "admin": (
        "ADMIN",
        E["key"],
        [
            f"{E['arrow']} <code>/stats</code> — lifetime totals and machine health",
            f"{E['arrow']} <code>/sys</code> or <code>/disk</code> — CPU, RAM, disk",
            f"{E['arrow']} <code>/cleanup</code> — delete leftover downloads",
            f"{E['arrow']} <code>/ping</code> — round-trip latency",
            f"{E['arrow']} <code>/id</code> — ids for the current chat and user",
            f"{E['arrow']} <code>/users</code> — list sudo users (owner only)",
            f"{E['arrow']} <code>/users add &lt;id&gt;</code> / "
            f"<code>/users del &lt;id&gt;</code>",
            f"{E['arrow']} <code>/log</code> — download the log file",
            f"{E['arrow']} <code>/restart</code> — restart the bot process",
        ],
    ),
}


def help_panel(page: str) -> str:
    title, icon, rows = HELP_PAGES.get(page, HELP_PAGES["main"])
    return panel(title, rows, icon)
