"""Backup gathers a consistent database copy, the settings file and the bots' keys, and nothing is lost."""
import json
import os
import sqlite3
import threading

import pytest

from signal_room import backup, config


def make_room(root):
    os.makedirs(root / "agents" / "state")
    db = sqlite3.connect(root / "signal_room.db")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE signals (id INTEGER PRIMARY KEY, x TEXT)")
    db.executemany("INSERT INTO signals (x) VALUES (?)", [("a",)] * 500)
    db.commit()
    (root / ".env").write_text("SIGNAL_ROOM_MASTER_KEY=test\nANCHOR_PRIVATE_KEY=0xabc\n")
    for name in ("momentum.json", "meanrev.json", "kakalshi.json.posted.json"):
        (root / "agents" / "state" / name).write_text(json.dumps({"k": name}))
    return db


def test_stage_copies_everything_consistently_while_the_room_writes(tmp_path, monkeypatch):
    root = tmp_path / "room"
    db = make_room(root)
    monkeypatch.setattr(backup, "ROOT", str(root))
    monkeypatch.setattr(config, "DATABASE_URL", "sqlite:///./signal_room.db")

    stop = threading.Event()
    def writer():                                   # the room keeps writing during the backup
        w = sqlite3.connect(root / "signal_room.db", check_same_thread=False)
        while not stop.is_set():
            w.execute("INSERT INTO signals (x) VALUES ('b')")
            w.commit()
        w.close()
    t = threading.Thread(target=writer)
    t.start()
    try:
        info = backup.stage(str(tmp_path / "out"))
    finally:
        stop.set()
        t.join()

    out = tmp_path / "out"
    copy = sqlite3.connect(out / "signal_room.db")
    assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert copy.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == info["signals"] >= 500
    assert (out / ".env").read_text() == (root / ".env").read_text()
    assert sorted(os.listdir(out / "agents" / "state")) == sorted(os.listdir(root / "agents" / "state"))
    assert info["bot_files"] == 2                      # momentum + meanrev, not the posted-ids file
    assert "To restore" in (out / "HOW TO RESTORE.txt").read_text()
    db.close()


def test_refuses_to_overwrite_and_needs_a_database(tmp_path, monkeypatch):
    root = tmp_path / "room"
    make_room(root).close()
    monkeypatch.setattr(backup, "ROOT", str(root))
    monkeypatch.setattr(config, "DATABASE_URL", "sqlite:///./signal_room.db")
    (tmp_path / "out").mkdir()
    with pytest.raises(FileExistsError):
        backup.stage(str(tmp_path / "out"))
    monkeypatch.setattr(config, "DATABASE_URL", "sqlite:///./missing.db")
    with pytest.raises(SystemExit):
        backup.stage(str(tmp_path / "out2"))
    monkeypatch.setattr(config, "DATABASE_URL", "postgresql://x")
    with pytest.raises(SystemExit):
        backup.stage(str(tmp_path / "out3"))
