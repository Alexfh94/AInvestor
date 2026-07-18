from __future__ import annotations

from ainvestor.models.schemas import (
    DecisionAction,
    InstrumentType,
    PortfolioSnapshot,
    TradeProposal,
)


def proposal_execution_key(proposal: TradeProposal) -> tuple[str, str, str, str]:
    """Stable identity for matching proposals across risk/learning."""
    return (
        proposal.symbol,
        proposal.action.value,
        proposal.position_side,
        proposal.instrument_type.value,
    )


def is_close_proposal(proposal: TradeProposal, snapshot: PortfolioSnapshot) -> bool:
    """True when the proposal closes an existing position on snapshot."""
    for pos in snapshot.positions:
        if pos.symbol != proposal.symbol:
            continue
        inst = proposal.instrument_type
        pos_inst = getattr(pos, "instrument_type", "spot") or "spot"

        if inst == InstrumentType.PERPETUAL:
            if pos_inst != "perpetual":
                continue
            side = getattr(pos, "position_side", "long") or "long"
            if side == "long" and proposal.action == DecisionAction.SELL:
                # SELL + short on an open long is a flip (open short), not the close leg.
                if proposal.position_side == "short":
                    return False
                return True
            if side == "short" and proposal.action == DecisionAction.BUY:
                if proposal.position_side == "long":
                    return False
                return True
            continue

        if proposal.action == DecisionAction.SELL:
            return True
    return False


def sort_proposals_for_execution(
    proposals: list[TradeProposal],
    snapshot: PortfolioSnapshot,
) -> list[TradeProposal]:
    """Close legs first so opens in the same cycle see updated cash/positions."""
    indexed = list(enumerate(proposals))
    indexed.sort(
        key=lambda item: (
            0 if is_close_proposal(item[1], snapshot) else 1,
            item[0],
        )
    )
    return [p for _, p in indexed]
