#!/bin/sh
# Auto-update for the laptop server (Linux/Mac). Run every 5 minutes from cron:
#   */5 * * * * /path/to/lead-audit-bot/deploy/auto-update.sh >> /tmp/auto-update.log 2>&1
cd "$(dirname "$0")/.." || exit 1
git fetch origin main 2>/dev/null
if [ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]; then
  echo "$(date) updating"
  git pull --ff-only origin main && docker compose up -d --build
fi
