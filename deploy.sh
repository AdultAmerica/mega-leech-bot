#!/usr/bin/env bash
set -euo pipefail

# ─────────────────────────────────────────────────────────────
# MEGA → Telegram Leech Bot — Hetzner one-shot deploy script
#
# Run this AS ROOT on a fresh Hetzner server:
#   bash deploy.sh
#
# It will:
#   1. Install Docker + Docker Compose
#   2. Clone the repo
#   3. Prompt you for credentials (or use the .env you pre-filled)
#   4. Build & start the container with restart=unless-stopped
# ─────────────────────────────────────────────────────────────

REPO_URL="https://github.com/adultamerica/mega-leech-bot.git"
BRANCH="claude/leech-bot-telegram-deploy-9mlnve"
INSTALL_DIR="/opt/mega-leech-bot"

echo "==> Installing Docker..."
if ! command -v docker &>/dev/null; then
    curl -fsSL https://get.docker.com | sh
    systemctl enable --now docker
    echo "    Docker installed."
else
    echo "    Docker already installed."
fi

echo "==> Cloning repository..."
if [ -d "$INSTALL_DIR" ]; then
    echo "    $INSTALL_DIR already exists — pulling latest..."
    cd "$INSTALL_DIR"
    git fetch origin "$BRANCH"
    git checkout "$BRANCH"
    git reset --hard "origin/$BRANCH"
else
    git clone -b "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

echo "==> Setting up .env..."
if [ -f .env ]; then
    echo "    .env already exists — keeping it."
else
    echo "    No .env found. Let's create one."
    echo ""
    read -rp "API_ID (from my.telegram.org):          " API_ID
    read -rp "API_HASH (from my.telegram.org):        " API_HASH
    read -rp "BOT_TOKEN (from @BotFather):            " BOT_TOKEN
    read -rp "OWNER_ID (your Telegram numeric id):    " OWNER_ID
    read -rp "MEGA_EMAIL:                             " MEGA_EMAIL
    read -rsp "MEGA_PASSWORD:                          " MEGA_PASSWORD
    echo ""

    cat > .env <<EOF
API_ID=${API_ID}
API_HASH=${API_HASH}
BOT_TOKEN=${BOT_TOKEN}
OWNER_ID=${OWNER_ID}
MEGA_EMAIL=${MEGA_EMAIL}
MEGA_PASSWORD=${MEGA_PASSWORD}
DATA_DIR=/data
EOF
    chmod 600 .env
    echo "    .env created (permissions locked to root)."
fi

echo "==> Building and starting the bot..."
docker compose up -d --build

echo ""
echo "==> Done! Check status with:"
echo "    docker compose -f $INSTALL_DIR/docker-compose.yml logs -f"
echo ""
echo "    The bot will auto-restart on reboot (restart: unless-stopped)."
echo "    To stop:   docker compose -f $INSTALL_DIR/docker-compose.yml down"
echo "    To update: cd $INSTALL_DIR && git pull && docker compose up -d --build"
