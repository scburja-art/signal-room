"""Entry point: ./start.sh agent momentum | meanrev | kakalshi <shadow_results.json>"""
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

BASE_URL = os.getenv("SIGNAL_ROOM_URL", f"http://{os.getenv('SIGNAL_ROOM_HOST', '127.0.0.1')}:{os.getenv('SIGNAL_ROOM_PORT', '8000')}")
STATE_DIR = os.path.join(os.path.dirname(__file__), "state")


def main(argv: list[str]) -> None:
    if not argv:
        raise SystemExit("usage: agent momentum | agent meanrev | agent kakalshi <path/to/shadow_results.json>")
    which = argv[0]
    if which == "momentum":
        from . import momentum_control
        momentum_control.main(BASE_URL, os.path.join(STATE_DIR, "momentum.json"))
    elif which == "meanrev":
        from . import mean_reversion_control
        mean_reversion_control.main(BASE_URL, os.path.join(STATE_DIR, "meanrev.json"))
    elif which == "kakalshi":
        if len(argv) < 2:
            raise SystemExit("usage: agent kakalshi <path/to/shadow_results.json>")
        from . import kakalshi_adapter
        kakalshi_adapter.main(BASE_URL, argv[1], os.path.join(STATE_DIR, "kakalshi.json"))
    else:
        raise SystemExit(f"unknown agent: {which}")


if __name__ == "__main__":
    main(sys.argv[1:])
