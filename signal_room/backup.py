"""Gather everything the room can't be rebuilt without into one folder, for `./start.sh backup`.

The code is on GitHub; these three things are not, on purpose, and losing them is losing the room:
  - the database (every call, every record), copied with SQLite's online backup so it is consistent
    even while the room is running and writing;
  - `.env`: the master key (without it sealed calls can never be opened again) and the anchoring
    wallet's key;
  - `agents/state/`: each bot's wallet key, i.e. its identity and track record.

start.sh then locks the folder into a password-encrypted disk image and deletes the unlocked copy.
"""
from __future__ import annotations

import datetime as dt
import os
import shutil
import sqlite3
import sys

from . import config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

RESTORE = """Signal Room backup, made {when}

What is in here:
  signal_room.db   the room's database (every call and record)
  .env             settings, including the master key and the anchoring wallet's key
  agents/state/    each bot's wallet key (its identity and record)

To restore onto a fresh copy of the code (git clone https://github.com/scburja-art/signal-room):
  1. Stop the room (Ctrl+C in its window).
  2. Copy signal_room.db, .env and the agents/state folder into the signal-room folder,
     replacing what is there. (.env starts with a dot, so it is hidden in Finder: press
     Cmd+Shift+. to show hidden files.)
  3. Start the room again (room).

Keep this backup's password somewhere safe, like a password manager. Without it this file
cannot be opened, by you or anyone else.
"""


def database_path() -> str:
    url = config.DATABASE_URL
    if not url.startswith("sqlite:///"):
        raise SystemExit("Backup copies the SQLite database; for Postgres use pg_dump instead.")
    path = url[len("sqlite:///"):]
    return path if os.path.isabs(path) else os.path.normpath(os.path.join(ROOT, path))


def stage(dest: str) -> dict:
    """Copy the three irreplaceable things into dest. Returns what was copied, for the summary."""
    os.makedirs(dest, exist_ok=False)
    db_src = database_path()
    if not os.path.exists(db_src):
        raise SystemExit(f"No database at {db_src}")
    src = sqlite3.connect(db_src)
    dst = sqlite3.connect(os.path.join(dest, "signal_room.db"))
    with dst:
        src.backup(dst)                       # consistent snapshot, safe while the room writes
    signals = dst.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
    dst.close()
    src.close()
    if ok != "ok":
        raise SystemExit(f"The database copy failed its integrity check ({ok}); nothing was saved.")

    env = os.path.join(ROOT, ".env")
    if not os.path.exists(env):
        raise SystemExit("No .env found; there is nothing secret to back up yet.")
    shutil.copy2(env, os.path.join(dest, ".env"))
    state = os.path.join(ROOT, "agents", "state")
    bots = 0
    if os.path.isdir(state):
        shutil.copytree(state, os.path.join(dest, "agents", "state"))
        bots = len([f for f in os.listdir(state) if f.endswith(".json") and "posted" not in f and "reads" not in f])
    with open(os.path.join(dest, "HOW TO RESTORE.txt"), "w") as f:
        f.write(RESTORE.format(when=dt.datetime.now().strftime("%b %d, %Y %I:%M %p")))
    return {"signals": signals, "bot_files": bots}


def main(argv: list[str]) -> None:
    if len(argv) != 1:
        raise SystemExit("usage: python -m signal_room.backup <empty-destination-folder>")
    info = stage(argv[0])
    print(f"Copied the database ({info['signals']:,} calls, integrity ok), settings, and {info['bot_files']} bot keys.")


if __name__ == "__main__":
    main(sys.argv[1:])
