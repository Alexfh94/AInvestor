from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path

from ainvestor.config import get_settings
from ainvestor.models.schemas import AIUsage, CycleDecision, DecisionAction, TradeProposal

logger = logging.getLogger(__name__)


def find_cursor_agent_cli() -> Path | None:
    """Locate cursor-agent CLI installed by Cursor IDE (uses IDE login session)."""
    base = (
        Path(os.environ.get("APPDATA", ""))
        / "cursor"
        / "User"
        / "globalStorage"
        / "anysphere.cursor-agent-worker"
        / "agent-cli"
        / ".local"
        / "share"
        / "cursor-agent"
        / "versions"
    )
    if not base.is_dir():
        return None
    for version_dir in sorted(base.iterdir(), reverse=True):
        cmd = version_dir / "cursor-agent.cmd"
        if cmd.is_file():
            return cmd
    return None


def _parse_cursor_cli_stdout(stdout: str) -> tuple[str, AIUsage]:
    """Parse cursor-agent --output-format json stdout."""
    text = stdout.strip()
    usage = AIUsage()

    if not text:
        return "", usage

    try:
        envelope = json.loads(text)
        if isinstance(envelope, dict):
            if envelope.get("is_error"):
                raise RuntimeError(envelope.get("result") or "Cursor agent error")
            raw_usage = envelope.get("usage") or {}
            usage = AIUsage(
                input_tokens=int(raw_usage.get("inputTokens") or 0),
                output_tokens=int(raw_usage.get("outputTokens") or 0),
                cache_read_tokens=int(raw_usage.get("cacheReadTokens") or 0),
                cache_write_tokens=int(raw_usage.get("cacheWriteTokens") or 0),
            )
            result = envelope.get("result")
            if isinstance(result, str) and result.strip():
                return result.strip(), usage
    except json.JSONDecodeError:
        pass

    return text, usage


def _extract_cursor_cli_response(stdout: str) -> str:
    """Backward-compatible text extraction."""
    return _parse_cursor_cli_stdout(stdout)[0]


CYCLE_PROMPT_TEMPLATE = """You are AInvestor, a crypto trading analysis agent.

Analyze the following market context and propose trades OR recommend HOLD.

## Portfolio
{portfolio_summary}

## Market Summary
{market_summary}

## Technical Signals
{signals_summary}

## News
{news_summary}

## Sentiment
{sentiment_summary}

## Historical Learning
{learning_summary}

## Risk Rules
- Position sizing scales with conviction (amount_pct = % of available USDT):
  - conviction 50: max ~{max_position_mid:.0f}% of portfolio
  - conviction 70: max ~{max_position_base:.0f}%
  - conviction 90+: max ~{max_position_high:.0f}%
- Use higher amount_pct only when conviction justifies it
- Taker fee: ~{fee_pct:.2f}% per trade on {exchange} (round-trip ~{fee_round_trip:.2f}%)
- Take-profit must exceed round-trip fees to be profitable
- Required stop-loss and take-profit on every buy
- Whitelist pairs only: {whitelist}
- Max {max_trades} trades per day
- Min order: {min_order} USDT

## Instructions
Return ONLY valid JSON in this exact format (no markdown):
{{
  "hold": false,
  "summary": "Brief analysis",
  "proposals": [
    {{
      "action": "buy|sell|hold",
      "symbol": "BTC/USDT",
      "amount_pct": 5.0,
      "stop_loss_pct": 3.0,
      "take_profit_pct": 6.0,
      "conviction": 75,
      "reasoning": "Why this trade"
    }}
  ]
}}

If no action recommended, set "hold": true and "proposals": [].
Use MCP tools to get additional data if needed before deciding.
"""


def build_cycle_prompt(
    portfolio_summary: str,
    market_summary: str,
    signals_summary: str,
    news_summary: str,
    sentiment_summary: str,
    risk_config: dict,
    learning_summary: str = "",
) -> str:
    from ainvestor.engine.risk import max_position_pct_for_conviction

    pos = risk_config["position"]
    fees = risk_config.get("fees", {})
    fee_rate = float(fees.get("fallback_taker_rate", 0.001))
    exchange = fees.get("exchange", "binance")

    return CYCLE_PROMPT_TEMPLATE.format(
        portfolio_summary=portfolio_summary,
        market_summary=market_summary,
        signals_summary=signals_summary,
        news_summary=news_summary,
        sentiment_summary=sentiment_summary,
        learning_summary=learning_summary or "Sin historial evaluado aún.",
        max_position_base=pos["max_position_pct"],
        max_position_high=pos.get("max_position_pct_high_conviction", pos["max_position_pct"]),
        max_position_mid=max_position_pct_for_conviction(50, risk_config),
        fee_pct=fee_rate * 100,
        fee_round_trip=fee_rate * 2 * 100,
        exchange=exchange,
        min_order=pos["min_order_value_usdt"],
        whitelist=", ".join(risk_config["whitelist"]["pairs"]),
        max_trades=risk_config["limits"]["max_trades_per_day"],
    )


def parse_trade_proposal(text: str) -> CycleDecision:
    """Parse AI response into structured CycleDecision."""
    cleaned = text.strip()

    json_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned)
    if json_match:
        cleaned = json_match.group(1)

    brace_match = re.search(r"\{[\s\S]*\}", cleaned)
    if brace_match:
        cleaned = brace_match.group(0)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        logger.error("Failed to parse AI response: %s", e)
        return CycleDecision(hold=True, summary=f"Parse error: {e}")

    proposals: list[TradeProposal] = []
    for p in data.get("proposals", []):
        try:
            action_str = p.get("action", "hold").lower()
            if action_str == "hold":
                continue
            proposals.append(
                TradeProposal(
                    action=DecisionAction(action_str),
                    symbol=p.get("symbol", ""),
                    amount_pct=float(p.get("amount_pct", 0)),
                    stop_loss_pct=float(p.get("stop_loss_pct", 0)),
                    take_profit_pct=float(p.get("take_profit_pct", 0)),
                    conviction=int(p.get("conviction", 50)),
                    reasoning=p.get("reasoning", ""),
                )
            )
        except (ValueError, KeyError) as e:
            logger.warning("Skipping invalid proposal: %s", e)

    return CycleDecision(
        proposals=proposals,
        summary=data.get("summary", ""),
        hold=data.get("hold", False) or len(proposals) == 0,
    )


class AIAgent:
    """Cursor agent via SDK, CLI (IDE session) or OpenAI fallback."""

    def __init__(self):
        self.settings = get_settings()
        self._project_root = Path(__file__).resolve().parent.parent.parent

    async def run_cycle(
        self, prompt: str
    ) -> tuple[CycleDecision, str, str | None, AIUsage]:
        """Returns (decision, raw_response, run_id, token_usage)."""
        errors: list[str] = []
        empty_usage = AIUsage()

        if self.settings.cursor_api_key:
            try:
                return await self._run_cursor_sdk(prompt)
            except Exception as e:
                logger.warning("Cursor SDK failed: %s", e)
                errors.append(f"SDK: {e}")

        agent_cli = find_cursor_agent_cli()
        if agent_cli:
            try:
                return await self._run_cursor_cli(prompt, agent_cli)
            except Exception as e:
                logger.warning("Cursor CLI failed: %s", e)
                errors.append(f"CLI: {e}")

        if self.settings.ai_fallback_enabled and self.settings.openai_api_key:
            return await self._run_openai_fallback(prompt)

        summary = "No AI API configured"
        if errors:
            summary += f" ({'; '.join(errors)})"
        logger.warning("No AI configured - returning HOLD")
        return CycleDecision(hold=True, summary=summary), "", None, empty_usage

    async def _run_cursor_cli(
        self, prompt: str, agent_cli: Path
    ) -> tuple[CycleDecision, str, str | None, AIUsage]:
        proc = await asyncio.create_subprocess_exec(
            str(agent_cli),
            "-p",
            "--trust",
            "--model",
            self.settings.ai_model,
            "--output-format",
            "json",
            cwd=str(self._project_root),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate(input=prompt.encode("utf-8"))
        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip() or f"exit code {proc.returncode}"
            raise RuntimeError(err)

        raw_text, usage = _parse_cursor_cli_stdout(stdout.decode(errors="replace"))
        decision = parse_trade_proposal(raw_text)
        return decision, raw_text, None, usage

    async def _run_cursor_sdk(
        self, prompt: str
    ) -> tuple[CycleDecision, str, str | None, AIUsage]:
        from cursor_sdk import Agent, LocalAgentOptions

        mcp_servers = [
            {
                "name": "ainvestor-tools",
                "command": "python",
                "args": ["-m", "ainvestor.mcp_server"],
                "env": {"DATABASE_URL": self.settings.database_url},
            }
        ]

        create_kwargs: dict = {
            "model": self.settings.ai_model,
            "api_key": self.settings.cursor_api_key,
            "local": LocalAgentOptions(cwd=str(self._project_root)),
            "mcp_servers": mcp_servers,
        }

        with Agent.create(**create_kwargs) as agent:
            run = agent.send(prompt)
            result = run.wait()
            run_id = result.id if hasattr(result, "id") else None

            raw_text = ""
            if hasattr(result, "result") and result.result:
                raw_text = str(result.result)
            else:
                for msg in run.messages():
                    if msg.type == "assistant":
                        for block in msg.message.content:
                            if block.type == "text":
                                raw_text += block.text

            if result.status == "error":
                raise RuntimeError(f"Cursor agent run failed: {run_id}")

            decision = parse_trade_proposal(raw_text)
            return decision, raw_text, run_id, AIUsage()

    async def _run_openai_fallback(
        self, prompt: str
    ) -> tuple[CycleDecision, str, str | None, AIUsage]:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=self.settings.openai_api_key)
        response = await client.chat.completions.create(
            model=self.settings.openai_model,
            messages=[
                {
                    "role": "system",
                    "content": "You are a crypto trading analyst. Respond only with valid JSON.",
                },
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.3,
        )
        raw_text = response.choices[0].message.content or "{}"
        decision = parse_trade_proposal(raw_text)
        usage = AIUsage()
        if response.usage:
            usage = AIUsage(
                input_tokens=response.usage.prompt_tokens or 0,
                output_tokens=response.usage.completion_tokens or 0,
            )
        return decision, raw_text, response.id, usage
