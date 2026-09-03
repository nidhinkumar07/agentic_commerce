"""
tests/test_failure_states.py

Proves a Razorpay failure lands the transaction in 'failed', not
'completed', with no double-charge and no stuck 'payment_pending' state.
Uses the injectable simulate_razorpay_failure flag rather than mocking the
SDK directly -- no real network calls are made either way.
"""

from conftest import auth_headers


def test_razorpay_failure_lands_in_failed_not_completed(helpers, api_client):
    quote_resp = api_client.post(
        "/quote", json={"product_id": "prod_test_instock", "quantity": 1}
    )
    assert quote_resp.status_code == 200
    quote = quote_resp.json()

    purchase_resp = api_client.post(
        "/purchase",
        json={
            "quote_id": quote["quote_id"],
            "agent_id": "agent_test_high",
            "simulate_razorpay_failure": True,
        },
        headers=auth_headers("agent_test_high"),
    )
    assert purchase_resp.status_code == 200
    txn = purchase_resp.json()["transaction"]

    assert txn["status"] == "failed"
    assert txn["status"] != "completed"
    assert txn["razorpay_order_id"] is None

    # Stock must be untouched -- decrement only ever happens on 'completed'.
    product = api_client.get(f"/catalog/prod_test_instock").json()
    assert product["stock"] == 10  # unchanged from seed

    # Audit trail should show exactly where it broke.
    audit_resp = api_client.get(f"/audit/{txn['txn_id']}")
    assert audit_resp.status_code == 200
    steps = [entry["step"] for entry in audit_resp.json()]
    assert "payment_pending" in steps
    assert "razorpay_call_failed" in steps
    assert "razorpay_order_created" not in steps
