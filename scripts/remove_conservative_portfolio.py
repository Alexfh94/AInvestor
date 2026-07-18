#!/usr/bin/env python3
"""CLI: remove conservative portfolio and all related DB records."""
from __future__ import annotations

import json
import sys

from ainvestor.db.models import SessionLocal
from ainvestor.services.paper_reset import remove_conservative_portfolio


def main() -> int:
    db = SessionLocal()
    try:
        result = remove_conservative_portfolio(db)
        print(json.dumps(result, indent=2))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
