"""
tests/test_idempotency.py

Proves that calling create_transaction() twice for the same quote_id
returns the existing row rather than creating a duplicate or erroring.
"""


def test_duplicate_purchase_same_quote_id_is_deduped(helpers, conn):
    quote = helpers.create_quote("prod_test_instock", 1)

    txn_1 = helpers.create_transaction(
        quote["quote_id"], "agent_test_high", quote["total_price"], "pending_gate", conn=conn
    )
    txn_2 = helpers.create_transaction(
        quote["quote_id"], "agent_test_high", quote["total_price"], "pending_gate", conn=conn
    )

    assert txn_1["txn_id"] == txn_2["txn_id"]

    rows = conn.execute(
        "SELECT COUNT(*) AS c FROM transactions WHERE quote_id = ?", (quote["quote_id"],)
    ).fetchone()
    assert rows["c"] == 1
