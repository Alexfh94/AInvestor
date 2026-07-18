#!/usr/bin/env python3
"""Auditoría rápida de ainvestor.db para informe operativo A/B (agresiva vs extrema)."""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

DB = Path(sys.argv[1] if len(sys.argv) > 1 else "/app/data/ainvestor.db")


def main() -> None:
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row

    def section(title: str) -> None:
        print(f"\n=== {title} ===")

    section("PORTFOLIOS")
    for r in c.execute(
        "SELECT id,profile,quote_balance,initial_balance,realized_pnl,kill_switch_active "
        "FROM portfolios ORDER BY profile"
    ):
        print(dict(r))

    section("OPEN POSITIONS")
    rows = list(
        c.execute(
            "SELECT portfolio_id,symbol,instrument_type,position_side,leverage,margin_used,"
            "amount,entry_price,opened_at "
            "FROM positions WHERE is_open=1"
        )
    )
    if not rows:
        print("(ninguna)")
    for r in rows:
        print(dict(r))

    section("TRADES BY PROFILE/INSTRUMENT")
    for r in c.execute(
        """
        SELECT p.profile,t.instrument_type,t.position_side,t.leverage,COUNT(*) n,
               ROUND(SUM(t.value_usdt),2) vol
        FROM trades t JOIN portfolios p ON p.id=t.portfolio_id
        GROUP BY 1,2,3,4 ORDER BY 1,n DESC
        """
    ):
        print(dict(r))

    section("RECENT TRADES (20)")
    for r in c.execute(
        """
        SELECT p.profile,t.executed_at,t.symbol,t.side,t.instrument_type,t.leverage,
               t.position_side,ROUND(t.value_usdt,2) usdt,ROUND(t.fee,4) fee
        FROM trades t JOIN portfolios p ON p.id=t.portfolio_id
        ORDER BY t.executed_at DESC LIMIT 20
        """
    ):
        print(dict(r))

    section("AI DECISIONS SUMMARY")
    for r in c.execute(
        """
        SELECT profile,COUNT(*) cycles,SUM(approved_count) approved,SUM(rejected_count) rejected,
               SUM(CASE WHEN hold=1 THEN 1 ELSE 0 END) holds,ROUND(AVG(tokens_total),0) avg_tokens
        FROM ai_decisions GROUP BY profile
        """
    ):
        print(dict(r))

    section("LAST 10 DECISIONS")
    for r in c.execute(
        "SELECT profile,created_at,hold,approved_count,rejected_count,substr(summary,1,200) summary "
        "FROM ai_decisions ORDER BY created_at DESC LIMIT 10"
    ):
        print(dict(r))

    section("PERP PROPOSALS (historial)")
    perp_count = 0
    for r in c.execute(
        "SELECT profile,created_at,approved_count,proposals_json FROM ai_decisions "
        "WHERE proposals_json LIKE '%perpetual%' ORDER BY created_at DESC"
    ):
        try:
            props = json.loads(r["proposals_json"] or "[]")
        except json.JSONDecodeError:
            continue
        for p in props:
            if p.get("instrument_type") == "perpetual":
                perp_count += 1
                print(
                    r["profile"],
                    r["created_at"],
                    p.get("symbol"),
                    p.get("action"),
                    p.get("position_side"),
                    f"{p.get('leverage')}x",
                    f"amt={p.get('amount_pct')}%",
                )
    print(f"total perp proposals: {perp_count}")

    section("PERP TRADES EXECUTED")
    for r in c.execute(
        """
        SELECT p.profile,t.executed_at,t.symbol,t.side,t.leverage,t.position_side,t.value_usdt
        FROM trades t JOIN portfolios p ON p.id=t.portfolio_id
        WHERE t.instrument_type='perpetual' ORDER BY t.executed_at DESC
        """
    ):
        print(dict(r))
    perp_trades = c.execute(
        "SELECT COUNT(*) FROM trades WHERE instrument_type='perpetual'"
    ).fetchone()[0]
    print(f"total perp trades executed: {perp_trades}")

    section("OUTCOMES")
    for r in c.execute(
        """
        SELECT profile,outcome,execution_status,COALESCE(instrument_type,'spot') inst,COUNT(*) n
        FROM decision_outcomes GROUP BY 1,2,3,4 ORDER BY 1,n DESC
        """
    ):
        print(dict(r))

    section("LEARNING (evaluated)")
    for r in c.execute(
        """
        SELECT profile,outcome,COUNT(*) n,ROUND(AVG(return_pct),2) avg_ret
        FROM decision_outcomes WHERE outcome!='pending' AND return_pct IS NOT NULL
        GROUP BY 1,2
        """
    ):
        print(dict(r))

    section("CYCLE RUNS")
    for r in c.execute("SELECT profile,status,COUNT(*) n FROM cycle_runs GROUP BY 1,2"):
        print(dict(r))

    section("TOP REJECTION REASONS")
    reasons: Counter[str] = Counter()
    for (details,) in c.execute(
        "SELECT details FROM risk_events WHERE event_type='proposal_rejected'"
    ):
        for part in (details or "").split("; "):
            if part.strip():
                reasons[part.strip()] += 1
    for reason, n in reasons.most_common(15):
        print(n, reason[:110])

    section("LATEST PORTFOLIO VALUES (portfolio_value_history)")
    for r in c.execute(
        """
        SELECT p.profile,pv.total_value_usdt,pv.quote_balance,pv.invested_usdt,pv.captured_at
        FROM portfolio_value_history pv JOIN portfolios p ON p.id=pv.portfolio_id
        WHERE pv.id IN (
            SELECT MAX(id) FROM portfolio_value_history GROUP BY portfolio_id
        )
        ORDER BY p.profile
        """
    ):
        d = dict(r)
        init = c.execute(
            "SELECT initial_balance,realized_pnl FROM portfolios WHERE profile=?",
            (d["profile"],),
        ).fetchone()
        initial = init[0] if init else 0
        realized = init[1] if init else 0
        d["pnl_pct"] = (
            round((d["total_value_usdt"] - initial) / initial * 100, 2) if initial else 0
        )
        d["realized_pnl"] = realized
        print(d)

    section("EXTREME PORTFOLIO SUMMARY")
    for r in c.execute(
        """
        SELECT p.profile,
               p.initial_balance,
               p.realized_pnl,
               COALESCE(latest.total_value_usdt, p.quote_balance) AS current_value,
               COALESCE(fees.total_fees, 0) AS total_fees,
               COALESCE(tc.trade_count, 0) AS trade_count,
               COALESCE(tc.perp_count, 0) AS perp_trades
        FROM portfolios p
        LEFT JOIN (
            SELECT portfolio_id, total_value_usdt
            FROM portfolio_value_history
            WHERE id IN (SELECT MAX(id) FROM portfolio_value_history GROUP BY portfolio_id)
        ) latest ON latest.portfolio_id = p.id
        LEFT JOIN (
            SELECT portfolio_id, ROUND(SUM(fee), 4) AS total_fees
            FROM trades GROUP BY portfolio_id
        ) fees ON fees.portfolio_id = p.id
        LEFT JOIN (
            SELECT portfolio_id,
                   COUNT(*) AS trade_count,
                   SUM(CASE WHEN instrument_type='perpetual' THEN 1 ELSE 0 END) AS perp_count
            FROM trades GROUP BY portfolio_id
        ) tc ON tc.portfolio_id = p.id
        WHERE p.profile = 'extreme'
        """
    ):
        d = dict(r)
        init = d["initial_balance"] or 0
        cur = d["current_value"] or 0
        d["return_pct"] = round((cur - init) / init * 100, 2) if init else 0
        d["fee_pct_of_initial"] = round((d["total_fees"] or 0) / init * 100, 2) if init else 0
        print(d)

    section("EXTREME VALUE HISTORY (15)")
    for r in c.execute(
        """
        SELECT pv.captured_at,ROUND(pv.total_value_usdt,2) total,ROUND(pv.quote_balance,2) cash,
               ROUND(pv.invested_usdt,2) invested
        FROM portfolio_value_history pv JOIN portfolios p ON p.id=pv.portfolio_id
        WHERE p.profile='extreme' ORDER BY pv.captured_at DESC LIMIT 15
        """
    ):
        print(dict(r))

    section("FEES BY PROFILE")
    for r in c.execute(
        """
        SELECT p.profile,ROUND(SUM(t.fee),4) fees,COUNT(*) trades,
               ROUND(SUM(t.value_usdt),2) volume
        FROM trades t JOIN portfolios p ON p.id=t.portfolio_id GROUP BY p.profile
        """
    ):
        print(dict(r))

    section("SCHEDULER / ERRORS")
    err = c.execute(
        "SELECT COUNT(*) FROM ai_decisions WHERE summary LIKE '%No AI API%' OR summary LIKE '%Parse error%'"
    ).fetchone()[0]
    print("bad_ai_cycles:", err)


if __name__ == "__main__":
    main()
