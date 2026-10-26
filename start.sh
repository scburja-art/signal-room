#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d venv ]; then
  python3 -m venv venv
  ./venv/bin/pip install -q --upgrade pip
  ./venv/bin/pip install -q -r requirements.txt
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

case "${1:-serve}" in
  serve)
    exec ./venv/bin/python -m signal_room.main
    ;;
  agent)
    shift
    exec ./venv/bin/python -m agents.run "$@"
    ;;
  test)
    exec ./venv/bin/python -m pytest -q tests
    ;;
  *)
    echo "usage: ./start.sh [serve | agent <momentum|kakalshi> [args] | test]"
    exit 1
    ;;
esac
