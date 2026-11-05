"""Private immutable trade-decision receipt; no portfolio snapshot or broker orders.

This function is called only AFTER owner authentication and deterministic gating.
The database validates genuine collection-time evidence again, transactionally.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone


ALLOWED_TICKET_KEYS = frozenset((
    "symbol", "action", "shares", "limit_price", "order_type",
    "trigger", "expires_at", "estimated_notional", "reason",
    "stop_price", "target_price", "risk_at_proposed_stop",
    "thesis_invalidated_if", "tax_review", "evidence_ids",
))


def prepare_decision_receipt(result: dict, *, method: str) -> dict:
    if method not in ("rule_based", "ai_researched"):
        raise ValueError("Invalid decision method")
    if result["status"] not in ("NO_TRADE", "PROPOSAL_ONLY"):
        raise ValueError("Invalid decision state")
    tickets = []
    for ticket in result.get("tickets") or []:
        if not isinstance(ticket, dict):
            raise ValueError("Invalid ticket")
        sanitized = {k: value for k, value in ticket.items() if k in ALLOWED_TICKET_KEYS}
        if not sanitized.get("symbol") or sanitized.get("action") not in ("BUY", "SELL", "TRIM"):
            raise ValueError("Invalid proposed trade")
        tickets.append(sanitized)
    if result["status"] == "NO_TRADE" and tickets:
        raise ValueError("NO_TRADE must not contain tickets")
    payload = {
        "as_of": result["as_of"], "status": result["status"],
        "regime": result["regime"], "method": method,
        "tickets": tickets, "evidence_ids": result.get("evidence_ids") or [],
        "blocked_reasons": (result.get("blocked_by") or [])[:25],
        "input_provenance": result.get("source_attestation", "unknown"),
        "model_label": result.get("research", {}).get("model") if method == "ai_researched" else None,
    }
    # Never store the full request, any NAV, cash, positions, account ID, token,
    # model prompts, or private account attributes.
    if not payload["evidence_ids"]:
        raise ValueError("No evidence IDs available for auditable decision")
    key = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False).encode()).hexdigest()
    return {"payload": payload, "p_key": key}


async def persist_trade_result(store, result: dict, *, method: str) -> dict:
    receipt = prepare_decision_receipt(result, method=method)
    record = await store.rpc("market_record_decision_v1", receipt)
    if not record.get("stored_privately") or record.get("orders_placed") is not False:
        raise ValueError("Decision audit was not committed or execution state is ambiguous")
    return {**result, "audit": record, "automatic_execution": False}
