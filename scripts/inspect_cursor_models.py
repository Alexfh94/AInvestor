#!/usr/bin/env python3
"""List Cursor models and inspect a recent agent run."""
from __future__ import annotations

import json
import sys

from ainvestor.config import get_settings
from ainvestor.db.models import AIDecision, SessionLocal


def main() -> int:
    from cursor_sdk import Cursor

    s = get_settings()
    if not s.cursor_api_key:
        print("No CURSOR_API_KEY")
        return 1

    models = Cursor.models.list(api_key=s.cursor_api_key)
    composer = [
        {
            "id": m.id,
            "display_name": getattr(m, "display_name", ""),
            "variants": [
                {
                    "display_name": v.display_name,
                    "is_default": v.is_default,
                    "params": [p.to_json() for p in v.params],
                }
                for v in getattr(m, "variants", []) or []
            ],
        }
        for m in models
        if "composer" in m.id.lower()
    ]
    print("=== Composer models ===")
    print(json.dumps(composer, indent=2))

    db = SessionLocal()
    try:
        decision = (
            db.query(AIDecision)
            .filter(AIDecision.run_id.isnot(None))
            .order_by(AIDecision.id.desc())
            .first()
        )
        if decision and decision.run_id:
            print("\n=== Latest run ===")
            print(
                json.dumps(
                    {
                        "stored_model": decision.model,
                        "run_id": decision.run_id,
                        "profile": decision.profile,
                        "created_at": str(decision.created_at),
                    },
                    indent=2,
                )
            )
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
