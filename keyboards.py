"""
Every inline keyboard in the bot.

Callback data is a colon-joined tuple: `<namespace>:<action>[:<arg>...]`.
Telegram caps callback data at 64 bytes, so args stay short (task ids are 6
chars, chat ids are numeric, page numbers are ints).

The router in bot.py splits on ":" and dispatches on the namespace, so adding
a button here plus a branch there is the whole story for new UI.
"""
from pyrogram.types import InlineKeyboardButton as Btn
from pyrogram.types import InlineKeyboardMarkup as Markup
from pyrogram.types import LinkPreviewOptions

import ui

CB_SEP = ":"

# Kurigram deprecated `disable_web_page_preview` in favour of this object and
# logs a warning for every call still using the old name — that's once per
# dashboard redraw, which buries the real log lines. Shared from here so bot.py
# and tasks.py send the same thing.
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


def cb(ns, action, *args) -> str:
    return CB_SEP.join([ns, action, *[str(a) for a in args]])


def parse(data):
    parts = data.split(CB_SEP)
    return parts[0], parts[1], parts[2:]


# ----------------------------------------------------------------------
# Main menu
# ----------------------------------------------------------------------
def main_menu(has_task: bool = False) -> Markup:
    rows = [
        [
            Btn(f"{ui.E['chart']} Status", cb("nav", "status")),
            Btn(f"{ui.E['queue']} Queue", cb("nav", "queue")),
        ],
        [
            Btn(f"{ui.E['chat']} Chats", cb("chats", "page", 0)),
            Btn(f"{ui.E['gear']} Settings", cb("set", "open")),
        ],
        [
            Btn(f"{ui.E['chart']} Stats", cb("nav", "stats")),
            Btn(f"{ui.E['info']} Help", cb("help", "page", "main")),
        ],
    ]
    if has_task:
        rows.insert(0, [Btn(f"{ui.E['stop']} Cancel running task", cb("task", "cancelcur"))])
    return Markup(rows)


def back_home(extra=None) -> Markup:
    rows = list(extra or [])
    rows.append([Btn(f"{ui.E['brand']} Home", cb("nav", "home"))])
    return Markup(rows)


# ----------------------------------------------------------------------
# Help
# ----------------------------------------------------------------------
def help_menu(page: str = "main") -> Markup:
    tabs = [
        ("main", f"{ui.E['info']} Overview"),
        ("leech", f"{ui.E['rocket']} Leeching"),
        ("chats", f"{ui.E['chat']} Chats"),
        ("settings", f"{ui.E['gear']} Settings"),
        ("admin", f"{ui.E['key']} Admin"),
    ]
    buttons = [
        Btn(("• " + label + " •") if key == page else label, cb("help", "page", key))
        for key, label in tabs
    ]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([Btn(f"{ui.E['brand']} Home", cb("nav", "home"))])
    return Markup(rows)


# ----------------------------------------------------------------------
# Task dashboard
# ----------------------------------------------------------------------
def task_controls(task_id, state) -> Markup:
    live = state in ("queued", "listing", "downloading", "splitting", "uploading")
    rows = []
    if live:
        rows.append([
            Btn(f"{ui.E['restart']} Refresh", cb("task", "refresh", task_id)),
            Btn(f"{ui.E['stop']} Cancel", cb("task", "cancel", task_id)),
        ])
        rows.append([
            Btn(f"{ui.E['queue']} Queue", cb("nav", "queue")),
            Btn(f"{ui.E['brand']} Home", cb("nav", "home")),
        ])
    else:
        rows.append([
            Btn(f"{ui.E['queue']} Queue", cb("nav", "queue")),
            Btn(f"{ui.E['chart']} Stats", cb("nav", "stats")),
            Btn(f"{ui.E['brand']} Home", cb("nav", "home")),
        ])
    return Markup(rows)


def queue_controls(pending) -> Markup:
    rows = []
    for t in pending[:8]:
        rows.append([
            Btn(
                f"{ui.E['fail']} Drop #{t.id} · {ui.trunc(t.title or t.url, 22)}",
                cb("task", "cancel", t.id),
            )
        ])
    rows.append([
        Btn(f"{ui.E['restart']} Refresh", cb("nav", "queue")),
        Btn(f"{ui.E['chart']} Status", cb("nav", "status")),
    ])
    if pending:
        rows.append([Btn(f"{ui.E['stop']} Clear queue", cb("task", "clearq"))])
    rows.append([Btn(f"{ui.E['brand']} Home", cb("nav", "home"))])
    return Markup(rows)


def confirm(action, *args, label="Confirm") -> Markup:
    return Markup([[
        Btn(f"{ui.E['ok']} {label}", cb("ok", action, *args)),
        Btn(f"{ui.E['fail']} Never mind", cb("nav", "close")),
    ]])


# ----------------------------------------------------------------------
# Folder preview
# ----------------------------------------------------------------------
def listing_controls(token, page, pages) -> Markup:
    nav = []
    if pages > 1:
        nav = [
            Btn("‹ Prev", cb("ls", "page", token, max(page - 1, 0))),
            Btn(f"{page + 1}/{pages}", cb("nav", "noop")),
            Btn("Next ›", cb("ls", "page", token, min(page + 1, pages - 1))),
        ]
    rows = [nav] if nav else []
    rows.append([Btn(f"{ui.E['rocket']} Start leech", cb("ls", "start", token))])
    rows.append([Btn(f"{ui.E['brand']} Home", cb("nav", "home"))])
    return Markup(rows)


# ----------------------------------------------------------------------
# Chat manager
# ----------------------------------------------------------------------
def chats_controls(chats, page, pages) -> Markup:
    rows = []
    for c in chats:
        mark = ui.E["ok"] if c["enabled"] else ui.E["pause"]
        rows.append([
            Btn(
                f"{mark} {ui.trunc(c['title'] or c['chat_id'], 26)}",
                cb("chats", "toggle", c["chat_id"], page),
            )
        ])
    if pages > 1:
        rows.append([
            Btn("‹ Prev", cb("chats", "page", max(page - 1, 0))),
            Btn(f"{page + 1}/{pages}", cb("nav", "noop")),
            Btn("Next ›", cb("chats", "page", min(page + 1, pages - 1))),
        ])
    rows.append([
        Btn(f"{ui.E['restart']} Refresh", cb("chats", "sync", page)),
        Btn(f"{ui.E['brand']} Home", cb("nav", "home")),
    ])
    return Markup(rows)


# ----------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------
BAR_ORDER = list(ui.BAR_STYLES.keys())
SPLIT_CHOICES = [1900, 1500, 1000, 500]          # MB
REFRESH_CHOICES = [3, 5, 6, 10, 15]              # seconds
RETRY_CHOICES = [1, 2, 3, 5]


def settings_controls(s) -> Markup:
    def toggle(key, label, icon):
        state = ui.E["ok"] if s.get(key) else ui.E["pause"]
        return Btn(f"{icon} {label} {state}", cb("set", "toggle", key))

    mode = s.get("upload_mode", "document")
    rows = [
        [
            Btn(f"{ui.E['up']} Mode: {mode}", cb("set", "cycle", "upload_mode")),
            Btn(
                f"{ui.E['split']} Split: {int(s.get('split_size', 0)) // (1024 * 1024)}MB",
                cb("set", "cycle", "split_size"),
            ),
        ],
        [
            toggle("fanout", "Fan-out", ui.E["chat"]),
            toggle("silent", "Silent", "🔕"),
        ],
        [
            toggle("use_thumb", "Thumb", "🖼"),
            toggle("notify_done", "Finish DM", "🔔"),
        ],
        [
            Btn(f"♻️ Retries: {s.get('retries')}", cb("set", "cycle", "retries")),
            Btn(f"🏷 Caption: {s.get('caption_style')}", cb("set", "cycle", "caption_style")),
        ],
        [
            Btn(f"{ui.E['chart']} Bar: {s.get('bar_style')}", cb("set", "cycle", "bar_style")),
            Btn(f"{ui.E['clock']} Refresh: {s.get('refresh_secs')}s", cb("set", "cycle", "refresh_secs")),
        ],
        [
            toggle("animate", "Animation", ui.E["spark"]),
            Btn(f"{ui.E['restart']} Reset", cb("set", "reset")),
        ],
        [Btn(f"{ui.E['brand']} Home", cb("nav", "home"))],
    ]
    return Markup(rows)


def next_value(key, current):
    """Cycle a settings value to its next choice."""
    if key == "upload_mode":
        return "media" if current == "document" else "document"
    if key == "caption_style":
        order = ["mono", "bold", "plain"]
        return order[(order.index(current) + 1) % len(order)] if current in order else "mono"
    if key == "bar_style":
        idx = BAR_ORDER.index(current) if current in BAR_ORDER else -1
        return BAR_ORDER[(idx + 1) % len(BAR_ORDER)]
    if key == "split_size":
        mb = int(current) // (1024 * 1024)
        choices = SPLIT_CHOICES
        idx = choices.index(mb) if mb in choices else -1
        return choices[(idx + 1) % len(choices)] * 1024 * 1024
    if key == "refresh_secs":
        idx = REFRESH_CHOICES.index(int(current)) if int(current) in REFRESH_CHOICES else -1
        return REFRESH_CHOICES[(idx + 1) % len(REFRESH_CHOICES)]
    if key == "retries":
        idx = RETRY_CHOICES.index(int(current)) if int(current) in RETRY_CHOICES else -1
        return RETRY_CHOICES[(idx + 1) % len(RETRY_CHOICES)]
    return current
