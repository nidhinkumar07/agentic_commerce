"""
db/prove_idempotency.py

One-off script (not part of the app) that proves the UNIQUE(quote_id)
constraint on `transactions` is enforced at the DB level, not just in
application code. Inserts a real quote, then attempts two transaction
inserts against the same quote_id and shows the second one fail with a
real sqlite3.IntegrityError.
"""

import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

DB_PATH = os.environ.get("DATABASE_PATH", os.path.join(os.path.dirname(__file__), "merchant.db"))


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


conn = sqlite3.connect(DB_PATH)
conn.execute("PRAGMA foreign_keys = ON")
cur = conn.cursor()

quote_id = f"quote_{uuid.uuid4().hex[:8]}"
created_at = now_iso()
expires_at = (datetime.now(timezone.utc) + timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")

cur.execute(
    "INSERT INTO quotes (quote_id, product_id, quantity, total_price, created_at, expires_at, consumed) "
    "VALUES (?, ?, ?, ?, ?, ?, 0)",
    (quote_id, "prod_001", 1, 4499.0, created_at, expires_at),
)
conn.commit()
print(f"Inserted test quote: {quote_id}")

txn_id_1 = f"txn_{uuid.uuid4().hex[:8]}"
ts = now_iso()
cur.execute(
    "INSERT INTO transactions (txn_id, quote_id, agent_id, amount, status, created_at, updated_at) "
    "VALUES (?, ?, ?, ?, 'pending_gate', ?, ?)",
    (txn_id_1, quote_id, "agent_high", 4499.0, ts, ts),
)
conn.commit()
print(f"First transaction insert SUCCEEDED: {txn_id_1} (quote_id={quote_id})")

txn_id_2 = f"txn_{uuid.uuid4().hex[:8]}"
ts2 = now_iso()
print(f"\nAttempting SECOND transaction insert with the SAME quote_id ({quote_id})...")
try:
    cur.execute(
        "INSERT INTO transactions (txn_id, quote_id, agent_id, amount, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'pending_gate', ?, ?)",
        (txn_id_2, quote_id, "agent_high", 4499.0, ts2, ts2),
    )
    conn.commit()
    print("ERROR: second insert succeeded — constraint is NOT working!")
except sqlite3.IntegrityError as e:
    conn.rollback()
    print(f"REJECTED as expected — sqlite3.IntegrityError: {e}")

cur.execute("SELECT COUNT(*) FROM transactions WHERE quote_id = ?", (quote_id,))
count = cur.fetchone()[0]
print(f"\nFinal row count for quote_id={quote_id}: {count} (expected: 1)")

# Cleanup so this doesn't pollute the demo DB
cur.execute("DELETE FROM transactions WHERE quote_id = ?", (quote_id,))
cur.execute("DELETE FROM quotes WHERE quote_id = ?", (quote_id,))
conn.commit()
conn.close()
print("Cleaned up test rows.")
