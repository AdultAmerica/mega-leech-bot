# MEGA → Telegram Leech Bot

Downloads a MEGA folder and uploads every file to all Telegram groups and
channels the bot has been added to. Runs on a Hetzner VPS under Docker Compose.

## What it does

- **Auto-tracks destinations.** Add the bot to a group, or make it an admin in
  a channel, and it remembers that chat. Remove it and it stops dumping there.
  You can also pause a chat from `/chats` without removing the bot from it.
- **Uploads once, copies to the rest.** A file is uploaded a single time and
  then copied to every other chat without re-uploading, so upload bandwidth is
  paid once no matter how many chats you dump to.
- **Sends media as media.** Video, audio and images arrive with players and the
  correct aspect ratio (dimensions read with `ffprobe`), falling back to a
  plain document if Telegram won't take the file.
- **Handles big files.** Anything over the configured split size (default
  ~1.9 GB, Telegram's bot upload limit) is split into parts automatically.
- **Streams one file at a time.** It never needs disk for the whole folder —
  only for the largest single file — so a 400 GB folder needs the same small
  volume as a tiny one.
- **Live dashboards.** Every task draws a self-refreshing panel with a progress
  bar, transfer speed, ETA, per-file and overall completion, and Cancel /
  Refresh buttons.
- **Queues work.** Send several links and they run back to back; inspect and
  drop queued tasks from `/queue`.
- **Resumes after restarts.** Progress is saved in SQLite; if the container
  restarts mid-job, the bot continues from the next unfinished file.
- **Private.** Only the owner, `EXTRA_OWNERS`, and any sudo users you add can
  issue commands.

## You will need

| Value | Where to get it |
|---|---|
| `API_ID`, `API_HASH` | https://my.telegram.org → *API development tools* |
| `BOT_TOKEN` | [@BotFather](https://t.me/BotFather) → `/newbot` |
| `OWNER_ID` | [@userinfobot](https://t.me/userinfobot) (your numeric id) |
| `MEGA_EMAIL`, `MEGA_PASSWORD` | your MEGA account (Pro / Pro Lite recommended) |

Optional: `EXTRA_OWNERS` (comma-separated ids with full powers), `SUDO_USERS`
(can run jobs and change settings, but not manage users), `BRAND` (the name in
panel headers — handy when you run two bots).

## Commands

Everything is also reachable from the inline menu that `/start` opens — the
commands below are the keyboard shortcuts for it.

### Leeching

| Command | Action |
|---|---|
| `/leech <url>` | queue a MEGA folder link |
| `/leech <url> \| Title` | same, with a custom label for the dashboards |
| `/list <url>` | paginated folder preview with sizes and a **Start** button |
| `/probe <url>` | raw `mega-ls` output (for debugging a bad listing) |
| `/status` | live dashboard for the running task |
| `/queue` | everything waiting, with per-task drop buttons |
| `/cancel [id\|all]` | stop the running task, one queued task, or everything |

`/leech` also works as a reply to a message containing a link, and `/mirror`
is an alias for it. Cancelling takes effect mid-file — the transfer is killed
rather than left to finish.

### Destinations

| Command | Action |
|---|---|
| `/chats` | paginated chat manager — tap a chat to pause or resume it |
| `/addchat <chat_id>` | add a chat auto-tracking missed |
| `/remove <chat_id>` | drop a chat from the dump list |
| `/topic <chat_id> <topic_id>` | deliver into one forum topic (`0` clears) |

### Settings

| Command | Action |
|---|---|
| `/settings` | upload mode, split size, captions, bar style, refresh rate |
| `/prefix <text>` | text prepended to every caption (`off` clears it) |
| `/suffix <text>` | text appended to every caption (`off` clears it) |
| `/setthumb` | reply to a photo to use it as the upload thumbnail |
| `/delthumb` | remove the saved thumbnail |

### Admin

| Command | Action |
|---|---|
| `/stats` | lifetime totals plus CPU / RAM / disk |
| `/sys`, `/disk` | machine health and the downloads folder |
| `/cleanup` | delete leftover downloads |
| `/ping` | round-trip latency |
| `/id` | ids for the current chat, topic and user (public) |
| `/users` | list authorized users; `add`/`del` are owner-only |
| `/log` | download the rotating log file |
| `/restart` | restart the bot process (owner only) |

## Settings reference

These are stored in SQLite and changed live from `/settings` — no redeploy,
no environment variables to edit.

| Setting | Meaning |
|---|---|
| **Upload mode** | `media` gives video/audio a player; `document` keeps files byte-exact |
| **Split size** | part size for files above Telegram's limit (1900 / 1500 / 1000 / 500 MB) |
| **Fan-out** | copy each upload to the other chats |
| **Silent** | deliver with notifications disabled |
| **Thumbnail** | attach the saved thumbnail to uploads |
| **Finish DM** | message the requester when a task ends |
| **Retries** | download attempts per file before it's recorded as failed |
| **Caption style** | `mono`, `bold` or `plain`, plus the prefix/suffix |
| **Bar style** | `blocks`, `shades`, `classic`, `dots`, `circles`, `fire` |
| **Refresh** | how often a live dashboard redraws (3–15 s) |
| **Animation** | spinner in the dashboard header |

## Which folder is live on the VPS

The bot runs from **`/opt/mega-leech-bot`**, on the branch
`claude/leech-bot-telegram-deploy-9mlnve`. Always run the `git` and
`docker compose` commands there.

Older clones may exist on the server (e.g. `/root/mega-leech-bot`,
`/opt/leechbot`) — these are stale and **not** what's running. Ignore them, or
remove them once you've confirmed everything works, to avoid editing the wrong
copy:

```bash
# confirm which one is live first — this is the deployed folder:
cd /opt/mega-leech-bot && git rev-parse --abbrev-ref HEAD
```

Note that `master` is **not** the deployment branch and lags well behind it.

## Deploy

First time on a fresh server, `deploy.sh` does everything (installs Docker,
clones the repo, prompts for credentials, builds and starts):

```bash
bash deploy.sh
```

To ship a code update to a server that's already running:

```bash
cd /opt/mega-leech-bot
git pull
docker compose up -d --build          # add --profile bot2 if you run two bots
docker compose logs -f
```

Wait for `Bot @yourbot started (v2.0).`, then message the bot `/start`.

The database, the MEGAcmd session and in-progress downloads live on the
`bot-data` named volume, so they all survive the rebuild. Size the server's
disk to **your largest single file + ~4 GB**.

### Health check

The container serves a plain-text status page on `PORT` (8080 by default,
not published by compose):

```
OK
uptime=1841s
running=a1b2c3
queued=2
```

## Add a second bot for another user (docker-compose)

Want a second person to run their own leech bot — their own commands, their own
channels — separate from yours? Add a second bot instance. Each bot is fully
isolated: its own Telegram token, its own owner, its own destination chats, its
own database, and its own MEGAcmd session (its own volume). This is different
from `EXTRA_OWNERS`, which lets extra people command *your* bot and share *your*
destination chats — use a second bot when the other user needs their **own**
space.

Why a second container instead of one bot? The download side uses **MEGAcmd**,
which keeps a single background session per container. One container per bot
gives each its own session, so they never step on each other — even when both
log into the **same** MEGA account (MEGA allows several sessions per account).

The second bot is already defined in `docker-compose.yml` under the `bot2`
profile, so it stays off until you switch it on. On the VPS (in
`/opt/mega-leech-bot`):

1. Create the bot in Telegram: message [@BotFather](https://t.me/BotFather),
   send `/newbot`, and copy the **token** it gives you.
2. Get the other user's numeric id: they message
   [@userinfobot](https://t.me/userinfobot) from their own account.
3. Make the second bot's env file by copying your working `.env`, then change
   just the token and owner:
   ```bash
   cp .env .env.bot2
   nano .env.bot2
   #   BOT_TOKEN=<the new token from BotFather>
   #   OWNER_ID=<the other user's numeric id>
   #   leave API_ID / API_HASH / MEGA_EMAIL / MEGA_PASSWORD / DATA_DIR as-is
   ```
   (Sharing your MEGA account is fine — just note both bots then draw from that
   account's transfer quota. For a fully separate account, put different MEGA
   credentials in `.env.bot2`.)
4. Start both bots:
   ```bash
   docker compose --profile bot2 up -d --build
   docker compose --profile bot2 logs -f      # wait for "Bot @... started." from both
   ```
   Your first bot keeps running as before; the new container
   (`mega-leech-bot2`) is the second user's bot.
5. The other user messages **their** bot `/start`, adds it to **their own**
   group/channel (as admin in channels), and runs `/leech`. Their files land
   only in their chats.

To manage just the second bot later: `docker compose --profile bot2 restart leech-bot2`
(or `stop` / `up -d`). Running `docker compose up -d` **without** `--profile bot2`
only touches bot 1, so your existing deploy flow is unchanged.

Give the two bots different `BRAND` values if you want their panels labelled
differently.

## First run — validate small

Before a big folder, test with a small public folder (2–3 files):

1. Add the bot to one test channel **as an admin**, send `/chats` to confirm it
   shows up.
2. Send `/list <small folder url>` to check the listing parses, then tap
   **Start leech**.
   - If it says *"No files found"*, send `/probe <same url>` and share the
     output.
3. Once a small folder works end to end, run your real one.

## How the MEGA side works

Public links are **imported into the account** before listing or downloading —
MEGAcmd 2.x can't walk a raw public link. Listing then uses `mega-find`, which
prints one clean path per line rather than the directory-header blocks that
`mega-ls -R` produces. Each download gets its own temp directory, and a
`mega-get` that exits non-zero but left a complete file is treated as success,
because it reports "Already exists" after a finished transfer.

The import is removed from the account when the job ends — including when it
fails or is cancelled.

## Run locally (optional)

```bash
pip install -r requirements.txt          # plus install MEGAcmd for your OS
cp .env.example .env                      # then edit .env with real values
python bot.py
```

## Cost note

A Hetzner VPS is billed at a flat monthly rate rather than metered egress, so
a leech run's bandwidth doesn't add extra cost the way it would on a
pay-per-GB host — just make sure the VPS plan's included traffic covers the
folder sizes you expect to dump.

## Files

| File | Purpose |
|---|---|
| `bot.py` | commands, button callbacks, auto-track, startup |
| `tasks.py` | the queue, the leech pipeline, live dashboard refreshers |
| `ui.py` | every panel, progress bar and icon the user sees |
| `keyboards.py` | inline keyboards and callback-data routing |
| `mega_client.py` | MEGAcmd wrapper (import / find / download with progress) |
| `db.py` | SQLite: chats, jobs, settings, users, lifetime stats |
| `utils.py` | splitter, speed meter, captions, machine health |
| `config.py` | environment settings and first-run defaults |
| `Dockerfile` | installs MEGAcmd, ffmpeg + Python deps |
| `docker-compose.yml` | the deployed services and their volumes |
| `deploy.sh` | one-shot first-time setup on a fresh server |
