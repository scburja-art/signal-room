"""The live feed: plain sentences, routine noise hidden, each bot named and colored, results shown."""
import json

from signal_room import feed

T = "2026-10-31 18:00:01,000"


def ev(**e):
    return f"{T} INFO signal_room.service: EVENT {json.dumps(e)}"


def test_noise_is_hidden():
    for line in (
        "2026-10-31 17:05:27,418 INFO agents.momentum: XRP: open signal still pending",
        'INFO:     127.0.0.1:61344 - "POST /signals HTTP/1.1" 409 Conflict',
        'INFO:     127.0.0.1:61078 - "POST /signals HTTP/1.1" 201 Created',
        "2026-10-31 17:03:53,843 INFO signal_room.scheduler: tick {'prices': 4, 'resolved': 0}",
        "2026-10-31 17:03:53,843 INFO signal_room.scheduler: tick {'prices': 4, 'resolved': 3}",
        "2026-10-31 17:04:56,608 INFO agents.momentum: BTC short @77409.02 -> d516195904b",   # the server reports it
        "2026-10-31 18:31:47,066 INFO agents.kakalshi: bet 43@2026-10-29T22:31:40+00:00 [crypto-first-minute] -> XRP 15m long conf=0.58 id=a",
        "2026-10-31 17:04:57,144 INFO agents.reader: read SOL 15m short by kakalshi-crypto-first-minute (luck, cost 1.0)",
        "==> logs/server.log <==",
        "",
    ):
        assert feed.render(line, color=False) is None, line


def test_a_new_call_is_shown_sealed():
    out = feed.render(ev(kind="posted", agent="momentum-control", pair="BTC", horizon="1h", closes="2026-10-31T22:59:00"), color=False)
    assert out.startswith(" 6:00 PM  Momentum      NEW CALL   BTC · 1 hour · sealed until ")
    assert "long" not in out and "short" not in out and "UP" not in out


def test_a_scored_call_reads_as_a_sentence():
    line = ev(kind="scored", agent="kakalshi-crypto-first-minute", pair="BTC", horizon="15m", direction="long",
              confidence=0.75, start=77400.0, end=77500.0, result="right", credit=0.75)
    assert feed.render(line, color=False) == (
        " 6:00 PM  Kakalshi CFM  ✔ RIGHT    BTC · 15 min · said UP, 75% sure · $77,400 → $77,500 · +0.75 credits")
    colored = feed.render(line, color=True)
    assert feed.FAMILY_COLORS["kakalshi"] in colored and feed.GREEN in colored
    wrong = ev(kind="scored", agent="meanrev-control", pair="XRP", horizon="1h", direction="short",
               confidence=0.55, start=1.2506, end=1.2601, result="wrong", credit=-1.21)
    assert feed.render(wrong, color=False) == (
        " 6:00 PM  Mean-rev      ✘ WRONG    XRP · 1 hour · said DOWN, 55% sure · $1.2506 → $1.2601 · -1.21 credits")
    assert feed.RED in feed.render(wrong, color=True)
    flat = ev(kind="scored", agent="momentum-control", pair="ETH", horizon="1h", direction="long",
              confidence=0.55, start=2250.0, end=2250.5, result="flat", credit=0.0)
    assert "– FLAT" in feed.render(flat, color=False) and "not scored" in feed.render(flat, color=False)


def test_reads_and_standing_changes():
    read = ev(kind="read", agent="reader-control", author="kakalshi-crypto-first-minute", pair="SOL", horizon="15m",
              verdict="luck", cost=1.0, left=8.0)
    assert feed.render(read, color=False) == (
        " 6:00 PM  Reader        READ       paid 1 credit for Kakalshi CFM's SOL · 15 min call (Luck) · 8 left")
    assert "DELISTED" in feed.render(ev(kind="delisted", agent="momentum-control"), color=False)
    assert "BACK" in feed.render(ev(kind="reinstated", agent="momentum-control"), color=False)


def test_problems_and_sleep_in_plain_words():
    warn = feed.render("2026-10-31 16:55:08,490 WARNING agents.meanrev: BTC: candle fetch failed: 403 Forbidden", color=True)
    assert feed.YELLOW in warn and "Mean-rev" in warn and "couldn't get BTC's last hour from Coinbase" in warn
    sleep = feed.render("2026-10-31 07:02:01,000 WARNING signal_room.scheduler: gap of 1260s since last tick (sleep?); due signals will backfill", color=False)
    assert "asleep or off for about 21 min" in sleep
    long_sleep = feed.render("2026-10-31 06:42:01,000 WARNING signal_room.scheduler: gap of 25260s since last tick (sleep?)", color=False)
    assert "about 7 hours" in long_sleep
    assert feed.money(2249.86) == "$2,249.86" and feed.money(77409.02) == "$77,409" and feed.money(1.2506) == "$1.2506"
    assert feed.RED in feed.render("Traceback (most recent call last):", color=True)
    assert feed.render("2026-10-31 17:04:56,608 INFO agents.momentum: momentum control running as 3d208ca5", color=False).endswith("connected to the room")


def test_unknown_names_are_kept_readable():
    assert feed.display("a-very-long-agent-name-from-a-stranger") == "a-very-long-a…"
    assert feed.color_of("stranger-bot") == feed.WHITE


def test_a_real_resolution_and_post_produce_feed_lines(client, session_factory, caplog):
    import logging
    from tests.api_helpers import Agent
    from tests.test_delisting import post_and_resolve
    a = Agent(client, "kakalshi-crypto-first-minute")
    with caplog.at_level(logging.INFO, logger="signal_room.service"):
        post_and_resolve(client, session_factory, a, 1, right=True, confidence=0.75)
    lines = [feed.render(f"{T} INFO signal_room.service: {r.getMessage()}", color=False)
             for r in caplog.records if r.getMessage().startswith("EVENT ")]
    assert lines[0].startswith(" 6:00 PM  Kakalshi CFM  NEW CALL   BTC · 15 min · sealed until")
    assert lines[1] == " 6:00 PM  Kakalshi CFM  ✔ RIGHT    BTC · 15 min · said UP, 75% sure · $100.00 → $101.00 · +0.75 credits"


def test_scoreboard_draws_from_the_database(client, session_factory, monkeypatch):
    from signal_room import db as dbmod
    from tests.api_helpers import Agent
    from tests.test_delisting import post_and_resolve
    monkeypatch.setattr(dbmod, "SessionLocal", session_factory)
    a = Agent(client, "momentum-control")
    post_and_resolve(client, session_factory, a, 3, right=True, confidence=0.6)
    lines = feed.scoreboard(color=False)
    header = next(line for line in lines if "Verdict" in line)
    row = next(line for line in lines if "Momentum" in line)
    assert header.split() == ["Bot", "Verdict", "Right", "vs.", "coin", "flip", "Calls", "Next", "step"]
    # 3 of 3 right at 60%: skill = 1 - 0.16/0.25 = +0.36 → 36 points better; unproven, so no next step.
    assert row.split() == ["Momentum", "Unproven", "100%", "▲", "36", "better", "3", "—"]
    assert header.index("Calls") + len("Calls") == row.index("3 ") + 1      # numbers right-align under their header
    assert any("Ladder: Unproven → Luck → Probably not luck → Proven" in line for line in lines)


def test_skill_reads_as_a_direction():
    assert feed.versus_coin_flip(6) == ("▲ 6 better", feed.GREEN)
    assert feed.versus_coin_flip(-4) == ("▼ 4 worse", feed.RED)
    assert feed.versus_coin_flip(1)[0] == feed.versus_coin_flip(-1)[0] == feed.versus_coin_flip(0)[0] == "≈ even"
    assert feed.days_text(3.4) == "~3 days" and feed.days_text(0.6) == "~1 day" and feed.days_text(None) == "—"
