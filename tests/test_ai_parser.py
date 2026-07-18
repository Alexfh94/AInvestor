"""Tests for AI response parsing and prompt building."""

from ainvestor.config import load_risk_config
from ainvestor.engine.ai_agent import (
    _is_bridge_token_arg_error,
    build_cycle_prompt,
    parse_trade_proposal,
)
from ainvestor.models.schemas import DecisionAction
from ainvestor.portfolio.profiles import PROFILE_EXTREME


def test_build_cycle_prompt_includes_execution_cadence():
    prompt = build_cycle_prompt(
        portfolio_summary="100 USDT cash",
        market_summary="BTC flat",
        signals_summary="neutral",
        news_summary="none",
        sentiment_summary="neutral",
        risk_config=load_risk_config(profile=PROFILE_EXTREME),
        profile=PROFILE_EXTREME,
        ai_cycle_interval_minutes=30,
        risk_monitor_interval_minutes=5,
    )
    assert "every **30 minutes** only" in prompt
    assert "every **5 minutes**" in prompt
    assert "1–3 cycles" in prompt
    assert "90 min" in prompt


def test_parse_valid_json():
    text = '{"hold": false, "summary": "Bullish BTC", "proposals": [{"action": "buy", "symbol": "BTC/USDT", "amount_pct": 5, "stop_loss_pct": 3, "take_profit_pct": 6, "conviction": 80, "reasoning": "test"}]}'
    decision = parse_trade_proposal(text)
    assert decision.hold is False
    assert len(decision.proposals) == 1
    assert decision.proposals[0].action == DecisionAction.BUY


def test_parse_markdown_wrapped():
    text = '```json\n{"hold": true, "summary": "Wait", "proposals": []}\n```'
    decision = parse_trade_proposal(text)
    assert decision.hold is True
    assert len(decision.proposals) == 0


def test_parse_invalid_returns_hold():
    decision = parse_trade_proposal("not json at all")
    assert decision.hold is True


def test_bridge_token_arg_error_detection():
    exc = RuntimeError("Missing value for --tool-callback-auth-token")
    assert _is_bridge_token_arg_error(exc) is True
    assert _is_bridge_token_arg_error(RuntimeError("connection refused")) is False
