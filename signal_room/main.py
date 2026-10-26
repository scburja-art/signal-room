"""Run the API and the background jobs in one process."""
import logging

import uvicorn

from . import config, scheduler
from .api import app
from .db import init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)


def run() -> None:
    if not config.MASTER_KEY:
        raise SystemExit("SIGNAL_ROOM_MASTER_KEY is not set. Run ./start.sh, which generates one into .env.")
    init_db()
    stop = scheduler.start()
    try:
        uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="info")
    finally:
        stop.set()


if __name__ == "__main__":
    run()
