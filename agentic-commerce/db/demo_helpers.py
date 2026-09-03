"""
db/demo_helpers.py

Not part of the app — a one-off script that calls every function in
db/helpers.py against the real seeded DB and prints the real return
values, so Step 2 can be verified with actual output before Step 3 starts.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

import helpers as h

print("=" * 70)
print("1. get_product('prod_001')")
print("=" * 70)
p = h.get_product("prod_001")
print(p)

print("\n" + "=" * 70)
print("2. get_product('prod_999')  [should be None]")
print("=" * 70)
print(h.get_product("prod_999"))

print("\n" + "=" * 70)
print("3. create_quote('prod_001', 2)")
print("=" * 70)
quote = h.create_quote("prod_001", 2)
print(quote)

print("\n" + "=" * 70)
print("4. get_quote(quote_id) + is_quote_valid(quote)")
print("=" * 70)
fetched_quote = h.get_quote(quote["quote_id"])
print("fetched:", fetched_quote)
print("is_quote_valid:", h.is_quote_valid(fetched_quote))

print("\n" + "=" * 70)
print("5. create_transaction(quote_id, agent_id, amount, status) -- FIRST call")
print("=" * 70)
txn1 = h.create_transaction(quote["quote_id"], "agent_high", quote["total_price"], "pending_gate")
print(txn1)

print("\n" + "=" * 70)
print("6. create_transaction() again with SAME quote_id -- should return SAME row (idempotent)")
print("=" * 70)
txn2 = h.create_transaction(quote["quote_id"], "agent_high", quote["total_price"], "pending_gate")
print(txn2)
print(f"\nSame txn_id returned both times: {txn1['txn_id'] == txn2['txn_id']}")

print("\n" + "=" * 70)
print("7. update_transaction_status(txn_id, 'approved')")
print("=" * 70)
updated = h.update_transaction_status(txn1["txn_id"], "approved")
print(updated)

print("\n" + "=" * 70)
print("8. log_audit(txn_id, 'gate_decision', {...})")
print("=" * 70)
log_entry = h.log_audit(
    txn1["txn_id"],
    "gate_decision",
    {"decision": "approved", "checks": ["per_txn_cap_ok", "daily_cap_ok", "stock_ok"]},
)
print(log_entry)

log_entry2 = h.log_audit(
    txn1["txn_id"],
    "razorpay_call_initiated",
    {"amount": updated["amount"], "currency": "INR"},
)
print(log_entry2)

print("\n" + "=" * 70)
print("9. get_audit_trail(txn_id)")
print("=" * 70)
trail = h.get_audit_trail(txn1["txn_id"])
for entry in trail:
    print(entry)

print("\n" + "=" * 70)
print("10. get_agent_daily_spend('agent_high')  [before completing this txn: pending_gate/approved don't count]")
print("=" * 70)
print("current daily spend (approved status doesn't count yet):", h.get_agent_daily_spend("agent_high"))

print("\n" + "=" * 70)
print("11. update_transaction_status -> 'payment_pending', then check daily spend")
print("=" * 70)
h.update_transaction_status(txn1["txn_id"], "payment_pending")
print("daily spend now (payment_pending counts):", h.get_agent_daily_spend("agent_high"))

print("\n" + "=" * 70)
print("12. update_transaction_status -> 'completed', then decrement_stock")
print("=" * 70)
h.update_transaction_status(txn1["txn_id"], "completed", razorpay_order_id="order_test_demo123")
before = h.get_product("prod_001")
print("stock before decrement:", before["stock"])
after = h.decrement_stock("prod_001", quantity=quote["quantity"])
print("stock after decrement:", after["stock"])

print("\n" + "=" * 70)
print("13. list_transactions(status='completed')")
print("=" * 70)
for t in h.list_transactions(status="completed"):
    print(t)

print("\n" + "=" * 70)
print("14. get_agent('agent_low')")
print("=" * 70)
print(h.get_agent("agent_low"))

# --- cleanup so this demo run doesn't pollute the seeded demo DB ---
print("\n" + "=" * 70)
print("Cleanup: restoring stock and removing test rows")
print("=" * 70)
conn = h.get_connection()
conn.execute("UPDATE products SET stock = stock + ? WHERE product_id = ?", (quote["quantity"], "prod_001"))
conn.execute("DELETE FROM audit_log WHERE txn_id = ?", (txn1["txn_id"],))
conn.execute("DELETE FROM transactions WHERE txn_id = ?", (txn1["txn_id"],))
conn.execute("DELETE FROM quotes WHERE quote_id = ?", (quote["quote_id"],))
conn.commit()
conn.close()
print("Done. DB restored to seeded state.")
