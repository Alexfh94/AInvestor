#!/usr/bin/env python3
"""CLI: remove aggressive portfolio and all related DB records."""
from __future__ import annotations

import json
import sys

from ainvestor.db.models import SessionLocal
from ainvestor.services.paper_reset import remove_aggressive_portfolio


def main() -> int:
    db = SessionLocal()
    try:
        result = remove_aggressive_portfolio(db)
        print(json.dumps(result, indent=2))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
