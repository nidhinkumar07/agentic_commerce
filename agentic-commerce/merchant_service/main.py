"""
merchant_service/main.py

The merchant HTTP API. Routes here contain NO gate logic themselves — all
approval/decline decisions come from gate.evaluate_purchase(). Routes are
responsible for: validating input, calling the gate, persisting the
resulting state, and logging every gate decision to audit_log (even on
approval) before any side effect (like a Razorpay call) happens.
"""

import sys
import os
import sqlite3
from typing import Optional

from dotenv import load_dotenv
load_dotenv()  # loaded before helpers.py reads DATABASE_PATH

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "db"))
sys.path.insert(0, os.path.dirname(__file__))
import helpers as db
import gate
import razorpay_client

from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel

app = FastAPI(title="Agentic Commerce — Merchant Service", version="0.1.0")

MAX_QUOTE_QUANTITY = 100  # sanity bound; prevents absurd/abusive quote requests


# ---------------------------------------------------------------------------
# related products (rule-based, static -- see db/seed.py RELATED_PRODUCTS)
# ---------------------------------------------------------------------------
RELATED_PRODUCTS = {
    "prod_001": ["prod_002", "prod_005"],
    "prod_002": ["prod_001", "prod_003"],
    "prod_003": ["prod_002"],
    "prod_004": [],
    "prod_005": ["prod_001"],
}


# ---------------------------------------------------------------------------
# request/response models
# ---------------------------------------------------------------------------
class QuoteRequest(BaseModel):
    product_id: str
    quantity: int = 1


class PurchaseRequest(BaseModel):
    quote_id: str
    agent_id: str
    simulate_razorpay_failure: bool = False  # for the live infra-failure demo only


# ---------------------------------------------------------------------------
# catalog
# ---------------------------------------------------------------------------
@app.get("/catalog")
def get_catalog():
    conn = db.get_connection()
    try:
        rows = conn.execute("SELECT * FROM products ORDER BY product_id").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.get("/catalog/{product_id}")
def get_catalog_item(product_id: str):
    product = db.get_product(product_id)
    if product is None:
        raise HTTPException(status_code=404, detail=f"product '{product_id}' not found")
    return product


@app.get("/catalog/{product_id}/related")
def get_related_products(product_id: str):
    """
    Rule-based, static cross-sell pairing -- NOT on the money path, not
    AI-driven, purely informational. Returns full product dicts for
    whatever's in RELATED_PRODUCTS, silently skipping any that no longer
    exist in the catalog.
    """
    if db.get_product(product_id) is None:
        raise HTTPException(status_code=404, detail=f"product '{product_id}' not found")
    related_ids = RELATED_PRODUCTS.get(product_id, [])
    related = [db.get_product(pid) for pid in related_ids]
    return [p for p in related if p is not None]


# ---------------------------------------------------------------------------
# quote
# ---------------------------------------------------------------------------
@app.post("/quote")
def create_quote(req: QuoteRequest):
    product = db.get_product(req.product_id)
    if product is None:
        raise HTTPException(status_code=404, detail=f"product '{req.product_id}' not found")
    if req.quantity < 1:
        raise HTTPException(status_code=400, detail="quantity must be >= 1")
    if req.quantity > MAX_QUOTE_QUANTITY:
        raise HTTPException(
            status_code=400,
            detail=f"quantity {req.quantity} exceeds max allowed quantity of {MAX_QUOTE_QUANTITY}",
        )
    if product["stock"] < req.quantity:
        # Validated at quote time; re-checked again at purchase time since
        # stock can change between the two calls.
        raise HTTPException(
            status_code=409,
            detail=f"insufficient stock for '{req.product_id}': have {product['stock']}, requested {req.quantity}",
        )
    quote = db.create_quote(req.product_id, req.quantity)
    return quote


# ---------------------------------------------------------------------------
# purchase
# ---------------------------------------------------------------------------
@app.post("/purchase")
def purchase(req: PurchaseRequest, x_agent_secret: Optional[str] = Header(None)):
    # --- Authentication: static per-agent shared secret ---
    # Without this, any caller who knows an agent_id (they're not secret --
    # they're returned by GET /catalog-adjacent flows and printed by the
    # CLI) could impersonate agent_high and spend against its higher caps.
    # This is deliberately checked AFTER confirming the agent exists (so a
    # 404 for a genuinely unknown agent isn't disguised as a 401), but
    # BEFORE any gate evaluation or DB write.
    agent = db.get_agent(req.agent_id)
    if agent is None:
        # Required test case: unknown agent_id must not crash the service.
        raise HTTPException(status_code=404, detail=f"agent '{req.agent_id}' not found")
    if not x_agent_secret or x_agent_secret != agent["shared_secret"]:
        raise HTTPException(status_code=401, detail="missing or invalid X-Agent-Secret header")

    # --- Critical section: gate evaluation through payment_pending ---
    # Opened with BEGIN IMMEDIATE (an exclusive write lock, acquired
    # immediately rather than on first write) to close a real race: the
    # daily-spend-cap check reads today's spend, then writes a new
    # transaction that counts toward it. Without serializing read-check-
    # write, two near-simultaneous requests for the same agent can both
    # read a daily spend that's still under the cap, then both write,
    # jointly exceeding the cap that neither individually violated. SQLite
    # has no row-level locking, so this necessarily serializes ALL writes
    # database-wide for the duration of this block -- acceptable at this
    # demo's scale, and documented as a known scalability limitation in
    # the README. The block is kept as short as possible: it ends at
    # payment_pending, BEFORE the Razorpay network call, so external
    # payment latency never holds this lock.
    conn = db.get_connection(autocommit=True)
    try:
        conn.execute("BEGIN IMMEDIATE")

        quote = db.get_quote(req.quote_id, conn=conn)
        if quote is None:
            conn.execute("ROLLBACK")
            raise HTTPException(status_code=404, detail=f"quote '{req.quote_id}' not found")

        result = gate.evaluate_purchase(quote, agent, conn=conn)

        if result.decision == "idempotent_existing":
            conn.execute("ROLLBACK")
            return {"idempotent": True, "transaction": result.existing_transaction}

        if result.decision == "declined":
            txn, created = db.try_create_transaction(
                quote_id=req.quote_id,
                agent_id=req.agent_id,
                amount=quote["total_price"] if quote else 0,
                status="declined",
                conn=conn,
            )
            if not created:
                conn.execute("ROLLBACK")
                return {"idempotent": True, "transaction": txn}
            txn = db.update_transaction_status(
                txn["txn_id"], "declined", decline_reason=result.reason, conn=conn
            )
            db.log_audit(
                txn["txn_id"],
                "gate_decision",
                {"decision": "declined", "reason": result.reason, "checked_amount": quote["total_price"] if quote else None},
                conn=conn,
            )
            conn.execute("COMMIT")
            return {"idempotent": False, "transaction": txn}

        # decision == "approved"
        db.mark_quote_consumed(req.quote_id, conn=conn)
        txn, created = db.try_create_transaction(
            quote_id=req.quote_id,
            agent_id=req.agent_id,
            amount=quote["total_price"],
            status="approved",
            conn=conn,
        )
        if not created:
            conn.execute("ROLLBACK")
            return {"idempotent": True, "transaction": txn}

        txn = db.update_transaction_status(txn["txn_id"], "approved", conn=conn)
        db.log_audit(
            txn["txn_id"],
            "gate_decision",
            {"decision": "approved", "reason": result.reason, "amount": quote["total_price"]},
            conn=conn,
        )

        # Write payment_pending and COMMIT before calling Razorpay, so a
        # mid-call crash leaves a real, queryable record instead of
        # silence -- and so the lock is released before the network call.
        txn = db.update_transaction_status(txn["txn_id"], "payment_pending", conn=conn)
        db.log_audit(txn["txn_id"], "payment_pending", {"amount": quote["total_price"]}, conn=conn)

        product_for_currency = db.get_product(quote["product_id"], conn=conn)
        conn.execute("COMMIT")
    except HTTPException:
        raise
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise
    finally:
        conn.close()

    # --- Payment path (lock already released) ---
    currency = product_for_currency["currency"] if product_for_currency else "INR"

    success, order_id_or_error = razorpay_client.create_order(
        amount=quote["total_price"],
        currency=currency,
        receipt_id=txn["txn_id"],
        simulate_failure=req.simulate_razorpay_failure,
    )

    if success:
        try:
            # Stock decrement ONLY happens here, at the moment of
            # completion — never at 'approved' or 'payment_pending' — so a
            # failed Razorpay call never leaves phantom stock reduction.
            db.decrement_stock(quote["product_id"], quantity=quote["quantity"])
        except db.StockRaceLostError as e:
            # Payment succeeded but stock could not be reserved: another
            # transaction's completion consumed the last unit(s) between
            # this transaction's purchase-time stock check and now. This
            # is a genuine compensating-action gap for a demo of this
            # scope — a real system would issue a refund/void here. We
            # fail loudly and log full detail rather than silently
            # reporting 'completed' with no stock actually reserved. See
            # README "Known Limitations".
            txn = db.update_transaction_status(
                txn["txn_id"], "failed", decline_reason="stock_race_lost"
            )
            db.log_audit(
                txn["txn_id"],
                "stock_race_lost",
                {
                    "error": str(e),
                    "razorpay_order_id": order_id_or_error,
                    "warning": (
                        "A Razorpay order WAS already created before this failure. "
                        "Manual reconciliation/refund is required in a real system."
                    ),
                },
            )
            return {"idempotent": False, "transaction": txn}

        txn = db.update_transaction_status(
            txn["txn_id"], "completed", razorpay_order_id=order_id_or_error
        )
        db.log_audit(
            txn["txn_id"],
            "razorpay_order_created",
            {"razorpay_order_id": order_id_or_error},
        )
    else:
        # razorpay_client.create_order() returns errors formatted as
        # "<category>: <detail>" (see its docstring for the full category
        # list). Extracting the category into decline_reason means a
        # 'failed' transaction is filterable/queryable by WHY it failed
        # (network vs bad request vs TLS vs simulated) via
        # GET /transactions?status=failed, not just an opaque blob you'd
        # have to open the audit log to interpret.
        failure_category = order_id_or_error.split(":", 1)[0].strip()
        txn = db.update_transaction_status(txn["txn_id"], "failed", decline_reason=failure_category)
        db.log_audit(
            txn["txn_id"],
            "razorpay_call_failed",
            {"error": order_id_or_error, "category": failure_category},
        )
        # No auto-retry, stock untouched, no double-charge risk: the
        # transaction is left in a terminal 'failed' state with a full
        # audit trail showing exactly where it broke.

    return {
        "idempotent": False,
        "transaction": txn,
    }


# ---------------------------------------------------------------------------
# audit / transactions
# ---------------------------------------------------------------------------
@app.get("/audit/{txn_id}")
def get_audit(txn_id: str):
    txn = db.get_transaction(txn_id)
    if txn is None:
        raise HTTPException(status_code=404, detail=f"transaction '{txn_id}' not found")
    return db.get_audit_trail(txn_id)


@app.get("/transactions")
def get_transactions(status: Optional[str] = None):
    return db.list_transactions(status=status)


@app.get("/metrics")
def get_metrics():
    """
    Real, computed-from-actual-data business metrics -- not vanity
    numbers. Every figure here is a direct aggregation over the
    transactions table; nothing is estimated or simulated. Intended to
    make the "growth" half of this project's scope legible: GMV,
    completion rate, and where money is being lost (declines by reason),
    which is the concrete question a merchant actually cares about.
    """
    all_txns = db.list_transactions()

    completed = [t for t in all_txns if t["status"] == "completed"]
    declined = [t for t in all_txns if t["status"] == "declined"]
    failed = [t for t in all_txns if t["status"] == "failed"]
    in_flight = [t for t in all_txns if t["status"] in ("pending_gate", "approved", "payment_pending")]

    gmv_completed = sum(t["amount"] for t in completed)
    gmv_declined = sum(t["amount"] for t in declined)
    gmv_failed = sum(t["amount"] for t in failed)
    gmv_attempted = gmv_completed + gmv_declined + gmv_failed

    decline_reasons = {}
    for t in declined:
        reason = t.get("decline_reason") or "unspecified"
        decline_reasons[reason] = decline_reasons.get(reason, 0) + 1

    failure_reasons = {}
    for t in failed:
        reason = t.get("decline_reason") or "unspecified"
        failure_reasons[reason] = failure_reasons.get(reason, 0) + 1

    total_resolved = len(completed) + len(declined) + len(failed)

    return {
        "total_transactions": len(all_txns),
        "completed_count": len(completed),
        "declined_count": len(declined),
        "failed_count": len(failed),
        "in_flight_count": len(in_flight),
        "gmv_completed": round(gmv_completed, 2),
        "gmv_declined": round(gmv_declined, 2),
        "gmv_failed": round(gmv_failed, 2),
        "gmv_attempted": round(gmv_attempted, 2),
        "completion_rate": round(len(completed) / total_resolved, 4) if total_resolved else None,
        "avg_order_value_completed": round(gmv_completed / len(completed), 2) if completed else None,
        "decline_reasons": decline_reasons,
        "failure_reasons": failure_reasons,
    }
