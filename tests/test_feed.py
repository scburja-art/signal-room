"""The live feed: plain sentences, routine noise hidden, each bot named and colored, results shown."""
import datetime as dt
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
    # The post itself is silent; the call is shown once its start price is set. Without the sealed
    # database at hand (another machine), the line shows the start and nothing about the direction.
    assert feed.render(ev(kind="posted", agent="momentum-control", pair="BTC", horizon="1h", closes="2026-10-31T22:59:00"), color=False) is None
    out = feed.render(ev(kind="started", agent="momentum-control", pair="BTC", horizon="1h", signal_id="nope",
                         start=77400.0, closes="2026-10-31T22:59:00"), color=False)
    assert out.startswith(" 6:00 PM  Momentum      NEW CALL   BTC · 1h · from $77,400 · sealed until ")
    assert "long" not in out and "short" not in out and "UP" not in out


def test_a_scored_call_reads_as_a_sentence():
    line = ev(kind="scored", agent="kakalshi-crypto-first-minute", pair="BTC", horizon="15m", direction="long",
              confidence=0.75, start=77400.0, end=77500.0, result="right", credit=0.75)
    assert feed.render(line, color=False) == (
        " 6:00 PM  Kakalshi CFM  ✔ RIGHT    BTC · 15m · UP 75% · $77,400→$77,500 · +0.75 cr")
    colored = feed.render(line, color=True)
    assert feed.FAMILY_COLORS["kakalshi"] in colored and feed.GREEN in colored
    wrong = ev(kind="scored", agent="meanrev-control", pair="XRP", horizon="1h", direction="short",
               confidence=0.55, start=1.2506, end=1.2601, result="wrong", credit=-1.21)
    assert feed.render(wrong, color=False) == (
        " 6:00 PM  Mean-rev      ✘ WRONG    XRP · 1h · DOWN 55% · $1.2506→$1.2601 · -1.21 cr")
    assert feed.RED in feed.render(wrong, color=True)
    # A scored line arrives long after the call; the window says which call it was.
    windowed = ev(kind="scored", agent="kakalshi-crypto-first-minute", pair="SOL", horizon="15m", direction="short",
                  confidence=0.79, start=89.535, end=89.59, result="wrong", credit=-1.5,
                  opened="2026-11-05T21:03:00", closed="2026-11-05T21:18:00")
    out = feed.render(windowed, color=False)
    assert "SOL · 15m · DOWN 79% · " in out and out.endswith(" · $89.53→$89.59 · -1.50 cr")
    assert "→" in out.split("79% · ")[1].split(" · ")[0]          # the window, e.g. 4:03→4:18
    # The widest case (ETH with cents) fits a 101-column laptop terminal.
    widest = ev(kind="scored", agent="kakalshi-crypto-first-minute", pair="ETH", horizon="15m", direction="DOWN",
                confidence=0.56, start=2225.11, end=2237.83, result="wrong", credit=-0.24,
                opened="2026-11-05T22:02:00", closed="2026-11-05T22:17:00")
    assert len(feed.render(widest, color=False)) <= 100
    flat = ev(kind="scored", agent="momentum-control", pair="ETH", horizon="1h", direction="long",
              confidence=0.55, start=2250.0, end=2250.5, result="flat", credit=0.0)
    assert "– FLAT" in feed.render(flat, color=False) and "not scored" in feed.render(flat, color=False)


def test_reads_and_standing_changes():
    read = ev(kind="read", agent="reader-control", author="kakalshi-crypto-first-minute", pair="SOL", horizon="15m",
              verdict="luck", cost=1.0, left=8.0)
    assert feed.render(read, color=False) == (
        " 6:00 PM  Reader        READ       paid 1 credit for Kakalshi CFM's SOL · 15m call (Luck) · 8 left")
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


def test_a_real_resolution_and_post_produce_feed_lines(client, session_factory, caplog, monkeypatch):
    import logging
    from signal_room import db as dbmod
    from tests.api_helpers import Agent
    from tests.test_delisting import post_and_resolve
    monkeypatch.setattr(dbmod, "SessionLocal", session_factory)   # the feed reads the same database
    a = Agent(client, "kakalshi-crypto-first-minute")
    with caplog.at_level(logging.INFO, logger="signal_room.service"):
        post_and_resolve(client, session_factory, a, 1, right=True, confidence=0.75)
    lines = [feed.render(f"{T} INFO signal_room.service: {r.getMessage()}", color=False)
             for r in caplog.records if r.getMessage().startswith("EVENT ")]
    lines = [x for x in lines if x]   # the bare post is silent
    # On the operator's own machine the feed unseals the direction and says what wins: 100 ± 0.05%.
    assert lines[0].startswith(" 6:00 PM  Kakalshi CFM  NEW CALL   BTC · 15m · UP from $100.00 · wins above $100.05 by ")
    assert "100.05" not in "".join(r.getMessage() for r in caplog.records)   # the log carries no direction
    assert not any('"direction"' in r.getMessage() and '"started"' in r.getMessage() for r in caplog.records)
    assert lines[1].startswith(" 6:00 PM  Kakalshi CFM  ✔ RIGHT    BTC · 15m · UP 75% · ")
    assert lines[1].endswith(" · $100.00→$101.00 · +0.75 cr")


def test_scoreboard_draws_from_the_database(client, session_factory, monkeypatch, yardsticks):
    from signal_room import db as dbmod
    from tests.api_helpers import Agent
    from tests.test_delisting import post_and_resolve
    monkeypatch.setattr(dbmod, "SessionLocal", session_factory)
    a = Agent(client, "momentum-control")
    yardsticks("momentum.json", a)
    post_and_resolve(client, session_factory, a, 3, right=True, confidence=0.6)
    from signal_room.db import Signal
    db = session_factory()
    last = max(s.resolved_at for s in db.query(Signal).all())
    db.close()
    # The scoreboard reads the clock itself; pin it to a minute after the last call was scored, the same day.
    monkeypatch.setattr(feed, "_utcnow", lambda: (last + dt.timedelta(minutes=1)).replace(tzinfo=dt.timezone.utc))
    monkeypatch.setattr(feed, "day_start", lambda now=None: last - dt.timedelta(hours=1))
    lines = feed.scoreboard(color=False)
    header = next(line for line in lines if "Verdict" in line)
    row = next(line for line in lines if "Momentum" in line)
    assert header.split() == ["Bot", "Verdict", "Right", "vs.", "its", "bar", "Calls", "Today", "Next", "step"]
    # 3 of 3 right at 60%: skill = 1 - 0.16/0.25 = +0.36 → 36 points better. Momentum is one of the room's
    # yardsticks: it shows as a control, not a verdict, and has no next step.
    assert row.split() == ["Momentum", "Control", "100%", "▲", "36", "better", "3", "3-0", "—"]
    assert header.index("Calls") + len("Calls") == row.index("3 ") + 1      # numbers right-align under their header
    assert any("Ladder: Unproven → Luck → Probably not luck → Proven" in line for line in lines)


def test_skill_reads_as_a_direction():
    assert feed.versus_coin_flip(6) == ("▲ 6 better", feed.GREEN)
    assert feed.versus_coin_flip(-4) == ("▼ 4 worse", feed.RED)
    assert feed.versus_coin_flip(1)[0] == feed.versus_coin_flip(-1)[0] == feed.versus_coin_flip(0)[0] == "≈ even"
    assert feed.days_text(3.4) == "~3 days" and feed.days_text(0.6) == "~1 day" and feed.days_text(None) == "—"
    assert feed.days_text(1096) == "over a year" and feed.days_text(365) == "~365 days"


def test_anchor_lines_in_plain_words():
    ok = feed.render("2026-11-04 09:50:23,597 INFO signal_room.anchor: anchored 2026-11-03 root d66ff0f89707… in 0x3520d47162353a54d44c5306de3fbe40934a42000c6ba8d785aa2809ee1a2c35", color=False)
    assert ok == " 9:50 AM  Room          ⛓ ANCHORED Nov 3's fingerprint of every call is now on the public chain (tx 0x3520d471…)"
    bad = feed.render("2026-11-04 09:51:23,000 WARNING signal_room.scheduler: anchor failed for 2026-10-26: rpc down", color=False)
    assert "couldn't write 2026-10-26's fingerprint yet" in bad and "retrying" in bad


def test_each_forecaster_keeps_its_own_name():
    v1 = feed.render("2026-11-04 22:31:35,821 INFO agents.forecaster: forecaster v1 running as 927b", color=False)
    v2 = feed.render("2026-11-04 22:31:35,841 INFO agents.forecaster2: forecaster v2 running as cc23", color=False)
    assert "Forecaster v1" in v1 and "Forecaster v2" not in v1
    assert "Forecaster v2" in v2 and "Forecaster v1" not in v2


def test_free_reads_of_closed_calls_are_not_paid_reads(client, session_factory, monkeypatch):
    from signal_room import db as dbmod
    from tests.api_helpers import Agent
    from tests.test_delisting import post_and_resolve
    from signal_room.db import Signal
    monkeypatch.setattr(dbmod, "SessionLocal", session_factory)
    author = Agent(client, "momentum-control")
    post_and_resolve(client, session_factory, author, 1, right=True)
    sig_id = session_factory().query(Signal).first().id
    verifier = Agent(client, "verifier")
    assert verifier.get(f"/room/{sig_id}").status_code == 200      # closed call: free
    assert not any("verifier" in line for line in feed.scoreboard(color=False))


# ---------- priced calls ----------

def priced(fill, **e):
    return ev(kind="priced", agent="kakalshi-crypto-first-minute-priced", pair="XRP", horizon="15m",
              market="KXXRP15M-26NOV111345-45", signal_id="s1", fill=fill, closes="2026-10-31T22:45:00", **e)


def test_a_priced_call_shows_its_side_and_cost_only_from_the_local_database(monkeypatch):
    # On a machine without the sealed database the line says a call exists and nothing more.
    away = feed.render(priced("decided"), color=False)
    assert away.startswith(" 6:00 PM  CFM priced    NEW CALL   XRP · 15m · priced call on Kalshi · sealed until ")
    assert "NO" not in away.split("NEW CALL")[1] and "¢" not in away
    # On the operator's machine the side and the price come from the database, never from the log line.
    monkeypatch.setattr(feed, "sealed_priced", lambda sid: {"side": "no", "limit": 0.85, "status": "filled", "ask": 0.85, "bar": 0.86, "reason": ""})
    here = feed.render(priced("decided"), color=False)
    assert " NEW CALL   XRP · 15m · DOWN · NO · 85¢, 86¢ w/ fee · ends " in here
    # With the market's target price, the line says what the coin has to finish under (NO) or over (YES).
    there = feed.render(priced("decided", target=2012.1), color=False)
    assert " NEW CALL   XRP · 15m · DOWN/NO under $2,012.10 · 85¢, 86¢ w/ fee · ends " in there
    widest = feed.render(ev(kind="priced", agent="kakalshi-crypto-first-minute-priced", pair="BTC", horizon="15m",
                            market="KXBTC15M-26NOV111345-45", signal_id="s1", fill="decided",
                            closes="2026-10-31T22:45:00", target=104402.91), color=False)
    assert "DOWN/NO under $104,403 · " in widest and len(widest) <= 100, (len(widest), widest)
    assert "under" not in feed.render(priced("decided", target=None), color=False)


def test_not_filled_and_waiting_say_why(monkeypatch):
    monkeypatch.setattr(feed, "sealed_priced", lambda sid: {"side": "yes", "limit": 0.3, "status": "unfilled", "ask": 0.335, "bar": None,
                                                            "reason": "price above the limit"})
    out = feed.render(priced("decided"), color=False)    # the log line never says which way the fill went
    assert " NOT FILLED XRP · 15m · UP · YES · price 33.5¢, limit 30¢ · not scored" in out
    assert " NOT FILLED XRP · 15m · UP/YES over $1.1494 · price 33.5¢, limit 30¢ · not scored" in feed.render(priced("decided", target=1.1494), color=False)
    monkeypatch.setattr(feed, "sealed_priced", lambda sid: {"side": "no", "limit": 0.9, "status": "unfilled", "ask": 0.85, "bar": None,
                                                            "reason": "under 10 on offer"})
    assert "DOWN · NO · under 10 on offer · not scored" in feed.render(priced("decided"), color=False)
    assert " WAITING    XRP · 15m · Kalshi unreadable; fills at that minute's worst price" in feed.render(priced("waiting"), color=False)


def test_a_scored_priced_call_reads_as_a_sentence():
    base = dict(kind="scored", agent="kakalshi-crypto-first-minute-priced", pair="XRP", horizon="15m", priced=True,
                market="KXXRP15M-26NOV111345-45", side="no")
    won = feed.render(ev(**base, result="right", confidence=0.9, bar=0.86, venue_result="no", credit=0.0384,
                         opened="2026-10-31T22:31:30", closed="2026-10-31T22:45:00"), color=False)
    assert " ✔ RIGHT    XRP · 15m · DOWN NO 90% · " in won and won.endswith(" · paid 86¢ · +0.04 cr")
    lost = feed.render(ev(**base, result="wrong", confidence=0.9, bar=0.86, venue_result="yes", credit=-0.2816,
                          opened="2026-10-31T22:31:30", closed="2026-10-31T22:45:00"), color=False)
    assert " ✘ WRONG " in lost and lost.endswith("-0.28 cr")
    assert feed.render(ev(**base, result="unfilled", reason="price above the limit"), color=False) is None   # said once already
    assert "no price or result could be found" in feed.render(ev(**base, result="void"), color=False)


def test_priced_lines_fit_a_101_column_window(monkeypatch):
    lines = []
    for status in ("filled", "unfilled"):
        widest = {"side": "yes", "limit": 0.955, "status": status, "ask": 0.945, "bar": 0.955, "reason": "price above the limit"}
        monkeypatch.setattr(feed, "sealed_priced", lambda sid, w=widest: w)
        lines.append(feed.render(priced("decided"), color=False))
    lines.append(feed.render(priced("waiting"), color=False))
    lines.append(feed.render(ev(kind="scored", agent="kakalshi-crypto-first-minute-priced", pair="XRP", horizon="15m",
                                priced=True, market="m", side="yes", result="right", confidence=1.0, bar=0.955,
                                venue_result="yes", credit=-3.99, opened="2026-10-31T22:31:30",
                                closed="2026-10-31T22:45:00"), color=False))
    for line in lines:
        assert len(line) <= 101, (len(line), line)


# ---------- today's right-wrong, from midnight Eastern ----------

def test_the_day_starts_at_midnight_eastern_whatever_the_season():
    utc = dt.timezone.utc
    # November: Eastern is UTC-5, so midnight is 05:00 UTC. 11:59 PM and 12:01 AM fall on different days.
    assert feed.day_start(dt.datetime(2026, 11, 15, 4, 59, tzinfo=utc)) == dt.datetime(2026, 11, 14, 5, 0)
    assert feed.day_start(dt.datetime(2026, 11, 15, 5, 1, tzinfo=utc)) == dt.datetime(2026, 11, 15, 5, 0)
    # July: daylight time, UTC-4, so midnight is 04:00 UTC.
    assert feed.day_start(dt.datetime(2027, 7, 1, 12, 0, tzinfo=utc)) == dt.datetime(2027, 7, 1, 4, 0)
    # The morning the clocks go forward (2027-03-14): that day still began at midnight standard time.
    assert feed.day_start(dt.datetime(2027, 3, 14, 15, 0, tzinfo=utc)) == dt.datetime(2027, 3, 14, 5, 0)
    assert feed.day_start(dt.datetime(2027, 3, 15, 15, 0, tzinfo=utc)) == dt.datetime(2027, 3, 15, 4, 0)


def test_today_counts_only_calls_scored_since_midnight_and_starts_over_at_the_next(client, session_factory):
    from tests.api_helpers import Agent
    from tests.test_delisting import post_and_resolve
    from signal_room.db import Signal
    a = Agent(client, "a")
    post_and_resolve(client, session_factory, a, 3, right=True)
    post_and_resolve(client, session_factory, a, 1, right=False)
    db = session_factory()
    rows = db.query(Signal).filter(Signal.agent_id == a.id).order_by(Signal.submitted_at).all()
    # By hand: two right calls scored at 11:50 PM ET (04:50 UTC), one right and one wrong at 12:10 AM ET (05:10 UTC).
    for s, at in zip(rows, ("04:50", "04:50", "05:10", "05:10")):
        s.resolved_at = dt.datetime(2026, 11, 15, int(at[:2]), int(at[3:]))
    db.commit()
    late = dt.datetime(2026, 11, 15, 4, 55, tzinfo=dt.timezone.utc)      # 11:55 PM ET
    early = dt.datetime(2026, 11, 15, 5, 30, tzinfo=dt.timezone.utc)     # 12:30 AM ET, the next day
    assert feed.today_record(db, a.id, late) == (2, 0)
    assert feed.today_record(db, a.id, early) == (1, 1)
    db.close()


def test_the_clock_is_read_fresh_every_time(monkeypatch):
    seen = iter([dt.datetime(2026, 11, 15, 4, 59, tzinfo=dt.timezone.utc), dt.datetime(2026, 11, 15, 5, 1, tzinfo=dt.timezone.utc)])
    monkeypatch.setattr(feed, "_utcnow", lambda: next(seen))
    assert feed.day_start() != feed.day_start()                      # a feed left running rolls over at midnight
