"""
db/helpers.py

The ONLY module in this project that should contain raw SQL. Every other
part of the system (merchant_service, buyer_agent, dashboard) talks to
SQLite exclusively through these functions.

Design notes:
- Functions take/return plain dicts, never raw sqlite3.Row/cursor objects.
- Every write to `transactions` or `quotes.consumed` happens inside a single
  DB transaction (one connection, one commit) — never split across multiple
  commits, so a crash mid-operation can't leave inconsistent state.
- Timestamps are ISO 8601 UTC strings everywhere: quotes, transactions, and
  audit_log all use the same format so the audit trail is consistent.
"""

import json
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

DB_PATH = os.environ.get(
    "DATABASE_PATH", os.path.join(os.path.dirname(__file__), "merchant.db")
)

QUOTE_EXPIRY_MINUTES = 2


class StockRaceLostError(Exception):
    """
    Raised by decrement_stock() when the UPDATE affects 0 rows -- meaning
    another completed transaction consumed the remaining stock between
    this transaction's purchase-time stock check and this decrement call.
    Callers MUST catch this and fail the transaction to 'failed' rather
    than letting it silently report 'completed' with no actual stock
    reserved.
    """
    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_connection(db_path: str = None, autocommit: bool = False) -> sqlite3.Connection:
    """
    Every call gets its own connection. Callers are responsible for
    closing it.

    autocommit=True sets isolation_level=None, putting the connection in
    Python's sqlite3 "autocommit" mode so explicit BEGIN IMMEDIATE / COMMIT
    / ROLLBACK statements behave predictably (Python's sqlite3 module
    otherwise manages implicit transactions itself, which conflicts with
    manually issued BEGIN statements). Used by callers that need to
    serialize a read-check-write critical section -- see
    merchant_service/main.py's use of BEGIN IMMEDIATE around the gate
    evaluation + transaction creation to close the daily-spend-cap race.
    """
    conn = sqlite3.connect(db_path or DB_PATH)
    if autocommit:
        conn.isolation_level = None
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def _row_to_dict(row):
    return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# products
# ---------------------------------------------------------------------------
def get_product(product_id: str, conn: sqlite3.Connection = None) -> dict | None:
    own_conn = conn is None
    conn = conn or get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM products WHERE product_id = ?", (product_id,)
        ).fetchone()
        return _row_to_dict(row)
    finally:
        if own_conn:
            conn.close()


def decrement_stock(product_id: str, quantity: int = 1, conn: sqlite3.Connection = None) -> dict:
    """
    Only ever called at the moment a transaction becomes `completed`.
    Never called on `approved` or `payment_pending`, so a failed Razorpay
    call never leaves phantom stock reduction.

    Raises StockRaceLostError if the UPDATE affects 0 rows -- meaning
    stock was insufficient at the moment of commit (the product's stock
    was consumed by another completed transaction between this
    transaction's purchase-time stock check and now). Callers MUST catch
    this and fail the transaction rather than reporting a false
    'completed' with no stock actually reserved.
    """
    own_conn = conn is None
    conn = conn or get_connection()
    try:
        cur = conn.execute(
            "UPDATE products SET stock = stock - ? WHERE product_id = ? AND stock >= ?",
            (quantity, product_id, quantity),
        )
        if cur.rowcount == 0:
            if own_conn:
                conn.rollback()
            raise StockRaceLostError(
                f"decrement_stock: 0 rows affected for product_id={product_id}, "
                f"quantity={quantity} -- stock was insufficient at commit time."
            )
        if own_conn:
            conn.commit()
        return get_product(product_id, conn=conn)
    finally:
        if own_conn:
            conn.close()


# ---------------------------------------------------------------------------
# quotes
# ---------------------------------------------------------------------------
def create_quote(product_id: str, quantity: int, conn: sqlite3.Connection = None) -> dict:
    own_conn = conn is None
    conn = conn or get_connection()
    try:
        product = get_product(product_id, conn=conn)
        if product is None:
            raise ValueError(f"Unknown product_id: {product_id}")

        quote_id = f"quote_{uuid.uuid4().hex[:10]}"
        created_at = _now_iso()
        expires_at = (
            datetime.now(timezone.utc) + timedelta(minutes=QUOTE_EXPIRY_MINUTES)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        total_price = round(product["price"] * quantity, 2)

        conn.execute(
            "INSERT INTO quotes (quote_id, product_id, quantity, total_price, "
            "created_at, expires_at, consumed) VALUES (?, ?, ?, ?, ?, ?, 0)",
            (quote_id, product_id, quantity, total_price, created_at, expires_at),
        )
        if own_conn:
            conn.commit()
        return get_quote(quote_id, conn=conn)
    finally:
        if own_conn:
            conn.close()


def get_quote(quote_id: str, conn: sqlite3.Connection = None) -> dict | None:
    own_conn = conn is None
    conn = conn or get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM quotes WHERE quote_id = ?", (quote_id,)
        ).fetchone()
        return _row_to_dict(row)
    finally:
        if own_conn:
            conn.close()


def is_quote_valid(quote: dict) -> tuple[bool, str]:
    """Returns (valid, reason). reason is '' when valid."""
    if quote is None:
        return False, "quote_not_found"
    if quote["consumed"]:
        return False, "quote_already_consumed"
    now = datetime.now(timezone.utc)
    expires_at = datetime.strptime(quote["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    if now > expires_at:
        return False, "quote_expired"
    return True, ""


def mark_quote_consumed(quote_id: str, conn: sqlite3.Connection = None) -> None:
    own_conn = conn is None
    conn = conn or get_connection()
    try:
        conn.execute("UPDATE quotes SET consumed = 1 WHERE quote_id = ?", (quote_id,))
        if own_conn:
            conn.commit()
    finally:
        if own_conn:
            conn.close()


# ---------------------------------------------------------------------------
# transactions
# ---------------------------------------------------------------------------
def get_transaction_by_quote_id(quote_id: str, conn: sqlite3.Connection = None) -> dict | None:
    own_conn = conn is None
    conn = conn or get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM transactions WHERE quote_id = ?", (quote_id,)
        ).fetchone()
        return _row_to_dict(row)
    finally:
        if own_conn:
            conn.close()


def get_transaction_by_razorpay_order_id(razorpay_order_id: str, conn: sqlite3.Connection = None) -> dict | None:
    """Used by the webhook handler to map an incoming Razorpay event back to our transaction."""
    own_conn = conn is None
    conn = conn or get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM transactions WHERE razorpay_order_id = ?", (razorpay_order_id,)
        ).fetchone()
        return _row_to_dict(row)
    finally:
        if own_conn:
            conn.close()


def get_transaction(txn_id: str, conn: sqlite3.Connection = None) -> dict | None:
    own_conn = conn is None
    conn = conn or get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM transactions WHERE txn_id = ?", (txn_id,)
        ).fetchone()
        return _row_to_dict(row)
    finally:
        if own_conn:
            conn.close()


def create_transaction(
    quote_id: str, agent_id: str, amount: float, status: str, conn: sqlite3.Connection = None
) -> dict:
    """
    Idempotent: if a transaction already exists for this quote_id, that
    existing row is returned instead of inserting a new one. The UNIQUE
    constraint IntegrityError is also caught as the belt-and-suspenders
    case for a race between two near-simultaneous callers.

    NOTE: this function does not tell the caller whether IT was the one
    that created the row. For any caller that goes on to run further
    side effects (status transitions, external payment calls, audit
    writes) after this call, use try_create_transaction() instead --
    otherwise concurrent duplicate callers will all re-run those side
    effects on the same row. See try_create_transaction()'s docstring.
    """
    txn, _created = try_create_transaction(quote_id, agent_id, amount, status, conn=conn)
    return txn


def try_create_transaction(
    quote_id: str, agent_id: str, amount: float, status: str, conn: sqlite3.Connection = None
) -> tuple[dict, bool]:
    """
    Same idempotent create as create_transaction(), but returns
    (transaction_dict, created: bool) so the caller can tell whether THIS
    call was the one that actually inserted the row.

    This matters under real concurrency: if two near-simultaneous requests
    both pass the pre-check "does a transaction already exist?" before
    either has committed an insert, both will attempt to create one. The
    UNIQUE(quote_id) constraint guarantees only one row ever exists -- but
    without this created flag, BOTH callers would go on to independently
    run status transitions, audit logging, and (critically) any external
    payment API call on that single row, racing each other and risking a
    double payment attempt. Callers MUST check `created` and skip further
    side effects when it is False, treating the request as an idempotent
    duplicate instead.
    """
    own_conn = conn is None
    conn = conn or get_connection()
    try:
        existing = get_transaction_by_quote_id(quote_id, conn=conn)
        if existing is not None:
            return existing, False

        txn_id = f"txn_{uuid.uuid4().hex[:10]}"
        ts = _now_iso()
        try:
            conn.execute(
                "INSERT INTO transactions (txn_id, quote_id, agent_id, amount, status, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (txn_id, quote_id, agent_id, amount, status, ts, ts),
            )
            if own_conn:
                conn.commit()
            return get_transaction(txn_id, conn=conn), True
        except sqlite3.IntegrityError:
            # Another concurrent caller won the race and committed first.
            if own_conn:
                conn.rollback()
            return get_transaction_by_quote_id(quote_id, conn=conn), False
    finally:
        if own_conn:
            conn.close()


def update_transaction_status(
    txn_id: str,
    new_status: str,
    decline_reason: str = None,
    razorpay_order_id: str = None,
    conn: sqlite3.Connection = None,
) -> dict:
    own_conn = conn is None
    conn = conn or get_connection()
    try:
        ts = _now_iso()
        conn.execute(
            "UPDATE transactions SET status = ?, decline_reason = COALESCE(?, decline_reason), "
            "razorpay_order_id = COALESCE(?, razorpay_order_id), updated_at = ? WHERE txn_id = ?",
            (new_status, decline_reason, razorpay_order_id, ts, txn_id),
        )
        if own_conn:
            conn.commit()
        return get_transaction(txn_id, conn=conn)
    finally:
        if own_conn:
            conn.close()


def list_transactions(status: str = None, conn: sqlite3.Connection = None) -> list[dict]:
    own_conn = conn is None
    conn = conn or get_connection()
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM transactions WHERE status = ? ORDER BY created_at DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM transactions ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if own_conn:
            conn.close()


def get_agent_daily_spend(agent_id: str, conn: sqlite3.Connection = None) -> float:
    """
    Sum of `completed` + `payment_pending` amounts for this agent, for
    today's calendar day in UTC (not a rolling 24h window).
    """
    own_conn = conn is None
    conn = conn or get_connection()
    try:
        today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        row = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM transactions "
            "WHERE agent_id = ? AND status IN ('completed', 'payment_pending') "
            "AND substr(created_at, 1, 10) = ?",
            (agent_id, today_utc),
        ).fetchone()
        return row["total"]
    finally:
        if own_conn:
            conn.close()


def get_agent(agent_id: str, conn: sqlite3.Connection = None) -> dict | None:
    own_conn = conn is None
    conn = conn or get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM buyer_agents WHERE agent_id = ?", (agent_id,)
        ).fetchone()
        return _row_to_dict(row)
    finally:
        if own_conn:
            conn.close()


# ---------------------------------------------------------------------------
# audit_log
# ---------------------------------------------------------------------------
def log_audit(txn_id: str, step: str, detail: dict, conn: sqlite3.Connection = None) -> dict:
    """
    Never put API keys or secrets in `detail` — this gets stored and
    displayed verbatim in the dashboard's audit trail drill-down.
    """
    own_conn = conn is None
    conn = conn or get_connection()
    try:
        ts = _now_iso()
        detail_json = json.dumps(detail)
        cur = conn.execute(
            "INSERT INTO audit_log (txn_id, step, detail, timestamp) VALUES (?, ?, ?, ?)",
            (txn_id, step, detail_json, ts),
        )
        if own_conn:
            conn.commit()
        log_id = cur.lastrowid
        row = conn.execute(
            "SELECT * FROM audit_log WHERE log_id = ?", (log_id,)
        ).fetchone()
        result = dict(row)
        result["detail"] = json.loads(result["detail"])
        return result
    finally:
        if own_conn:
            conn.close()


def get_audit_trail(txn_id: str, conn: sqlite3.Connection = None) -> list[dict]:
    own_conn = conn is None
    conn = conn or get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE txn_id = ? ORDER BY log_id ASC", (txn_id,)
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d["detail"])
            result.append(d)
        return result
    finally:
        if own_conn:
            conn.close()
