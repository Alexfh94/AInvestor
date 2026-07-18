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
_BRIDGE_TOKEN_ARG_ERROR = "tool-callback-auth-token"
_MAX_BRIDGE_LAUNCH_ATTEMPTS = 8


def _format_error(exc: BaseException) -> str:
    msg = str(exc).strip()
    return msg or f"{type(exc).__name__}: {exc!r}"


def _is_bridge_token_arg_error(exc: BaseException) -> bool:
    msg = _format_error(exc).lower()
    return "missing value for --tool-callback-auth-token" in msg or (
        _BRIDGE_TOKEN_ARG_ERROR in msg and "missing value" in msg
    )


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

## Execution cadence (plan trades with this in mind)
- Your discretionary decisions (open / close / rotate / HOLD) run every **{ai_cycle_interval} minutes** only.
- Stop-loss, take-profit and liquidation are checked automatically every **{risk_monitor_interval} minutes** (no new AI call).
- Between AI cycles you **cannot** change your mind: only SL/TP/liquidation can exit early.
- If you open a position, assume you hold until the **next AI cycle** (~{ai_cycle_interval} min) unless SL/TP triggers.
- Set SL/TP for moves realistic within **1–3 cycles** ({cycles_horizon_min}–{cycles_horizon_max} min); include round-trip fees and 8h funding if hold may cross a funding event.
- Signals use 1h/4h/1d candles — prefer swing/multi-cycle edge over scalping.

Analyze the context in layers: MACRO → SECTOR → PAIR/ASSET → EXECUTION.

## Layer 1 — Macro context
{macro_summary}

## Layer 2 — Portfolio
{portfolio_summary}

## Layer 3 — Market (crypto + stocks)
{market_summary}

## Layer 4 — Derivatives (funding / OI / basis)
{derivatives_summary}

## Layer 4b — Instrument opportunities (spot vs perp long/short)
{instrument_context}

## Layer 5 — Technical signals (multi-timeframe + ATR)
{signals_summary}

## Layer 5b — Quant reference (do not ignore)
{quant_reference}

## Layer 6 — News (filtered)
{news_summary}

## Layer 7 — Sentiment
{sentiment_summary}

## Layer 8 — Historical learning
{learning_summary}

## Risk rules
- Position sizing scales with conviction:
  - SPOT: amount_pct = % of available balance (nocional)
  - PERPETUAL: amount_pct = % of balance used as MARGIN; notional = margin × leverage
  - conviction 50: max ~{max_position_mid:.0f}% margin/position
  - conviction 70: max ~{max_position_base:.0f}%
  - conviction 90+: max ~{max_position_high:.0f}%
- Taker fee: ~{fee_pct:.2f}% per trade on {exchange} (round-trip ~{fee_round_trip:.2f}%)
- Take-profit must exceed round-trip fees
- Required stop-loss and take-profit on every buy/open
- Crypto whitelist: {whitelist}
- Stocks: disabled for this profile
- Max leverage perpetuals: {max_leverage}x | Max open perps: {max_open_perps}
- Funding cost warning: {funding_warning_pct}% per 8h
- Max {max_trades} trades per day | Min order/margin: {min_order} USDT
- Perp short opens with action "sell" + position_side "short"
- Do NOT default to spot if perp edge exists (funding + momentum aligned)
- If spot long open on same symbol: close spot first OR use perp short as hedge
- Crypto spot and perpetuals only (no stocks)
{mcp_instruction}

## Instrument selection (mandatory justification)
For each proposal explain: why spot vs perpetual, why long vs short, why this leverage.
Summary structure: MACRO → SECTOR → PAIR → INSTRUMENT (spot/perp, side, leverage) → EXECUTION.

## Instructions
Return ONLY valid JSON (no markdown):
{{
  "hold": false,
  "summary": "Brief layered analysis ending with INSTRUMENT choice",
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
      "reasoning": "Why this instrument and direction"
    }}
  ]
}}

Examples:
- Spot long: {{"action":"buy","symbol":"BTC/USDT","instrument_type":"spot","position_side":"long","leverage":1,...}}
- Perp long 10x: {{"action":"buy","symbol":"SOL/USDT","instrument_type":"perpetual","position_side":"long","leverage":10,"amount_pct":10,...}}
- Perp short 5x: {{"action":"sell","symbol":"ETH/USDT","instrument_type":"perpetual","position_side":"short","leverage":5,"amount_pct":15,...}}

If no action recommended, set "hold": true and "proposals": [].
Spot buy = long only. Perpetual short uses action "sell" with position_side "short".
Close perp long with SELL; close perp short with BUY.
"""


PROFILE_PROMPT_INSTRUCTIONS = {
    "extreme": (
        "EXTREME profile — profit maximization with controlled risk. "
        "ONLY perpetuals (long/short). NO spot. "
        "ALL-IN rule: every open and close uses amount_pct=100 (full margin in or full position out). "
        "Maximum ONE open position at a time — if a position is open, only HOLD or close it at 100%. "
        "Leverage 10x when edge is clear (funding + momentum aligned). "
        "Stop-loss MUST be at least 100/leverage % (e.g. 10x → min 10% SL on price). "
        "Take-profit MUST be 0.35%–1.5% on price (realistic for 1–3 cycles at 15 min). "
        "Do NOT open if conviction < 60 or quant conviction < 55 — stay in cash instead. "
        "Do NOT rotate to another asset unless new setup conviction is ≥15 pts above current. "
        "Plan each trade for the cycle cadence: you re-decide only every AI cycle; exits between cycles are SL/TP/trailing only."
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
    instrument_context: str = "",
    quant_reference: str = "",
    market_status: str = "",
    use_mcp: bool = False,
    profile: str = "extreme",
    ai_cycle_interval_minutes: int | None = None,
    risk_monitor_interval_minutes: int | None = None,
) -> str:
    from ainvestor.config import get_profile_ai_cycle_interval, get_settings
    from ainvestor.engine.risk import max_position_pct_for_conviction
    from ainvestor.portfolio.profiles import PROFILE_LABELS, normalize_profile

    prof = normalize_profile(profile)
    ai_cycle = ai_cycle_interval_minutes or get_profile_ai_cycle_interval(prof)
    risk_monitor = risk_monitor_interval_minutes or get_settings().risk_monitor_interval
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
        instrument_context=instrument_context or "No instrument opportunities.",
        signals_summary=signals_summary,
        quant_reference=quant_reference or "No quant reference.",
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
        max_open_perps=deriv.get("max_open_perps", 2),
        funding_warning_pct=deriv.get("funding_cost_warning_pct", 0.05),
        max_trades=risk_config["limits"]["max_trades_per_day"],
        market_status=market_status or "unknown",
        mcp_instruction=mcp_instruction,
        ai_cycle_interval=ai_cycle,
        risk_monitor_interval=risk_monitor,
        cycles_horizon_min=ai_cycle,
        cycles_horizon_max=ai_cycle * 3,
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
    """Cursor agent via SDK or local CLI (IDE session)."""

    def __init__(self):
        self.settings = get_settings()
        self._project_root = Path(__file__).resolve().parent.parent.parent

    async def run_cycle(
        self, prompt: str
    ) -> tuple[CycleDecision, str, str | None, AIUsage]:
        """Returns (decision, raw_response, run_id, token_usage)."""
        errors: list[str] = []
        empty_usage = AIUsage()

        if self.settings.cursor_api_key.strip():
            try:
                logger.info(
                    "AI provider: Cursor SDK (mcp=%s)",
                    self.settings.ai_use_mcp,
                )
                return await self._run_cursor_sdk(prompt)
            except Exception as e:
                logger.warning("Cursor SDK failed: %s", _format_error(e), exc_info=True)
                errors.append(f"SDK: {_format_error(e)}")

        agent_cli = find_cursor_agent_cli()
        if agent_cli:
            try:
                logger.info("AI provider: Cursor CLI")
                return await self._run_cursor_cli(prompt, agent_cli)
            except Exception as e:
                logger.warning("Cursor CLI failed: %s", _format_error(e), exc_info=True)
                errors.append(f"CLI: {_format_error(e)}")

        summary = "No AI API configured"
        if errors:
            summary += f" ({'; '.join(errors)})"
        elif not self.settings.cursor_api_key.strip():
            summary += " (CURSOR_API_KEY not set)"
        logger.warning("All AI providers failed - returning HOLD")
        return CycleDecision(hold=True, summary=summary), "", None, empty_usage

    async def _run_cursor_cli(
        self, prompt: str, agent_cli: Path
    ) -> tuple[CycleDecision, str, str | None, AIUsage]:
        logger.warning(
            "Cursor CLI may bill composer-2.5-fast unless fast=false is supported; "
            "prefer CURSOR_API_KEY + SDK for non-fast billing."
        )
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
        model = self.settings.cursor_model_selection()
        logger.info("Cursor SDK request model=%s", model.to_json())
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            _SDK_EXECUTOR,
            partial(
                _run_cursor_sdk_blocking,
                prompt,
                self.settings.cursor_api_key,
                model,
                self.settings.database_url,
                self._project_root,
                self.settings.ai_use_mcp,
            ),
        )


def _run_cursor_sdk_blocking(
    prompt: str,
    api_key: str,
    model,
    database_url: str,
    project_root: Path,
    use_mcp: bool,
) -> tuple[CycleDecision, str, str | None, AIUsage]:
    return asyncio.run(
        _cursor_sdk_async(
            prompt, api_key, model, database_url, project_root, use_mcp=use_mcp
        )
    )


async def _launch_cursor_bridge(workspace: str):
    """Launch cursor-sdk-bridge, retrying on rare auth-token argv collisions."""
    from cursor_sdk.asyncio import AsyncClient

    last_exc: Exception | None = None
    for attempt in range(1, _MAX_BRIDGE_LAUNCH_ATTEMPTS + 1):
        try:
            return await AsyncClient.launch_bridge(workspace=workspace)
        except Exception as exc:
            last_exc = exc
            if not _is_bridge_token_arg_error(exc):
                raise
            logger.warning(
                "Cursor bridge auth-token argv collision (attempt %s/%s)",
                attempt,
                _MAX_BRIDGE_LAUNCH_ATTEMPTS,
            )
    assert last_exc is not None
    raise last_exc


async def _cursor_sdk_async(
    prompt: str,
    api_key: str,
    model,
    database_url: str,
    project_root: Path,
    *,
    use_mcp: bool = False,
) -> tuple[CycleDecision, str, str | None, AIUsage]:
    from cursor_sdk import AgentOptions, LocalAgentOptions, ModelSelection, SendOptions
    from cursor_sdk.asyncio import AsyncAgent

    if isinstance(model, str):
        model = get_settings().cursor_model_selection()
    elif not isinstance(model, ModelSelection):
        model = ModelSelection.from_value(model)

    mcp_servers = None
    if use_mcp:
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

    client = await _launch_cursor_bridge(workspace=str(project_root))
    try:
        agent = await AsyncAgent.create(options, client=client)
        try:
            send_opts = SendOptions(mcp_servers=mcp_servers) if mcp_servers else SendOptions()
            run = await agent.send(prompt, send_opts)
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
