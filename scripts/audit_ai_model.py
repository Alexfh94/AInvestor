#!/usr/bin/env python3
"""Audit which AI model AInvestor requests vs stores."""
from __future__ import annotations

import json

from ainvestor.config import get_settings
from ainvestor.db.models import AIDecision, CycleRun, SessionLocal


def main() -> None:
    s = get_settings()
    db = SessionLocal()
    try:
        decisions = (
            db.query(AIDecision)
            .order_by(AIDecision.id.desc())
            .limit(15)
            .all()
        )
        cycles = (
            db.query(CycleRun)
            .order_by(CycleRun.id.desc())
            .limit(5)
            .all()
        )
        payload = {
            "config": {
                "ai_model": s.ai_model,
                "effective_ai_model": s.effective_ai_model(),
                "ai_use_fast": s.ai_use_fast,
                "has_cursor_api_key": bool(s.cursor_api_key),
            },
            "recent_decisions": [
                {
                    "id": d.id,
                    "model": d.model,
                    "profile": d.profile,
                    "created_at": str(d.created_at),
                    "run_id": d.run_id,
                }
                for d in decisions
            ],
            "recent_cycles": [
                {
                    "id": c.id,
                    "profile": c.profile,
                    "status": c.status,
                    "started_at": str(c.started_at),
                }
                for c in cycles
            ],
        }
        print(json.dumps(payload, indent=2))
    finally:
        db.close()


if __name__ == "__main__":
    main()
