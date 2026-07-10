#!/usr/bin/env python3
"""Run a single AI trading cycle manually."""

import asyncio
import logging
import sys

from ainvestor.cycle_runner import CycleRunner
from ainvestor.db import SessionLocal, init_db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def main():
    init_db()
    db = SessionLocal()
    try:
        runner = CycleRunner(db)
        result = await runner.run()
        logger.info("Cycle result: %s", result)
        if result.get("status") == "error":
            sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
