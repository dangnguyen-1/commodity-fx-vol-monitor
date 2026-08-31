#!/bin/bash
# Pulls the VM's nightly database dumps down to this machine.
#
# The dumps on the VM live on the same disk as the database they came from,
# so they cover a bad migration or an accidental drop but not losing the box
# itself. This is the off-box copy. It is deliberately the cheap version:
# whatever this machine has is at worst a week old, which beats losing six
# weeks of 1-minute history that TradingView will not serve again.
#
# Runs from a launchd agent (see scripts/launchd/README or the install block
# below), but it is safe to run by hand any time.
#
# Every run appends to the log with a timestamp and outcome, because the one
# thing worse than no backup is believing you have one. `--status` prints when
# the last successful pull happened without transferring anything.
#
# INSTALLING THE SCHEDULE (macOS)
# -------------------------------
#   ./scripts/pull_backups.sh --install
#
# That copies this script to ~/Library/Application Support/commodities/ and
# points a launchd agent at the copy. The copy is not an accident: macOS TCC
# refuses to let launchd execute anything inside ~/Desktop, ~/Documents or
# ~/Downloads without Full Disk Access, and it refuses *silently* — the job
# exits 126 with "Operation not permitted" into a log nobody reads, while
# running perfectly when you try it by hand. That is exactly how the previous
# owner's launchd agent stopped working without anyone noticing. Re-run
# --install after editing this file to refresh the installed copy.
set -u

VM_HOST="${BACKUP_VM_HOST:-commodities@178.105.118.123}"
REMOTE_DIR="${BACKUP_REMOTE_DIR:-~/commodities/backups/}"
LOCAL_DIR="${BACKUP_LOCAL_DIR:-$HOME/commodities-backups}"
LOG_FILE="$LOCAL_DIR/pull.log"
STAMP_FILE="$LOCAL_DIR/.last_success"
KEEP=${BACKUP_KEEP_LOCAL:-8}

mkdir -p "$LOCAL_DIR"

INSTALL_DIR="$HOME/Library/Application Support/commodities"
INSTALLED="$INSTALL_DIR/pull_backups.sh"
PLIST="$HOME/Library/LaunchAgents/com.commodities.pull-backups.plist"

if [ "${1:-}" = "--install" ]; then
  mkdir -p "$INSTALL_DIR"
  cp "$0" "$INSTALLED"
  chmod +x "$INSTALLED"
  cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.commodities.pull-backups</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$INSTALLED</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key><integer>12</integer>
        <key>Minute</key><integer>0</integer>
    </dict>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>$LOCAL_DIR/launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>$LOCAL_DIR/launchd.err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
</dict>
</plist>
PLIST_EOF
  launchctl unload "$PLIST" 2>/dev/null
  launchctl load "$PLIST"
  echo "installed to: $INSTALLED"
  echo "agent loaded:  $PLIST"
  echo
  echo "Verify it actually runs under launchd, not just from your shell:"
  echo "    launchctl kickstart -k gui/\$(id -u)/com.commodities.pull-backups"
  echo "    ./scripts/pull_backups.sh --status"
  exit 0
fi

if [ "${1:-}" = "--status" ]; then
  if [ -f "$STAMP_FILE" ]; then
    last=$(cat "$STAMP_FILE")
    age_days=$(( ( $(date +%s) - $(date -j -f "%Y-%m-%dT%H:%M:%SZ" "$last" +%s 2>/dev/null || echo 0) ) / 86400 ))
    echo "last successful pull: $last (${age_days}d ago)"
  else
    echo "last successful pull: NEVER"
  fi
  echo "local copies: $(ls -1 "$LOCAL_DIR"/commodities_*.dump 2>/dev/null | wc -l | tr -d ' ')"
  echo "log: $LOG_FILE"
  exit 0
fi

now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { echo "[$(now)] $*" >> "$LOG_FILE"; echo "$*"; }

log "pulling from $VM_HOST:$REMOTE_DIR"

# BatchMode so a launchd run can never hang on a prompt with nobody watching.
if rsync -az --timeout=120 \
     -e "ssh -o BatchMode=yes -o ConnectTimeout=20" \
     "$VM_HOST:$REMOTE_DIR" "$LOCAL_DIR/" >> "$LOG_FILE" 2>&1; then
  count=$(ls -1 "$LOCAL_DIR"/commodities_*.dump 2>/dev/null | wc -l | tr -d ' ')
  newest=$(ls -t "$LOCAL_DIR"/commodities_*.dump 2>/dev/null | head -1)

  # A dump pg_restore cannot read its own table of contents from is not a
  # backup. Check before claiming success.
  if [ -n "$newest" ] && command -v pg_restore >/dev/null 2>&1; then
    if ! pg_restore -l "$newest" > /dev/null 2>&1; then
      log "FAILED: newest dump $(basename "$newest") is not a readable archive"
      exit 1
    fi
  fi

  # Keep the most recent few; the VM already rotates at 14 days.
  ls -t "$LOCAL_DIR"/commodities_*.dump 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do
    rm -f "$old"
    log "pruned $(basename "$old")"
  done

  now > "$STAMP_FILE"
  log "OK: $count local copies, newest $(basename "${newest:-none}")"
  exit 0
fi

log "FAILED: rsync could not reach $VM_HOST (is the VM up, is your SSH key loaded?)"
exit 1
