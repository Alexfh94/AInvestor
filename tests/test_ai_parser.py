"""Tests for AI response parsing."""

from ainvestor.engine.ai_agent import parse_trade_proposal
from ainvestor.models.schemas import DecisionAction


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
