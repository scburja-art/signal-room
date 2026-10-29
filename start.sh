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
  test)
    exec ./venv/bin/python -m pytest -q tests
    ;;
  *)
    echo "usage: ./start.sh [serve | agent <momentum|meanrev|kakalshi> [args] | test]"
    exit 1
    ;;
esac
