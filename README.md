# MEGA → Telegram Leech Bot

Downloads a MEGA folder and uploads every file to all Telegram groups and
channels the bot has been added to. Built to run on Railway.

## What it does

- **Auto-tracks destinations.** Add the bot to a group, or make it an admin in
  a channel, and it remembers that chat. Remove it and it stops dumping there.
  No hardcoded chat list.
- **Uploads once, fans out by file id.** A file is uploaded a single time and
  then copied to every other chat without re-uploading, so you pay upload
  bandwidth (Railway egress) only once.
- **Handles big files.** Anything over ~2 GB is split into parts automatically
  (Telegram's bot upload limit).
- **Streams one file at a time.** It never needs disk for the whole folder —
  only for the largest single file — so a 400 GB folder needs the same small
  volume as a tiny one.
- **Resumes after restarts.** Progress is saved in SQLite; if Railway restarts
  mid-job, the bot continues from the next unfinished file.
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

## Deploy on Railway

1. Push this folder to a GitHub repository.
2. On [Railway](https://railway.app): **New Project → Deploy from GitHub repo**,
   and pick the repo. Railway will detect the `Dockerfile` automatically.
3. Open the service → **Variables**, and add every name from `.env.example`
   with your real values (do **not** add `DATA_DIR` yet).
4. Open **Settings → Volumes → New Volume**, and set the mount path to `/data`.
   Then add a variable `DATA_DIR=/data`. The volume gives you persistent disk
   for downloads and lets jobs resume after a restart.
   - Size it to **your largest single file + ~4 GB** (e.g. 20 GB is plenty
     unless you have very large individual files).
5. **Deploy.** Watch the build logs — the one step that occasionally needs a
   tweak is the MEGAcmd download in the `Dockerfile` (see the comment there).
6. When the deploy logs show `Bot @yourbot started.`, message your bot `/start`.

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

## Cost note (Railway)

Railway meters egress (~$0.05/GB). Because files are uploaded once and copied,
egress ≈ the folder size — roughly $10–20 for a 200–400 GB run, regardless of
how many chats you dump to. Railway has no hard spending cap, so set a usage
alert in your account if you want a safety net.

## Files

| File | Purpose |
|---|---|
| `bot.py` | handlers, auto-track, the leech pipeline, startup |
| `mega_client.py` | MEGAcmd wrapper (login / list / download) |
| `db.py` | SQLite: tracked chats + resume state |
| `utils.py` | sizes, progress throttle, file splitter |
| `config.py` | loads settings from environment variables |
| `Dockerfile` | installs MEGAcmd + Python deps |
| `railway.json` | tells Railway to use the Dockerfile |
