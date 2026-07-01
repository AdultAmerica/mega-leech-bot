# MEGA → Telegram Leech Bot

Downloads a MEGA folder and uploads every file to all Telegram groups and
channels the bot has been added to. Built to run on a Hetzner VPS via Docker.

## What it does

- **Auto-tracks destinations.** Add the bot to a group, or make it an admin in
  a channel, and it remembers that chat. Remove it and it stops dumping there.
  No hardcoded chat list.
- **Uploads once, fans out by file id.** A file is uploaded a single time and
  then copied to every other chat without re-uploading, so you pay upload
  bandwidth only once.
- **Handles big files.** Anything over ~2 GB is split into parts automatically
  (Telegram's bot upload limit).
- **Streams one file at a time.** It never needs disk for the whole folder —
  only for the largest single file — so a 400 GB folder needs the same small
  volume as a tiny one.
- **Resumes after restarts.** Progress is saved in SQLite; if the container
  restarts mid-job, the bot continues from the next unfinished file.
- **Owner-only.** Only your Telegram user id can issue commands.

## You will need

| Value | Where to get it |
|---|---|
| `API_ID`, `API_HASH` | https://my.telegram.org → *API development tools* |
| `BOT_TOKEN` | [@BotFather](https://t.me/BotFather) → `/newbot` |
| `OWNER_ID` | [@userinfobot](https://t.me/userinfobot) (your numeric id) |
| `MEGA_EMAIL`, `MEGA_PASSWORD` | your MEGA account (Pro / Pro Lite recommended) |

## Commands (owner only)

| Command | Action |
|---|---|
| `/leech <mega folder url>` | download the folder and dump every file |
| `/chats` | list the chats it will dump to |
| `/probe <mega folder url>` | print the raw MEGA listing (debug) |
| `/cancel` | stop after the current file |
| `/start`, `/help` | show help |

## Deploy on a Hetzner VPS

1. SSH into your Hetzner VPS and install Docker (`curl -fsSL https://get.docker.com | sh`)
   if it isn't already installed.
2. Clone this repository onto the VPS:
   ```bash
   git clone <this-repo-url>
   cd mega-leech-bot
   ```
3. Copy `.env.example` to `.env` and fill in your real values:
   ```bash
   cp .env.example .env
   ```
   Set `DATA_DIR=/data` — this path is inside the container and gets mapped to
   a persistent folder on the VPS in the next step.
4. Create a folder on the VPS for persistent storage and build the image:
   ```bash
   mkdir -p /opt/mega-leech-bot/data
   docker build -t mega-leech-bot .
   ```
   - Size the disk to **your largest single file + ~4 GB** (e.g. 20 GB is
     plenty unless you have very large individual files).
5. Run the container, mounting the data folder and restarting automatically
   on reboot or crash:
   ```bash
   docker run -d --name mega-leech-bot \
     --env-file .env \
     -v /opt/mega-leech-bot/data:/data \
     --restart unless-stopped \
     mega-leech-bot
   ```
6. Watch the logs with `docker logs -f mega-leech-bot`. The one step that
   occasionally needs a tweak is the MEGAcmd download in the `Dockerfile`
   (see the comment there). When the logs show `Bot @yourbot started.`,
   message your bot `/start`.

To ship a code update later: `git pull`, `docker build -t mega-leech-bot .`,
then `docker rm -f mega-leech-bot` and re-run the `docker run` command above.

## First run — validate small

Before a big folder, test with a small public folder (2–3 files):

1. Add the bot to one test channel **as an admin**, send `/chats` to confirm it
   shows up.
2. Send `/leech <small folder url>`.
   - If it says *"No files found"*, send `/probe <same url>` and share the
     output — the listing parser in `mega_client.py` can be matched to it.
3. Once a small folder works end to end, run your real one.

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
| `bot.py` | handlers, auto-track, the leech pipeline, startup |
| `mega_client.py` | MEGAcmd wrapper (login / list / download) |
| `db.py` | SQLite: tracked chats + resume state |
| `utils.py` | sizes, progress throttle, file splitter |
| `config.py` | loads settings from environment variables |
| `Dockerfile` | installs MEGAcmd + Python deps |
