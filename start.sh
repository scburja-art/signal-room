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
    touch logs/agent-momentum.log logs/agent-meanrev.log logs/agent-reader.log logs/agent-kakalshi.log
    tail -n 0 -F logs/server.log logs/agent-*.log 2>/dev/null | ./venv/bin/python -u -m signal_room.feed &
    FEED=$!
    for agent in momentum meanrev reader; do
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
  test)
    exec ./venv/bin/python -m pytest -q tests
    ;;
  *)
    echo "usage: ./start.sh [all | serve | agent <momentum|meanrev|reader|kakalshi> [args] | status | test]"
    exit 1
    ;;
esac
