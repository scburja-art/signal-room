"""The live feed: routine noise hidden, each bot tagged, results colored. Lines are real log shapes."""
from signal_room import feed


def test_noise_is_hidden():
    for line in (
        "2026-10-31 17:05:27,418 INFO agents.momentum: XRP: open signal still pending",
        'INFO:     127.0.0.1:61344 - "POST /signals HTTP/1.1" 409 Conflict',
        'INFO:     127.0.0.1:61078 - "POST /signals HTTP/1.1" 201 Created',
        "2026-10-31 17:03:53,843 INFO signal_room.scheduler: tick {'prices': 4, 'resolved': 0}",
        "==> logs/server.log <==",
        "",
    ):
        assert feed.render(line) is None, line


def test_each_bot_is_tagged_and_shortened():
    out = feed.render("2026-10-31 17:04:56,608 INFO agents.momentum: BTC short @77409.02 -> d516195904b", color=False)
    assert out == "17:04:56  momentum | BTC short @77409.02 -> d516195904b"
    out = feed.render("2026-10-31 17:04:57,144 INFO agents.reader: read SOL 15m short by kakalshi-crypto-first-minute", color=False)
    assert out.startswith("17:04:57    reader |")
    assert feed.render("2026-10-31 17:05:01,000 INFO signal_room.scheduler: tick {'prices': 4, 'resolved': 3}",
                       color=False) == "17:05:01      room | scoring tick {'prices': 4, 'resolved': 3}"


def test_scored_calls_carry_their_authors_color_and_result():
    line = ("2026-10-31 18:00:01,000 INFO signal_room.service: scored kakalshi-crypto-first-minute BTC 15m long at 75%: "
            "77400 -> 77500 = right (credit +0.75)")
    plain = feed.render(line, color=False)
    assert plain == "18:00:01  kakalshi | scored BTC 15m long at 75%: 77400 -> 77500 = RIGHT (credit +0.75)"
    colored = feed.render(line, color=True)
    assert feed.WHO["kakalshi"] in colored and feed.GREEN in colored
    wrong = feed.render(line.replace("= right (credit +0.75)", "= wrong (credit -2.25)"), color=True)
    assert feed.RED in wrong and "WRONG" in wrong
    meanrev = feed.render(line.replace("kakalshi-crypto-first-minute", "meanrev-control"), color=True)
    assert feed.WHO["meanrev"] in meanrev


def test_problems_stand_out():
    warn = feed.render("2026-10-31 16:55:08,490 WARNING agents.meanrev: BTC: candle fetch failed: 403 Forbidden", color=True)
    assert feed.YELLOW in warn
    assert feed.RED in feed.render("Traceback (most recent call last):", color=True)
    assert feed.RED in feed.render("ModuleNotFoundError: No module named 'base58'", color=True)


def test_a_real_resolution_writes_a_line_the_feed_colors(client, session_factory, caplog):
    import logging
    from tests.api_helpers import Agent
    from tests.test_delisting import post_and_resolve
    a = Agent(client, "kakalshi-crypto-first-minute")
    with caplog.at_level(logging.INFO, logger="signal_room.service"):
        post_and_resolve(client, session_factory, a, 1, right=True, confidence=0.75)
    rec = next(r for r in caplog.records if r.getMessage().startswith("scored "))
    line = f"2026-10-31 18:00:01,000 INFO signal_room.service: {rec.getMessage()}"
    assert feed.render(line, color=False) == (
        "18:00:01  kakalshi | scored BTC 15m long at 75%: 100 -> 101 = RIGHT (credit +0.75)")
