"""Two-stage owner-requested market synthesis: public AI research then risk-gated tickets.

Never send portfolio positions, balances, account types, or private records to
the model. All caller data remains ephemeral. No broker calls or order writes.
"""
from __future__ import annotations

from typing import Any
from pydantic import Field, model_validator

from app.models import StrictModel
from app.trade_synthesis import Candidate, TradeSynthesisRequest, synthesize
from app.trade_research import ResearchRequest, propose_research


class AutoTradeRequest(StrictModel):
    trade: TradeSynthesisRequest
    research: ResearchRequest

    @model_validator(mode="after")
    def match_universe(self):
        quoted = {q.ticker for q in self.trade.quotes}
        if not set(self.research.universe) <= quoted:
            raise ValueError("All research tickers must have corresponding quoted instruments")
        return self


async def run_auto_synthesis(store: Any, request: AutoTradeRequest) -> dict:
    """Abstain before model spend unless complete signals show an eligible risk-on situation.

    Portfolio/private fields are never added to research packet.
    """
    # Intentional: the upstream candidate descriptions are not trusted in auto mode.
    base = request.trade.model_copy(update={"candidates": []})
    preliminary = synthesize(base)
    if preliminary["status"] == "PROPOSAL_ONLY":
        return {**preliminary,
                "research": {"status": "SKIPPED", "reason": "Owner hard exit or rule-based trim already triggered"},
                "automatic_execution": False}
    if preliminary["regime"] != "risk_on":
        return {**preliminary,
                "research": {"status": "SKIPPED", "reason": "Missing data or risk-off/mixed regime; no model call"},
                "automatic_execution": False}

    # AI may propose a shortlist but cannot create a price, quantity or risk limit.
    research = await propose_research(store, request.research)
    if research.get("status") != "RESEARCH_ONLY":
        return {**preliminary,
                "blocked_by": preliminary.get("blocked_by", []) + [
                    "AI candidate research not available, no discretionary BUY"
                ],
                "research": research,
                "automatic_execution": False}

    ideas = [
        Candidate(ticker=item["ticker"], reason=item["thesis"],
                  thesis_invalidated_if=item["what_would_change_mind"])
        for item in research["ideas"] if item["bias"] == "consider_buy"
    ]
    # No BUY from watch or avoid labels. Research must cite retained evidence.
    used_ids = list(dict.fromkeys(
        request.trade.evidence_ids +
        [id_ for idea in research["ideas"] for id_ in idea["source_ids"]]
    ))[:20]
    updated = request.trade.model_copy(update={"candidates": ideas, "evidence_ids": used_ids})
    result = synthesize(updated)
    return {**result, "research": {
        "status": research["status"],
        "state": research["state"],
        "state_rationale": research["state_rationale"],
        "gaps": research["gaps"],
        "idea_count": len(research["ideas"]),
        "candidate_symbols": [idea.ticker for idea in ideas],
        "model": research.get("model"),
    }, "automatic_execution": False}
