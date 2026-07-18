from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from ainvestor.utils.datetime_utils import app_now

from sqlalchemy.orm import Session

from ainvestor.config import get_settings
from ainvestor.db.models import AIDecision, DecisionOutcome
from ainvestor.engine.proposal_order import proposal_execution_key
from ainvestor.models.schemas import CycleDecision, TradeProposal
from ainvestor.portfolio.profiles import DEFAULT_PROFILE, normalize_profile

logger = logging.getLogger(__name__)

OUTCOME_PENDING = "pending"
OUTCOME_GOOD = "good"
OUTCOME_BAD = "bad"
OUTCOME_NEUTRAL = "neutral"


class DecisionLearning:
    """Registra decisiones, evalúa resultados y genera contexto para ciclos futuros."""

    def __init__(self, db: Session, profile: str = DEFAULT_PROFILE):
        self.db = db
        self.settings = get_settings()
        self.profile = normalize_profile(profile)

    def record_cycle(
        self,
        cycle_id: str,
        decision: CycleDecision,
        prices: dict[str, float],
        approved_keys: set[tuple[str, str, str, str]],
        rejected: list[tuple[TradeProposal, list[str]]],
        open_positions: list | None = None,
    ) -> None:
        market_avg = self._market_avg_change(prices)
        positions = open_positions or []

        if decision.hold or not decision.proposals:
            record_type = "cycle_hold"
            symbol = None
            position_side = None
            leverage = None
            instrument_type = None
            price_ref = market_avg
            if positions:
                record_type = "hold_with_position"
                pos = positions[0]
                symbol = pos.symbol
                position_side = getattr(pos, "position_side", "long")
                leverage = getattr(pos, "leverage", 1)
                instrument_type = getattr(pos, "instrument_type", "spot")
                price_ref = prices.get(pos.symbol, pos.current_price or market_avg)
            self.db.add(
                DecisionOutcome(
                    cycle_id=cycle_id,
                    profile=self.profile,
                    record_type=record_type,
                    symbol=symbol,
                    action="hold",
                    summary=decision.summary,
                    execution_status="hold",
                    price_at_decision=price_ref,
                    outcome=OUTCOME_PENDING,
                    instrument_type=instrument_type,
                    position_side=position_side,
                    leverage=leverage,
                )
            )

        for proposal in decision.proposals:
            price = prices.get(proposal.symbol, 0.0)
            pkey = proposal_execution_key(proposal)
            if pkey in approved_keys:
                status = "approved"
            else:
                status = "rejected"

            rejection_reasons = ""
            for prop, reasons in rejected:
                if proposal_execution_key(prop) == pkey:
                    rejection_reasons = "; ".join(reasons)
                    break

            self.db.add(
                DecisionOutcome(
                    cycle_id=cycle_id,
                    profile=self.profile,
                    record_type=f"proposal_{proposal.action.value}",
                    symbol=proposal.symbol,
                    action=proposal.action.value,
                    summary=decision.summary,
                    reasoning=proposal.reasoning,
                    conviction=proposal.conviction,
                    amount_pct=proposal.amount_pct,
                    execution_status=status,
                    price_at_decision=price,
                    outcome=OUTCOME_PENDING,
                    outcome_notes=rejection_reasons or None,
                    instrument_type=proposal.instrument_type.value,
                    position_side=proposal.position_side,
                    leverage=proposal.leverage,
                )
            )

        self.db.commit()
        self._record_rotation_outcomes(cycle_id, decision, approved_keys, prices)

    def _record_rotation_outcomes(
        self,
        cycle_id: str,
        decision: CycleDecision,
        approved_keys: set[tuple[str, str, str, str]],
        prices: dict[str, float],
    ) -> None:
        approved = [p for p in decision.proposals if proposal_execution_key(p) in approved_keys]
        closes = [p for p in approved if self._is_perp_close(p)]
        opens = [p for p in approved if self._is_perp_open(p)]
        for close_p in closes:
            for open_p in opens:
                if close_p.symbol == open_p.symbol:
                    continue
                self.db.add(
                    DecisionOutcome(
                        cycle_id=cycle_id,
                        profile=self.profile,
                        record_type="rotation",
                        symbol=open_p.symbol,
                        action="rotate",
                        summary=decision.summary,
                        reasoning=(
                            f"Rotate {close_p.symbol} → {open_p.symbol} "
                            f"(conv {close_p.conviction}→{open_p.conviction})"
                        ),
                        conviction=open_p.conviction,
                        execution_status="approved",
                        price_at_decision=prices.get(open_p.symbol, 0.0),
                        outcome=OUTCOME_PENDING,
                        instrument_type=open_p.instrument_type.value,
                        position_side=open_p.position_side,
                        leverage=open_p.leverage,
                        outcome_notes=close_p.symbol,
                    )
                )
        if closes and opens:
            self.db.commit()

    @staticmethod
    def _is_perp_close(proposal: TradeProposal) -> bool:
        return (
            proposal.instrument_type.value == "perpetual"
            and (
                (proposal.action.value == "sell" and proposal.position_side == "long")
                or (proposal.action.value == "buy" and proposal.position_side == "short")
            )
        )

    @staticmethod
    def _is_perp_open(proposal: TradeProposal) -> bool:
        return (
            proposal.instrument_type.value == "perpetual"
            and (
                (proposal.action.value == "buy" and proposal.position_side == "long")
                or (proposal.action.value == "sell" and proposal.position_side == "short")
            )
        )

    def evaluate_pending(self, prices: dict[str, float]) -> int:
        """Evalúa decisiones pendientes tras la ventana configurada."""
        cutoff = app_now() - timedelta(hours=self.settings.decision_eval_hours)
        pending = (
            self.db.query(DecisionOutcome)
            .filter(
                DecisionOutcome.profile == self.profile,
                DecisionOutcome.outcome == OUTCOME_PENDING,
                DecisionOutcome.created_at <= cutoff,
            )
            .all()
        )

        evaluated = 0
        for record in pending:
            if record.record_type == "cycle_hold":
                outcome, notes, return_pct, eval_price = self._evaluate_hold(record, prices)
            elif record.record_type == "hold_with_position":
                outcome, notes, return_pct, eval_price = self._evaluate_hold_with_position(
                    record, prices
                )
            elif record.record_type == "rotation":
                outcome, notes, return_pct, eval_price = self._evaluate_rotation(record, prices)
            elif record.execution_status == "rejected":
                outcome, notes, return_pct, eval_price = self._evaluate_rejected(record, prices)
            elif record.action == "buy":
                outcome, notes, return_pct, eval_price = self._evaluate_buy(record, prices)
            elif record.action == "sell":
                outcome, notes, return_pct, eval_price = self._evaluate_sell(record, prices)
            else:
                continue

            record.outcome = outcome
            record.outcome_notes = notes
            record.return_pct = return_pct
            record.price_at_evaluation = eval_price
            record.evaluated_at = app_now()
            evaluated += 1

        if evaluated:
            self.db.commit()
            logger.info("Evaluated %d decision outcomes (%s)", evaluated, self.profile)
        return evaluated

    def build_learning_summary(self, limit: int = 15) -> str:
        records = (
            self.db.query(DecisionOutcome)
            .filter(
                DecisionOutcome.profile == self.profile,
                DecisionOutcome.outcome != OUTCOME_PENDING,
            )
            .order_by(DecisionOutcome.evaluated_at.desc())
            .limit(limit)
            .all()
        )

        if not records:
            recent = (
                self.db.query(DecisionOutcome)
                .filter(DecisionOutcome.profile == self.profile)
                .order_by(DecisionOutcome.created_at.desc())
                .limit(5)
                .all()
            )
            if not recent:
                return "No hay historial de decisiones evaluadas aún."
            lines = ["Decisiones recientes (pendientes de evaluación):"]
            for r in recent:
                sym = r.symbol or "cartera"
                lines.append(
                    f"- {r.created_at.strftime('%Y-%m-%d %H:%M')} | {r.action.upper()} {sym} "
                    f"({r.execution_status}): {self._short_text(r.summary or r.reasoning, 120)}"
                )
            return "\n".join(lines)

        good = sum(1 for r in records if r.outcome == OUTCOME_GOOD)
        bad = sum(1 for r in records if r.outcome == OUTCOME_BAD)
        neutral = sum(1 for r in records if r.outcome == OUTCOME_NEUTRAL)

        lines = [
            f"Últimas {len(records)} decisiones evaluadas: {good} acertadas, {bad} erróneas, {neutral} neutras.",
            "Lecciones recientes (úsalo para calibrar convicción y timing):",
        ]
        for r in records[:8]:
            sym = r.symbol or "MERCADO"
            ret = f"{r.return_pct:+.2f}%" if r.return_pct is not None else "N/A"
            tag = {"good": "✓", "bad": "✗", "neutral": "~"}.get(r.outcome, "?")
            detail = r.outcome_notes or r.reasoning or r.summary or ""
            inst = ""
            if r.instrument_type == "perpetual":
                lev = r.leverage or 1
                side = r.position_side or "long"
                inst = f" perp {side} {lev}x"
            lines.append(
                f"- [{tag}] {r.action.upper()} {sym}{inst} ({r.execution_status}): retorno {ret} — "
                f"{self._short_text(detail, 100)}"
            )

        by_instrument: dict[str, list[float]] = {}
        for r in records:
            if r.symbol and r.return_pct is not None and r.execution_status == "approved":
                key = r.symbol
                if r.instrument_type == "perpetual":
                    key = f"{r.symbol} perp {r.position_side or 'long'} {r.leverage or 1}x"
                by_instrument.setdefault(key, []).append(r.return_pct)
        if by_instrument:
            lines.append("Rendimiento medio por instrumento (operaciones aprobadas):")
            for key, returns in sorted(by_instrument.items()):
                avg = sum(returns) / len(returns)
                lines.append(f"  {key}: {avg:+.2f}% ({len(returns)} ops)")

        return "\n".join(lines)

    def get_stats(self) -> dict:
        base = self.db.query(DecisionOutcome).filter(
            DecisionOutcome.profile == self.profile
        )
        total = base.count()
        pending = base.filter(DecisionOutcome.outcome == OUTCOME_PENDING).count()
        good = base.filter(DecisionOutcome.outcome == OUTCOME_GOOD).count()
        bad = base.filter(DecisionOutcome.outcome == OUTCOME_BAD).count()
        neutral = base.filter(DecisionOutcome.outcome == OUTCOME_NEUTRAL).count()
        evaluated = good + bad + neutral
        return {
            "profile": self.profile,
            "total_records": total,
            "pending_evaluation": pending,
            "evaluated": evaluated,
            "good": good,
            "bad": bad,
            "neutral": neutral,
            "accuracy_pct": round(good / evaluated * 100, 1) if evaluated else None,
            "good_pct": round(good / evaluated * 100, 1) if evaluated else None,
            "neutral_pct": round(neutral / evaluated * 100, 1) if evaluated else None,
            "bad_pct": round(bad / evaluated * 100, 1) if evaluated else None,
        }

    def _evaluate_hold_with_position(
        self, record: DecisionOutcome, prices: dict[str, float]
    ) -> tuple[str, str, float, float]:
        if not record.symbol:
            return self._evaluate_hold(record, prices)

        eval_price = prices.get(record.symbol, record.price_at_decision)
        if record.price_at_decision <= 0:
            return OUTCOME_NEUTRAL, "Sin precio de referencia", 0.0, eval_price

        return_pct = ((eval_price - record.price_at_decision) / record.price_at_decision) * 100
        if record.position_side == "short":
            return_pct = -return_pct

        if return_pct >= 1.5:
            return OUTCOME_GOOD, "Hold con posición: precio a favor", return_pct, eval_price
        if return_pct <= -2.0:
            return OUTCOME_BAD, "Hold con posición: precio en contra", return_pct, eval_price
        return OUTCOME_NEUTRAL, "Hold con posición: movimiento lateral", return_pct, eval_price

    def _evaluate_rotation(
        self, record: DecisionOutcome, prices: dict[str, float]
    ) -> tuple[str, str, float, float]:
        if not record.symbol:
            return OUTCOME_NEUTRAL, "Rotación sin símbolo destino", 0.0, 0.0

        new_price = prices.get(record.symbol, record.price_at_decision)
        old_symbol = record.outcome_notes or ""
        old_price = prices.get(old_symbol, 0.0) if old_symbol else 0.0

        if record.price_at_decision <= 0 or old_price <= 0:
            return OUTCOME_NEUTRAL, "Rotación sin precios de referencia", 0.0, new_price

        new_ret = ((new_price - record.price_at_decision) / record.price_at_decision) * 100
        if record.position_side == "short":
            new_ret = -new_ret

        if new_ret >= 1.0:
            return OUTCOME_GOOD, f"Rotación acertada hacia {record.symbol}", new_ret, new_price
        if new_ret <= -2.0:
            return OUTCOME_BAD, f"Rotación fallida hacia {record.symbol}", new_ret, new_price
        return OUTCOME_NEUTRAL, f"Rotación {record.symbol} sin edge claro", new_ret, new_price

    def _evaluate_hold(
        self, record: DecisionOutcome, prices: dict[str, float]
    ) -> tuple[str, str, float, float]:
        eval_price = self._market_avg_change(prices)
        if record.price_at_decision <= 0:
            return OUTCOME_NEUTRAL, "Sin precio de referencia", 0.0, eval_price

        return_pct = ((eval_price - record.price_at_decision) / record.price_at_decision) * 100
        if return_pct <= -2:
            return OUTCOME_GOOD, "Hold evitó caída del mercado", return_pct, eval_price
        if return_pct >= 3:
            return OUTCOME_BAD, "Hold perdió rally del mercado", return_pct, eval_price
        return OUTCOME_NEUTRAL, "Mercado lateral tras hold", return_pct, eval_price

    def _evaluate_rejected(
        self, record: DecisionOutcome, prices: dict[str, float]
    ) -> tuple[str, str, float, float]:
        if not record.symbol:
            return OUTCOME_NEUTRAL, "Sin símbolo", 0.0, 0.0

        eval_price = prices.get(record.symbol, record.price_at_decision)
        if record.price_at_decision <= 0:
            return OUTCOME_NEUTRAL, "Sin precio de referencia", 0.0, eval_price

        return_pct = ((eval_price - record.price_at_decision) / record.price_at_decision) * 100
        if record.action == "buy":
            if return_pct >= 3:
                return OUTCOME_BAD, "Rechazo incorrecto: oportunidad perdida", return_pct, eval_price
            if return_pct <= -3:
                return OUTCOME_GOOD, "Rechazo acertado: evitó caída", return_pct, eval_price
        return OUTCOME_NEUTRAL, "Rechazo sin impacto claro", return_pct, eval_price

    def _evaluate_buy(
        self, record: DecisionOutcome, prices: dict[str, float]
    ) -> tuple[str, str, float, float]:
        if not record.symbol:
            return OUTCOME_NEUTRAL, "Sin símbolo", 0.0, 0.0

        eval_price = prices.get(record.symbol, record.price_at_decision)
        if record.price_at_decision <= 0:
            return OUTCOME_NEUTRAL, "Sin precio de referencia", 0.0, eval_price

        return_pct = ((eval_price - record.price_at_decision) / record.price_at_decision) * 100
        is_short = (
            record.instrument_type == "perpetual" and record.position_side == "short"
        )
        if is_short:
            return_pct = -return_pct
        if record.execution_status == "approved":
            if return_pct >= 1:
                return OUTCOME_GOOD, "Compra con retorno positivo", return_pct, eval_price
            if return_pct <= -2:
                return OUTCOME_BAD, "Compra con pérdida significativa", return_pct, eval_price
            return OUTCOME_NEUTRAL, "Compra con movimiento lateral", return_pct, eval_price

        return self._evaluate_rejected(record, prices)

    def _evaluate_sell(
        self, record: DecisionOutcome, prices: dict[str, float]
    ) -> tuple[str, str, float, float]:
        if not record.symbol:
            return OUTCOME_NEUTRAL, "Sin símbolo", 0.0, 0.0

        eval_price = prices.get(record.symbol, record.price_at_decision)
        if record.price_at_decision <= 0:
            return OUTCOME_NEUTRAL, "Sin precio de referencia", 0.0, eval_price

        return_pct = ((eval_price - record.price_at_decision) / record.price_at_decision) * 100
        is_short = (
            record.instrument_type == "perpetual" and record.position_side == "short"
        )
        if is_short:
            return_pct = -return_pct
        if return_pct <= -1:
            return OUTCOME_GOOD, "Venta antes de caída", return_pct, eval_price
        if return_pct >= 2:
            return OUTCOME_BAD, "Venta prematura antes de subida", return_pct, eval_price
        return OUTCOME_NEUTRAL, "Venta con movimiento lateral", return_pct, eval_price

    @staticmethod
    def _market_avg_change(prices: dict[str, float]) -> float:
        if not prices:
            return 0.0
        return sum(prices.values()) / len(prices)

    @staticmethod
    def _short_text(text: str | None, max_len: int) -> str:
        if not text:
            return ""
        cleaned = text.replace("\n", " ").strip()
        if len(cleaned) <= max_len:
            return cleaned
        return cleaned[: max_len - 3] + "..."

    def backfill_from_decisions(self) -> int:
        """Migra decisiones antiguas sin registros de aprendizaje."""
        existing_cycles = {
            r.cycle_id
            for r in self.db.query(DecisionOutcome.cycle_id)
            .filter(DecisionOutcome.profile == self.profile)
            .distinct()
            .all()
        }
        decisions = (
            self.db.query(AIDecision)
            .filter(AIDecision.profile == self.profile)
            .order_by(AIDecision.created_at.asc())
            .all()
        )
        created = 0
        for d in decisions:
            if d.cycle_id in existing_cycles:
                continue
            hold = d.hold if d.hold is not None else (d.approved_count == 0 and d.rejected_count == 0)
            summary = d.summary or ""
            proposals: list[dict] = []
            if d.proposals_json:
                try:
                    proposals = json.loads(d.proposals_json)
                except json.JSONDecodeError:
                    pass

            if hold or not proposals:
                self.db.add(
                    DecisionOutcome(
                        cycle_id=d.cycle_id,
                        profile=self.profile,
                        record_type="cycle_hold",
                        action="hold",
                        summary=summary,
                        execution_status="hold",
                        price_at_decision=0.0,
                        outcome=OUTCOME_PENDING,
                        created_at=d.created_at,
                    )
                )
                created += 1
            else:
                for p in proposals:
                    self.db.add(
                        DecisionOutcome(
                            cycle_id=d.cycle_id,
                            profile=self.profile,
                            record_type=f"proposal_{p.get('action', 'hold')}",
                            symbol=p.get("symbol"),
                            action=p.get("action", "hold"),
                            summary=summary,
                            reasoning=p.get("reasoning"),
                            conviction=p.get("conviction"),
                            amount_pct=p.get("amount_pct"),
                            execution_status="approved" if d.approved_count else "rejected",
                            price_at_decision=0.0,
                            outcome=OUTCOME_PENDING,
                            created_at=d.created_at,
                            instrument_type=p.get("instrument_type"),
                            position_side=p.get("position_side"),
                            leverage=p.get("leverage"),
                        )
                    )
                    created += 1
            existing_cycles.add(d.cycle_id)

        if created:
            self.db.commit()
        return created
