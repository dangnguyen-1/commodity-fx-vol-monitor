#!/bin/bash
# Takes a fresh Ubuntu VM (22.04/24.04) from nothing to "all collectors
# running under supervision" in one pass. Written for a from-scratch
# deployment on infrastructure YOU own and control — see the handoff
# doc for why that matters.
#
# By default this also installs and initialises Postgres ON THIS BOX, so
# one VM owns both the data and the processes that write it. Earlier
# versions assumed DATABASE_URL pointed at somebody else's already-
# populated database, which quietly made that somebody a permanent
# dependency — and left this script unable to stand up a working system
# on its own, since nothing here ever created the collectors' tables.
# Choose "external" at the prompt in step 4 if you really do want a
# managed/remote Postgres; note the free tiers of the usual suspects are
# a poor fit (0.5 GB storage and ~100 CU-hours/month against a 24/7
# writer that is already past 288 MB — see the handoff notes).
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

echo "=== [1/10] System packages ==="
sudo apt-get update -y
# postgresql-client gives us psql regardless of which database mode step 5
# takes — we need it to apply the collectors' schema in step 7 even when
# the server itself lives somewhere else.
sudo apt-get install -y git curl build-essential ufw postgresql-client

echo "=== [2/10] Python + venv ==="
# Use whatever python3 the distro ships rather than pinning a minor
# version. The previous `apt-get install python3.11` could not succeed on
# any Ubuntu this script claims to support — 22.04 ships 3.10, 24.04 ships
# 3.12 and 26.04 ships 3.14, and none of them carry a python3.11 package.
# Getting 3.11 specifically would have meant the deadsnakes PPA, which the
# script never added.
#
# The floor is 3.11: the codebase uses `X | Y` type syntax at runtime in
# places, and eval_type_backport only covers the API's Pydantic models.
# Verified working end to end on 3.14 (Ubuntu 26.04) — every requirement
# installs and imports cleanly there.
sudo apt-get install -y python3 python3-venv python3-pip
PY_OK=$(python3 -c 'import sys; print(1 if sys.version_info >= (3, 11) else 0)')
if [ "$PY_OK" != "1" ]; then
  echo "Need Python 3.11 or newer; this box has $(python3 --version)."
  echo "Install a newer python3 (deadsnakes PPA on older Ubuntu) and re-run."
  exit 1
fi
echo "Using $(python3 --version)"
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
pip install -r requirements-api.txt
pip install -r requirements-dashboard.txt
# The Dash research dashboard's own dependencies (dash, dash-bootstrap-
# components, numpy, yfinance). requirements-dashboard.txt above is the
# *Streamlit* strategy monitor's — two different apps with confusingly
# similar names, see the handoff. Without this line the `dashboard` pm2
# process dies immediately on `import dash`.
pip install -r dashboard/requirements.txt

echo "=== [3/10] Node.js (>= 20) + npm packages ==="
# Prefer the distro's own nodejs when it's new enough — 26.04 ships 22.x,
# which is fine — and only reach for the NodeSource repo when it isn't.
# Adding a third-party apt repo is worth avoiding when the archive already
# has what we need.
if ! command -v node >/dev/null 2>&1 || [ "$(node -v | cut -d. -f1 | tr -d v)" -lt 20 ]; then
  sudo apt-get install -y nodejs npm || true
fi
if ! command -v node >/dev/null 2>&1 || [ "$(node -v | cut -d. -f1 | tr -d v)" -lt 20 ]; then
  curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
  sudo apt-get install -y nodejs
fi
echo "Using node $(node -v)"
npm install
sudo npm install -g pm2

echo "=== [4/10] Environment file ==="
if [ ! -f ".env" ]; then
  cp .env.example .env
  echo "Created .env from .env.example."
else
  echo ".env already exists — leaving it alone."
fi

echo "=== [5/10] Postgres ==="
echo "Where should the collectors' Postgres live?"
echo "  local    — install and run it on this box (recommended: one VM owns"
echo "             the data and the processes writing it, no egress, no"
echo "             per-hour compute quota to exhaust)"
echo "  external — a managed/remote Postgres you'll paste a URL for"
read -p "local or external? [local] " DB_MODE
DB_MODE="${DB_MODE:-local}"

if [ "$DB_MODE" = "local" ]; then
  sudo apt-get install -y postgresql
  # Role and database are created only if absent, so re-running this script
  # on an existing box doesn't blow away a populated database.
  if sudo -u postgres psql -tAc \
      "SELECT 1 FROM pg_roles WHERE rolname='commodities'" | grep -q 1; then
    echo "Role 'commodities' already exists — keeping its existing password."
    echo "If you don't have that password to hand, set a new one with:"
    echo "    sudo -u postgres psql -c \"ALTER ROLE commodities PASSWORD 'new-pw'\""
    echo "then update DATABASE_URL in .env yourself."
    DB_URL=""
  else
    # Hex so the password is URL-safe — it goes straight into a connection
    # string, where a stray '@' or '/' or '#' would silently mis-parse.
    DB_PASS="$(openssl rand -hex 24)"
    sudo -u postgres psql -c \
      "CREATE ROLE commodities LOGIN PASSWORD '${DB_PASS}'"
    DB_URL="postgresql://commodities:${DB_PASS}@localhost:5432/commodities"
  fi

  if ! sudo -u postgres psql -tAc \
      "SELECT 1 FROM pg_database WHERE datname='commodities'" | grep -q 1; then
    sudo -u postgres createdb -O commodities commodities
  fi

  # Postgres listens on localhost only by default on Debian/Ubuntu, and we
  # deliberately leave it that way — step 9's firewall allows SSH alone, so
  # the database is reachable only from this box or through an SSH tunnel.
  if [ -n "$DB_URL" ]; then
    if grep -q '^DATABASE_URL=' .env; then
      sed -i "s#^DATABASE_URL=.*#DATABASE_URL=${DB_URL}#" .env
    else
      echo "DATABASE_URL=${DB_URL}" >> .env
    fi
    echo "Wrote DATABASE_URL for the local database into .env."
    echo "To reach it from your laptop (e.g. to run scripts/migrate_database.sh):"
    echo "    ssh -L 15432:localhost:5432 \$(whoami)@<this-vm-ip>"
  fi
else
  echo "Fine — you'll paste your own DATABASE_URL into .env at the next step."
fi

echo "=== [6/10] Remaining secrets ==="
echo
echo "!!! STOP — before continuing, edit .env with your own credentials: !!!"
echo "    - TV_SESSION_ID / TV_SESSION_SIGN   (your TradingView session — these"
echo "                                          expire, so re-grab them rather"
echo "                                          than reusing an old pair)"
echo "    - COMTRADE_API_KEY_1 through _7      (your UN Comtrade keys)"
echo "    - OPENAI_API_KEY                     (your own OpenAI key)"
if [ "$DB_MODE" != "local" ] || [ -z "$DB_URL" ]; then
  echo "    - DATABASE_URL                       (your own Postgres — not one"
  echo "                                          borrowed from someone else)"
fi
echo
read -p "Press Enter once .env is filled in and saved to continue... " _

echo "=== [7/10] Create the collectors' Postgres tables ==="
# market_data / fundamental_trade_data / news_articles / news_sentiment.
# Nothing else in this repo ever applies this file — it used to be run by
# hand exactly once, on a database that was then treated as a given, which
# is why a from-scratch deployment could never actually work. All of it is
# CREATE TABLE IF NOT EXISTS, so this is safe against a populated database
# (including one just seeded by scripts/migrate_database.sh).
set -a
. ./.env
set +a
if [ -z "$DATABASE_URL" ]; then
  echo "DATABASE_URL is still empty in .env — nothing to apply the schema to."
  exit 1
fi
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f data_collector/database/schema.sql
echo "Collector tables are in place."

echo "=== [8/10] Bootstrap the paper-trading database and verify the spec ==="
mkdir -p paper_trading/data
python3 -m paper_trading.database.init_database
python3 -m paper_trading.database.load_relationships
python3 -m paper_trading.database.populate_live_instrument_registry
python3 -m strategy.config.intraday.load_intraday_spec

echo "=== [9/10] Firewall — SSH only, nothing else exposed ==="
sudo ufw allow OpenSSH
sudo ufw --force enable

echo "=== [10/10] Start everything under pm2 ==="
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
