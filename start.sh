#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d venv ]; then
  python3 -m venv venv
  ./venv/bin/pip install -q --upgrade pip
fi

# Install anything new in requirements.txt. Cheap when nothing changed.
if [ ! -f venv/.requirements.sha ] || ! shasum -a 256 -c --status venv/.requirements.sha 2>/dev/null; then
  ./venv/bin/pip install -q -r requirements.txt
  shasum -a 256 requirements.txt > venv/.requirements.sha
fi

if [ ! -f .env ]; then
  cp .env.example .env
fi

if ! grep -q "^SIGNAL_ROOM_MASTER_KEY=.\+" .env; then
  KEY=$(./venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
  if grep -q "^SIGNAL_ROOM_MASTER_KEY=" .env; then
    sed -i.bak "s|^SIGNAL_ROOM_MASTER_KEY=.*|SIGNAL_ROOM_MASTER_KEY=$KEY|" .env && rm -f .env.bak
  else
    echo "SIGNAL_ROOM_MASTER_KEY=$KEY" >> .env
  fi
  echo "Generated master key into .env (keep it; losing it makes stored signals unreadable)."
fi

set -a; source .env; set +a
export PYTHONUNBUFFERED=1
mkdir -p logs

case "${1:-serve}" in
  serve)
    ./venv/bin/python -m signal_room.main 2>&1 | tee -a logs/server.log
    ;;
  agent)
    shift
    ./venv/bin/python -m agents.run "$@" 2>&1 | tee -a "logs/agent-${1:-unknown}.log"
    ;;
  all)
    # Server plus every seed agent in this one terminal. Each still writes its own log in logs/.
    # Ctrl+C stops all of them together. Kakalshi itself runs in its own window (its own program).
    KAKALSHI_LOG="${SIGNAL_ROOM_KAKALSHI_LOG:-$HOME/Stove Codes/kalshi-deep-trading-bot/kalshi-copy-bot/shadow_results.json}"
    PIDS=()
    stop_all() {
      trap - INT TERM EXIT
      echo; echo "Stopping everything..."
      kill "${PIDS[@]}" 2>/dev/null
      pkill -P $$ 2>/dev/null   # the log viewer too
      wait 2>/dev/null
      echo "Stopped."
    }
    trap stop_all INT TERM EXIT
    ./venv/bin/python -m signal_room.main >> logs/server.log 2>&1 &
    PIDS+=($!)
    PORT="${SIGNAL_ROOM_PORT:-8000}"
    for _ in $(seq 1 30); do
      curl -s "http://127.0.0.1:$PORT/health" > /dev/null 2>&1 && break
      sleep 1
    done
    if ! curl -s "http://127.0.0.1:$PORT/health" > /dev/null 2>&1; then
      echo "The server did not start. Last lines of logs/server.log:"; tail -20 logs/server.log; exit 1
    fi
    echo "Signal Room is up (port $PORT). Bots joining..."
    # The viewer starts before the agents so their first lines show. It runs in the background and the
    # script waits on it, so Ctrl+C reaches stop_all at once.
    touch logs/agent-momentum.log logs/agent-meanrev.log logs/agent-forecaster.log logs/agent-forecaster2.log logs/agent-reader.log logs/agent-kakalshi.log
    tail -n 0 -F logs/server.log logs/agent-*.log 2>/dev/null | ./venv/bin/python -u -m signal_room.feed &
    FEED=$!
    for agent in momentum meanrev forecaster forecaster2 reader; do
      ./venv/bin/python -m agents.run "$agent" >> "logs/agent-$agent.log" 2>&1 &
      PIDS+=($!)
    done
    if [ -f "$KAKALSHI_LOG" ]; then
      ./venv/bin/python -m agents.run kakalshi "$KAKALSHI_LOG" >> logs/agent-kakalshi.log 2>&1 &
      PIDS+=($!)
    else
      echo "Kakalshi's paper log isn't at $KAKALSHI_LOG, so Kakalshi's calls won't come in."
    fi
    echo "Live feed below. Ctrl+C stops everything."
    wait "$FEED"
    ;;
  status)
    exec ./venv/bin/python -m signal_room.status
    ;;
  report)
    shift
    exec ./venv/bin/python -m signal_room.report "$@"
    ;;
  backtest)
    shift
    exec ./venv/bin/python -m agents.forecaster_backtest "$@"
    ;;
  verify)
    shift
    exec ./venv/bin/python -m agents.verify "$@"
    ;;
  backup)
    # One password-locked file holding what GitHub deliberately doesn't: the database, .env (master
    # and wallet keys) and the bots' keys. Saved to iCloud Drive if it is set up, else the Desktop.
    if [ "$(uname)" != "Darwin" ]; then
      echo "Backup uses macOS's encrypted disk images; run it on the Mac."; exit 1
    fi
    ICLOUD="$HOME/Library/Mobile Documents/com~apple~CloudDocs"
    if [ -n "${2:-}" ]; then DEST="$2"
    elif [ -d "$ICLOUD" ]; then DEST="$ICLOUD/Signal Room Backups"
    else DEST="$HOME/Desktop/Signal Room Backups"; fi
    mkdir -p "$DEST"
    STAGE="$(mktemp -d)"
    trap 'rm -rf "$STAGE"' EXIT
    ./venv/bin/python -m signal_room.backup "$STAGE/Signal Room Backup"
    OUT="$DEST/signal-room-backup-$(date +%Y-%m-%d-%H%M).dmg"
    echo
    echo "Now choose a password for this backup. You'll need it to restore, so save it in your"
    echo "password manager. macOS asks twice; nothing shows while you type."
    hdiutil create -quiet -encryption AES-256 -volname "Signal Room Backup" \
      -srcfolder "$STAGE/Signal Room Backup" -format UDZO "$OUT"
    rm -rf "$STAGE"
    echo
    echo "Backup saved, locked with your password:"
    echo "  $OUT"
    case "$DEST" in
      "$ICLOUD"*) echo "It's in iCloud Drive, so it's off this laptop once iCloud syncs." ;;
      *) echo "It's on this laptop only. Drag it into iCloud Drive or Google Drive to get it off the laptop." ;;
    esac
    ;;
  test)
    exec ./venv/bin/python -m pytest -q tests
    ;;
  *)
    echo "usage: ./start.sh [all | serve | agent <momentum|meanrev|forecaster|forecaster2|reader|kakalshi> [args] | status | report [YYYY-MM-DD | week] | backtest [v1|v2] | verify <YYYY-MM-DD> | backup | test]"
    exit 1
    ;;
esac
