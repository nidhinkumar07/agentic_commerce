"""
merchant_service/gate.py

===============================================================================
DESIGN DECISION: THIS FILE CONTAINS NO AI / LLM CALLS. THIS IS DELIBERATE.
===============================================================================
The gate is the only place in this system that decides whether real money
moves. Purchase approval/decline must be deterministic, auditable, and
reproducible — the same (quote, agent, DB state) input must always produce
the same decision. An LLM call here would make approvals probabilistic and
unauditable, which is unacceptable for a money-approval path.

Everywhere an AI model IS used in this project (buyer_agent's optional
natural-language goal parsing), it is isolated in its own module, run
BEFORE the gate is ever called, and has zero ability to influence the
gate's decision. See README.md § "AI Judgment: Where We Did and Didn't Use
a Model" for the full explanation.
===============================================================================

evaluate_purchase() runs these checks in order and returns as soon as one
fails:
  a. Idempotency check   -- existing transaction for this quote_id? return it, skip re-evaluation
  b. Quote validity      -- not expired, not already consumed
  c. Per-transaction cap -- total_price <= agent.max_authorized_amount
  d. Daily cap           -- get_agent_daily_spend(agent) + total_price <= agent.daily_spend_cap
  e. Stock check          -- re-checked at purchase time (can differ from quote time)
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "db"))
import helpers as db


class GateResult:
    """Plain result object: decision is one of 'idempotent_existing', 'approved', 'declined'."""

    def __init__(self, decision: str, reason: str, existing_transaction: dict = None):
        self.decision = decision
        self.reason = reason
        self.existing_transaction = existing_transaction

    def to_dict(self):
        return {
            "decision": self.decision,
            "reason": self.reason,
            "existing_transaction_id": (
                self.existing_transaction["txn_id"] if self.existing_transaction else None
            ),
        }


def evaluate_purchase(quote: dict, agent: dict, conn=None) -> GateResult:
    """
    Pure decision function. Does not write to the DB itself (callers are
    responsible for creating/updating the transaction row and logging the
    audit trail) — this keeps the gate side-effect-free and easy to unit
    test in isolation.
    """
    quote_id = quote["quote_id"] if quote else None

    # (a) Idempotency check
    if quote_id:
        existing = db.get_transaction_by_quote_id(quote_id, conn=conn)
        if existing is not None:
            return GateResult(
                decision="idempotent_existing",
                reason="transaction_already_exists_for_quote",
                existing_transaction=existing,
            )

    # (b) Quote validity
    if quote is None:
        return GateResult(decision="declined", reason="quote_not_found")

    valid, invalid_reason = db.is_quote_valid(quote)
    if not valid:
        return GateResult(decision="declined", reason=invalid_reason)

    if agent is None:
        return GateResult(decision="declined", reason="agent_not_found")

    # (c) Per-transaction cap
    if quote["total_price"] > agent["max_authorized_amount"]:
        return GateResult(decision="declined", reason="exceeds_per_transaction_cap")

    # (d) Daily cap
    daily_spend = db.get_agent_daily_spend(agent["agent_id"], conn=conn)
    if daily_spend + quote["total_price"] > agent["daily_spend_cap"]:
        return GateResult(decision="declined", reason="exceeds_daily_spend_cap")

    # (e) Stock check (re-checked at purchase time — can differ from quote time)
    product = db.get_product(quote["product_id"], conn=conn)
    if product is None:
        return GateResult(decision="declined", reason="product_not_found")
    if product["stock"] < quote["quantity"]:
        return GateResult(decision="declined", reason="insufficient_stock")

    return GateResult(decision="approved", reason="all_checks_passed")
