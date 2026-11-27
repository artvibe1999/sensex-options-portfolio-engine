#!/usr/bin/env bash
# Install/upgrade the engine as a second container on the sxbot VM.
# Usage (on the VM): bash ~/sxhedge/deploy/install-on-vm.sh
# Expects the repo archive already extracted into ~/sxhedge.
set -euo pipefail
APP="$HOME/sxhedge"
PRIMARY_ENV="$HOME/sxbot/.env"
cd "$APP/deploy"
mkdir -p data
if [ ! -f .env ]; then
  # Same Kite app as the primary bot: copy only the API key, never the secret/token.
  grep -E '^KITE_API_KEY=' "$PRIMARY_ENV" > .env
  sed -n '/^KITE_STATIC_IP_CONFIRMED/,$p' .env.example | grep -v '^KITE_API_KEY=' >> .env
  chmod 600 .env
  echo "Created $APP/deploy/.env (Telegram empty: headless paper mode)."
fi
test -r "$HOME/sxbot/data/token.json" || echo "WARNING: primary bot token.json not found yet"
ENGINE_UID="$(id -u)" ENGINE_GID="$(id -g)" docker compose up -d --build
docker compose ps
