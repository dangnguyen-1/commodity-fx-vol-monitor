#!/bin/bash
# Takes a fresh Ubuntu VM (22.04/24.04) from nothing to "all collectors
# running under supervision" in one pass. Written for a from-scratch
# deployment on infrastructure YOU own and control — see the handoff
# doc for why that matters.
#
# What this does NOT do, on purpose:
#   - Fill in .env. Secrets are yours; this script stops and tells you
#     exactly what to edit before continuing.
#   - Run `pm2 startup`'s printed sudo command for you. That needs an
#     interactive sudo prompt this script can't answer non-interactively
#     — it prints the exact command for you to run yourself at the end.
#   - Open any port beyond SSH. Dashboards/API stay bound to localhost;
#     access them via an SSH tunnel (instructions printed at the end),
#     not by exposing them to the public internet. Do that later,
#     deliberately, with real TLS — not as a byproduct of this script.
#
# Usage: run this FROM INSIDE a fresh clone of the repo, as the user
# that will own the running processes (not root):
#   git clone <repo-url> commodities && cd commodities
#   ./scripts/setup_cloud_vm.sh

set -e

if [ "$(id -u)" -eq 0 ]; then
  echo "Don't run this as root — run it as the regular user that will own the processes."
  echo "(sudo is used internally only for apt/system package installs.)"
  exit 1
fi

if [ ! -f "ecosystem.config.js" ]; then
  echo "Run this from inside the repo root (ecosystem.config.js not found here)."
  exit 1
fi

REPO_ROOT="$(pwd)"

echo "=== [1/8] System packages ==="
sudo apt-get update -y
sudo apt-get install -y git curl build-essential ufw

echo "=== [2/8] Python 3.11 + venv ==="
# This codebase was originally developed and tested against Python 3.9,
# with several fixes made this project for genuine 3.9-only syntax
# incompatibilities (all of them additive/backwards-compatible, e.g.
# `from __future__ import annotations`). 3.11 is a safe, well-supported
# modern default; if anything unexpected breaks, installing 3.9
# specifically (deadsnakes PPA) and rebuilding the venv against it is
# the fallback.
sudo apt-get install -y python3.11 python3.11-venv python3-pip
if [ ! -d ".venv" ]; then
  python3.11 -m venv .venv
fi
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
pip install -r requirements-api.txt
pip install -r requirements-dashboard.txt

echo "=== [3/8] Node.js 20 + npm packages ==="
if ! command -v node >/dev/null 2>&1 || [ "$(node -v | cut -d. -f1 | tr -d v)" -lt 20 ]; then
  curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
  sudo apt-get install -y nodejs
fi
npm install
sudo npm install -g pm2

echo "=== [4/8] Environment secrets ==="
if [ ! -f ".env" ]; then
  cp .env.example .env
fi
echo
echo "!!! STOP — before continuing, edit .env with your own credentials: !!!"
echo "    - TV_SESSION_ID / TV_SESSION_SIGN   (your TradingView session)"
echo "    - COMTRADE_API_KEY_1 through _7      (your UN Comtrade keys)"
echo "    - DATABASE_URL                       (the shared upstream Postgres —"
echo "                                           confirm this with whoever set it up,"
echo "                                           don't guess it)"
echo "    - OPENAI_API_KEY                     (your own OpenAI key)"
echo
read -p "Press Enter once .env is filled in and saved to continue... " _

echo "=== [5/8] Bootstrap the local paper-trading database ==="
mkdir -p paper_trading/data
python3 -m paper_trading.database.init_database
python3 -m paper_trading.database.load_relationships
python3 -m paper_trading.database.populate_live_instrument_registry

echo "=== [6/8] Verify the intraday spec loads cleanly ==="
python3 -m strategy.config.intraday.load_intraday_spec

echo "=== [7/8] Firewall — SSH only, nothing else exposed ==="
sudo ufw allow OpenSSH
sudo ufw --force enable

echo "=== [8/8] Start everything under pm2 ==="
pm2 start ecosystem.config.js
pm2 save

echo
echo "=================================================================="
echo "Done. Check status with: pm2 status"
echo
echo "To survive a reboot, run the command pm2 prints from:"
echo "    pm2 startup"
echo "(it needs an interactive sudo prompt, so run it yourself, then"
echo "run 'pm2 save' one more time after)."
echo
echo "Dashboards are bound to localhost only, on purpose. To view them"
echo "from your own machine, tunnel over SSH rather than opening ports:"
echo "    ssh -L 8050:localhost:8050 -L 8000:localhost:8000 -L 8501:localhost:8501 <user>@<vm-ip>"
echo "then open http://localhost:8050 (research dashboard), :8000/docs"
echo "(API), :8501 (strategy monitor) in your own browser."
echo "=================================================================="
