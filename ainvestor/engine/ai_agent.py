from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path

from ainvestor.config import get_settings
from ainvestor.models.schemas import (
    AIUsage,
    AssetClass,
    CycleDecision,
    DecisionAction,
    InstrumentType,
    TradeProposal,
)

logger = logging.getLogger(__name__)
_SDK_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cursor-sdk")


def _format_error(exc: BaseException) -> str:
    msg = str(exc).strip()
    return msg or f"{type(exc).__name__}: {exc!r}"


def find_cursor_agent_cli() -> Path | None:
    """Locate cursor-agent CLI installed by Cursor IDE (uses IDE login session)."""
    appdata = Path(os.environ.get("APPDATA", ""))
    bases = [
        appdata / "Cursor" / "User" / "globalStorage" / "anysphere.cursor-agent-worker",
        appdata / "cursor" / "User" / "globalStorage" / "anysphere.cursor-agent-worker",
    ]
    for base in bases:
        versions = base / "agent-cli" / ".local" / "share" / "cursor-agent" / "versions"
        if not versions.is_dir():
            continue
        for version_dir in sorted(versions.iterdir(), reverse=True):
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


CYCLE_PROMPT_TEMPLATE = """You are AInvestor, a multi-asset trading analysis agent (crypto spot and perpetuals).

## Portfolio profile: {profile_label}
{profile_instructions}

Analyze the context in layers: MACRO → SECTOR → PAIR/ASSET → EXECUTION.

## Layer 1 — Macro context
{macro_summary}

## Layer 2 — Portfolio
{portfolio_summary}

## Layer 3 — Market (crypto + stocks)
{market_summary}

## Layer 4 — Derivatives (funding / OI)
{derivatives_summary}

## Layer 5 — Technical signals (multi-timeframe + ATR)
{signals_summary}

## Layer 6 — News (filtered)
{news_summary}

## Layer 7 — Sentiment
{sentiment_summary}

## Layer 8 — Historical learning
{learning_summary}

## Risk rules
- Position sizing scales with conviction (amount_pct = % of available balance):
  - conviction 50: max ~{max_position_mid:.0f}%
  - conviction 70: max ~{max_position_base:.0f}%
  - conviction 90+: max ~{max_position_high:.0f}%
- Taker fee: ~{fee_pct:.2f}% per trade on {exchange} (round-trip ~{fee_round_trip:.2f}%)
- Take-profit must exceed round-trip fees
- Required stop-loss and take-profit on every buy/open
- Crypto whitelist: {whitelist}
- Stocks: disabled for this profile
- Max leverage perpetuals: {max_leverage}x (MiFID retail cap)
- Max {max_trades} trades per day | Min order: {min_order} USDT
- Perps: only short if funding favorable; no perp+spot same asset without hedge
- Crypto spot and perpetuals only (no stocks)
{mcp_instruction}

## Instructions
Return ONLY valid JSON (no markdown):
{{
  "hold": false,
  "summary": "Brief layered analysis",
  "allocation": {{"crypto": 60, "stocks": 30, "derivatives": 10}},
  "proposals": [
    {{
      "action": "buy|sell|hold",
      "symbol": "BTC/USDT",
      "instrument_type": "spot|perpetual|stock",
      "position_side": "long|short",
      "leverage": 1,
      "asset_class": "crypto|stock|derivative",
      "amount_pct": 5.0,
      "stop_loss_pct": 3.0,
      "take_profit_pct": 6.0,
      "conviction": 75,
      "reasoning": "Why this trade"
    }}
  ]
}}

If no action recommended, set "hold": true and "proposals": [].
Spot buy = long only. Perpetual short uses action "sell" with position_side "short".
"""


PROFILE_PROMPT_INSTRUCTIONS = {
    "conservative": (
        "CONSERVATIVE profile: prioritize capital preservation. Favor BTC/ETH as anchors. "
        "Require high conviction (>=75) for alts. Prefer hold when signals are mixed. "
        "Use moderate sizing (5-15% amount_pct). Perps only with 1x leverage and clear edge."
    ),
    "aggressive": (
        "AGGRESSIVE / GAMBLER profile: maximize upside in liquid altcoins only (no BTC/ETH). "
        "Be bold — when momentum and conviction align (>=70), size 60-100% (all-in OK). "
        "Use 2x leverage on perps when edge is clear. Prefer action over hold. "
        "Accept higher drawdown for bigger wins; stack perps on strong setups."
    ),
}


def build_cycle_prompt(
    portfolio_summary: str,
    market_summary: str,
    signals_summary: str,
    news_summary: str,
    sentiment_summary: str,
    risk_config: dict,
    learning_summary: str = "",
    macro_summary: str = "",
    derivatives_summary: str = "",
    market_status: str = "",
    use_mcp: bool = False,
    profile: str = "conservative",
) -> str:
    from ainvestor.engine.risk import max_position_pct_for_conviction
    from ainvestor.portfolio.profiles import PROFILE_LABELS, normalize_profile

    prof = normalize_profile(profile)
    pos = risk_config["position"]
    fees = risk_config.get("fees", {})
    fee_rate = float(fees.get("fallback_taker_rate", 0.001))
    exchange = fees.get("exchange", "binance")
    deriv = risk_config.get("derivatives", {})

    mcp_instruction = (
        "- Use MCP tools to get additional data if needed before deciding."
        if use_mcp
        else "- MCP tools not available in this session; use only the context above."
    )

    return CYCLE_PROMPT_TEMPLATE.format(
        profile_label=PROFILE_LABELS.get(prof, prof),
        profile_instructions=PROFILE_PROMPT_INSTRUCTIONS.get(prof, ""),
        macro_summary=macro_summary or "No macro data.",
        portfolio_summary=portfolio_summary,
        market_summary=market_summary,
        derivatives_summary=derivatives_summary or "No derivatives data.",
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
        max_leverage=deriv.get("max_leverage", 2),
        max_trades=risk_config["limits"]["max_trades_per_day"],
        market_status=market_status or "unknown",
        mcp_instruction=mcp_instruction,
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
            inst = p.get("instrument_type", "spot").lower()
            ac = p.get("asset_class")
            if not ac:
                ac = "stock" if inst == "stock" else "derivative" if inst == "perpetual" else "crypto"
            proposals.append(
                TradeProposal(
                    action=DecisionAction(action_str),
                    symbol=p.get("symbol", ""),
                    amount_pct=float(p.get("amount_pct", 0)),
                    stop_loss_pct=float(p.get("stop_loss_pct", 0)),
                    take_profit_pct=float(p.get("take_profit_pct", 0)),
                    conviction=int(p.get("conviction", 50)),
                    reasoning=p.get("reasoning", ""),
                    instrument_type=InstrumentType(inst),
                    position_side=p.get("position_side", "long"),
                    leverage=int(p.get("leverage", 1)),
                    asset_class=AssetClass(ac),
                )
            )
        except (ValueError, KeyError) as e:
            logger.warning("Skipping invalid proposal: %s", e)

    allocation = data.get("allocation") or {}

    return CycleDecision(
        proposals=proposals,
        summary=data.get("summary", ""),
        hold=data.get("hold", False) or len(proposals) == 0,
        allocation=allocation,
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
                logger.warning("Cursor SDK failed: %s", _format_error(e), exc_info=True)
                errors.append(f"SDK: {_format_error(e)}")

        agent_cli = find_cursor_agent_cli()
        if agent_cli:
            try:
                return await self._run_cursor_cli(prompt, agent_cli)
            except Exception as e:
                logger.warning("Cursor CLI failed: %s", _format_error(e), exc_info=True)
                errors.append(f"CLI: {_format_error(e)}")

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
            self.settings.effective_ai_model(),
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
        """Run Cursor SDK in a dedicated thread (uvicorn/Windows event-loop safe)."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            _SDK_EXECUTOR,
            partial(
                _run_cursor_sdk_blocking,
                prompt,
                self.settings.cursor_api_key,
                self.settings.effective_ai_model(),
                self.settings.database_url,
                self._project_root,
            ),
        )


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


def _run_cursor_sdk_blocking(
    prompt: str,
    api_key: str,
    model: str,
    database_url: str,
    project_root: Path,
) -> tuple[CycleDecision, str, str | None, AIUsage]:
    return asyncio.run(
        _cursor_sdk_async(prompt, api_key, model, database_url, project_root)
    )


async def _cursor_sdk_async(
    prompt: str,
    api_key: str,
    model: str,
    database_url: str,
    project_root: Path,
) -> tuple[CycleDecision, str, str | None, AIUsage]:
    from cursor_sdk import AgentOptions, LocalAgentOptions, SendOptions
    from cursor_sdk.asyncio import AsyncAgent, AsyncClient

    mcp_servers = {
        "ainvestor-tools": {
            "command": "python",
            "args": ["-m", "ainvestor.mcp_server"],
            "env": {"DATABASE_URL": database_url},
        }
    }

    options = AgentOptions(
        model=model,
        api_key=api_key,
        local=LocalAgentOptions(cwd=str(project_root)),
    )

    client = await AsyncClient.launch_bridge(workspace=str(project_root))
    try:
        agent = await AsyncAgent.create(options, client=client)
        try:
            run = await agent.send(prompt, SendOptions(mcp_servers=mcp_servers))
            result = await run.wait()
            run_id = result.id if hasattr(result, "id") else None

            raw_text = ""
            if hasattr(result, "result") and result.result:
                raw_text = str(result.result)
            else:
                async for msg in run.messages():
                    if msg.type == "assistant":
                        for block in msg.message.content:
                            if block.type == "text":
                                raw_text += block.text

            if result.status == "error":
                raise RuntimeError(f"Cursor agent run failed: {run_id}")

            usage = _extract_usage_from_run(result, run)
            decision = parse_trade_proposal(raw_text)
            return decision, raw_text, run_id, usage
        finally:
            await agent.close()
    finally:
        await client.aclose()


def _extract_usage_from_run(result, run) -> AIUsage:
    """Extract token usage from Cursor async SDK RunResult."""
    usage = AIUsage()
    raw_usage = None
    if hasattr(result, "usage") and result.usage:
        raw_usage = result.usage
    elif hasattr(run, "usage") and run.usage:
        raw_usage = run.usage

    if raw_usage:
        if isinstance(raw_usage, dict):
            usage = AIUsage(
                input_tokens=int(raw_usage.get("inputTokens") or raw_usage.get("input_tokens") or 0),
                output_tokens=int(raw_usage.get("outputTokens") or raw_usage.get("output_tokens") or 0),
                cache_read_tokens=int(raw_usage.get("cacheReadTokens") or raw_usage.get("cache_read_tokens") or 0),
                cache_write_tokens=int(raw_usage.get("cacheWriteTokens") or raw_usage.get("cache_write_tokens") or 0),
            )
        else:
            usage = AIUsage(
                input_tokens=int(getattr(raw_usage, "input_tokens", 0) or getattr(raw_usage, "inputTokens", 0) or 0),
                output_tokens=int(getattr(raw_usage, "output_tokens", 0) or getattr(raw_usage, "outputTokens", 0) or 0),
                cache_read_tokens=int(getattr(raw_usage, "cache_read_tokens", 0) or getattr(raw_usage, "cacheReadTokens", 0) or 0),
                cache_write_tokens=int(getattr(raw_usage, "cache_write_tokens", 0) or getattr(raw_usage, "cacheWriteTokens", 0) or 0),
            )
    return usage
