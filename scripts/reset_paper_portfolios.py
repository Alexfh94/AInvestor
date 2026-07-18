#!/usr/bin/env python3
"""CLI: reset paper portfolios and all trading/AI DB records."""
from __future__ import annotations

import argparse
import json
import sys

from ainvestor.db.models import SessionLocal
from ainvestor.services.paper_reset import reset_paper_portfolios


def main() -> int:
    parser = argparse.ArgumentParser(description="Reset AInvestor paper portfolios to initial state")
    parser.add_argument(
        "--clear-market",
        action="store_true",
        help="Also clear market snapshots, news, sentiment and derivatives history",
    )
    args = parser.parse_args()
    db = SessionLocal()
    try:
        result = reset_paper_portfolios(db, clear_market_history=args.clear_market)
        print(json.dumps(result, indent=2))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
