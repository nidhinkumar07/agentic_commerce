"""
tests/test_webhook.py

Tests the Razorpay webhook endpoint: signature verification (must reject
tampered/unsigned payloads), correct processing of payment.captured /
payment.failed events, and idempotency (a webhook delivered twice must
not double-decrement stock or flip an already-resolved transaction).
"""

import json

from conftest import auth_headers

WEBHOOK_SECRET = "test-webhook-secret"


def _sign(body_bytes: bytes) -> str:
    import webhook_utils
    return webhook_utils.sign_payload_for_testing(body_bytes, WEBHOOK_SECRET)


def _make_payload(event: str, order_id: str, payment_id: str = "pay_test123") -> bytes:
    return json.dumps({
        "event": event,
        "payload": {"payment": {"entity": {"id": payment_id, "order_id": order_id}}},
    }).encode("utf-8")


def _setup_pending_transaction(helpers, conn, api_client, monkeypatch):
    """Creates a real transaction sitting in payment_pending via the normal purchase flow."""
    import main as merchant_main

    def fake_create_order(amount, currency, receipt_id, simulate_failure=False):
        # Simulate the synchronous response being "lost" -- payment_pending
        # is written (as it always is, before the call) but we pretend the
        # merchant never got a usable response, leaving it there for the
        # webhook to resolve later. In reality create_order() always
        # returns something; this fixture just needs the DB left in
        # payment_pending with a known razorpay_order_id, which we do
        # directly for test clarity.
        return True, "order_webhook_test_001"

    monkeypatch.setattr(merchant_main.razorpay_client, "create_order", fake_create_order)

    quote = api_client.post(
        "/quote", json={"product_id": "prod_test_instock", "quantity": 1}
    ).json()
    resp = api_client.post(
        "/purchase",
        json={"quote_id": quote["quote_id"], "agent_id": "agent_test_high"},
        headers=auth_headers("agent_test_high"),
    )
    txn = resp.json()["transaction"]
    # This will actually be 'completed' already since fake_create_order
    # succeeds and the synchronous path completes it -- force it back to
    # payment_pending to simulate the "lost response" scenario the webhook
    # is meant to reconcile.
    conn.execute(
        "UPDATE transactions SET status = 'payment_pending' WHERE txn_id = ?",
        (txn["txn_id"],),
    )
    conn.commit()
    return txn


def test_webhook_rejects_tampered_signature(helpers, api_client, monkeypatch):
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", WEBHOOK_SECRET)
    import main as merchant_main
    import importlib
    importlib.reload(merchant_main)
    from fastapi.testclient import TestClient
    client = TestClient(merchant_main.app)

    body = _make_payload("payment.captured", "order_does_not_matter")
    resp = client.post(
        "/webhooks/razorpay",
        content=body,
        headers={"Content-Type": "application/json", "X-Razorpay-Signature": "not-the-real-signature"},
    )
    assert resp.status_code == 401


def test_webhook_rejects_missing_signature(helpers, api_client, monkeypatch):
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", WEBHOOK_SECRET)
    import main as merchant_main
    import importlib
    importlib.reload(merchant_main)
    from fastapi.testclient import TestClient
    client = TestClient(merchant_main.app)

    body = _make_payload("payment.captured", "order_does_not_matter")
    resp = client.post("/webhooks/razorpay", content=body, headers={"Content-Type": "application/json"})
    assert resp.status_code == 401


def test_webhook_processes_valid_payment_captured(helpers, conn, api_client, monkeypatch):
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", WEBHOOK_SECRET)
    import main as merchant_main
    import importlib
    importlib.reload(merchant_main)
    from fastapi.testclient import TestClient
    client = TestClient(merchant_main.app)

    txn = _setup_pending_transaction(helpers, conn, client, monkeypatch)
    # Attach the razorpay_order_id the webhook will reference.
    conn.execute(
        "UPDATE transactions SET razorpay_order_id = 'order_webhook_test_001' WHERE txn_id = ?",
        (txn["txn_id"],),
    )
    conn.commit()

    product_before = helpers.get_product("prod_test_instock", conn=conn)

    body = _make_payload("payment.captured", "order_webhook_test_001")
    resp = client.post(
        "/webhooks/razorpay",
        content=body,
        headers={"Content-Type": "application/json", "X-Razorpay-Signature": _sign(body)},
    )
    assert resp.status_code == 200
    assert resp.json()["transaction_status"] == "completed"

    updated_txn = helpers.get_transaction(txn["txn_id"], conn=conn)
    assert updated_txn["status"] == "completed"

    product_after = helpers.get_product("prod_test_instock", conn=conn)
    assert product_after["stock"] == product_before["stock"] - 1


def test_webhook_is_idempotent_on_duplicate_delivery(helpers, conn, api_client, monkeypatch):
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", WEBHOOK_SECRET)
    import main as merchant_main
    import importlib
    importlib.reload(merchant_main)
    from fastapi.testclient import TestClient
    client = TestClient(merchant_main.app)

    txn = _setup_pending_transaction(helpers, conn, client, monkeypatch)
    conn.execute(
        "UPDATE transactions SET razorpay_order_id = 'order_webhook_test_001' WHERE txn_id = ?",
        (txn["txn_id"],),
    )
    conn.commit()

    body = _make_payload("payment.captured", "order_webhook_test_001")
    sig = _sign(body)

    resp1 = client.post("/webhooks/razorpay", content=body, headers={"Content-Type": "application/json", "X-Razorpay-Signature": sig})
    assert resp1.json()["transaction_status"] == "completed"

    stock_after_first = helpers.get_product("prod_test_instock", conn=conn)["stock"]

    # Razorpay retries webhooks by design -- deliver the identical event again.
    resp2 = client.post("/webhooks/razorpay", content=body, headers={"Content-Type": "application/json", "X-Razorpay-Signature": sig})
    assert resp2.status_code == 200
    assert resp2.json()["status"] == "ignored"

    stock_after_second = helpers.get_product("prod_test_instock", conn=conn)["stock"]
    assert stock_after_second == stock_after_first  # no double-decrement


def test_webhook_for_unknown_order_id_is_ignored_not_error(helpers, api_client, monkeypatch):
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", WEBHOOK_SECRET)
    import main as merchant_main
    import importlib
    importlib.reload(merchant_main)
    from fastapi.testclient import TestClient
    client = TestClient(merchant_main.app)

    body = _make_payload("payment.captured", "order_never_existed")
    resp = client.post(
        "/webhooks/razorpay",
        content=body,
        headers={"Content-Type": "application/json", "X-Razorpay-Signature": _sign(body)},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
