#!/bin/bash
DB=/opt/ainvestor/data/ainvestor.db

echo "=== PORTFOLIOS ==="
sqlite3 -header -column "$DB" "SELECT id, profile, quote_balance, initial_balance, realized_pnl, kill_switch_active FROM portfolios ORDER BY profile;"

echo ""
echo "=== OPEN POSITIONS ==="
sqlite3 -header -column "$DB" "SELECT portfolio_id, symbol, instrument_type, position_side, leverage, margin_used, amount, entry_price, stop_loss, take_profit, opened_at FROM positions WHERE is_open=1;"

echo ""
echo "=== TRADES BY PROFILE ==="
sqlite3 -header -column "$DB" "SELECT p.profile, t.instrument_type, t.position_side, t.leverage, COUNT(*) as n, ROUND(SUM(t.value_usdt),2) as vol_usdt FROM trades t JOIN portfolios p ON p.id=t.portfolio_id GROUP BY p.profile, t.instrument_type, t.position_side, t.leverage ORDER BY p.profile, n DESC;"

echo ""
echo "=== RECENT TRADES ==="
sqlite3 -header -column "$DB" "SELECT p.profile, t.executed_at, t.symbol, t.side, t.instrument_type, t.leverage, t.position_side, ROUND(t.value_usdt,2) as usdt, ROUND(t.fee,4) as fee FROM trades t JOIN portfolios p ON p.id=t.portfolio_id ORDER BY t.executed_at DESC LIMIT 20;"

echo ""
echo "=== AI DECISIONS SUMMARY ==="
sqlite3 -header -column "$DB" "SELECT profile, COUNT(*) as cycles, SUM(approved_count) as approved, SUM(rejected_count) as rejected, SUM(CASE WHEN hold=1 THEN 1 ELSE 0 END) as holds, ROUND(AVG(tokens_total),0) as avg_tokens FROM ai_decisions GROUP BY profile;"

echo ""
echo "=== LAST 12 DECISIONS ==="
sqlite3 -header -column "$DB" "SELECT profile, created_at, hold, approved_count, rejected_count, substr(summary,1,150) as summary FROM ai_decisions ORDER BY created_at DESC LIMIT 12;"

echo ""
echo "=== PROPOSALS WITH PERP (from json) ==="
sqlite3 "$DB" "SELECT profile, created_at, proposals_json FROM ai_decisions WHERE proposals_json LIKE '%perpetual%' ORDER BY created_at DESC LIMIT 10;"

echo ""
echo "=== DECISION OUTCOMES ==="
sqlite3 -header -column "$DB" "SELECT profile, outcome, execution_status, COALESCE(instrument_type,'spot') as inst, COUNT(*) as n FROM decision_outcomes GROUP BY profile, outcome, execution_status, instrument_type ORDER BY profile, n DESC;"

echo ""
echo "=== LEARNING ACCURACY ==="
sqlite3 -header -column "$DB" "SELECT profile, outcome, COUNT(*) as n, ROUND(AVG(return_pct),2) as avg_ret FROM decision_outcomes WHERE outcome!='pending' AND return_pct IS NOT NULL GROUP BY profile, outcome;"

echo ""
echo "=== CYCLE RUNS ==="
sqlite3 -header -column "$DB" "SELECT profile, status, COUNT(*) as n FROM cycle_runs GROUP BY profile, status;"

echo ""
echo "=== RISK REJECTIONS (top reasons) ==="
sqlite3 -header -column "$DB" "SELECT symbol, substr(details,1,120) as reason, COUNT(*) as n FROM risk_events WHERE event_type='proposal_rejected' GROUP BY details ORDER BY n DESC LIMIT 15;"

echo ""
echo "=== LATEST PORTFOLIO VALUES ==="
sqlite3 -header -column "$DB" "SELECT p.profile, pv.total_value_usdt, pv.quote_balance, pv.invested_usdt, pv.captured_at FROM portfolio_value_history pv JOIN portfolios p ON p.id=pv.portfolio_id WHERE pv.id IN (SELECT MAX(id) FROM portfolio_value_history GROUP BY portfolio_id) ORDER BY p.profile;"

echo ""
echo "=== PNL EVOLUTION (last 20 snapshots extreme) ==="
sqlite3 -header -column "$DB" "SELECT pv.captured_at, ROUND(pv.total_value_usdt,2) as total, ROUND(pv.quote_balance,2) as cash FROM portfolio_value_history pv JOIN portfolios p ON p.id=pv.portfolio_id WHERE p.profile='extreme' ORDER BY pv.captured_at DESC LIMIT 20;"

echo ""
echo "=== DERIVATIVES RECORDS (latest per symbol) ==="
sqlite3 -header -column "$DB" "SELECT symbol, funding_rate_pct, mark_price, open_interest, captured_at FROM derivatives_records WHERE id IN (SELECT MAX(id) FROM derivatives_records GROUP BY symbol) ORDER BY ABS(funding_rate_pct) DESC LIMIT 10;"
