"""
tests/test_concurrent_purchase.py

Proves the stock-race fix (db/helpers.py's decrement_stock() rowcount
check + StockRaceLostError) actually works under real concurrency, not
just sequentially. Fires genuinely concurrent /purchase requests -- via
real threads hitting a live TestClient -- for TWO DIFFERENT quotes on a
product with exactly 1 unit of stock. Both requests' gate evaluations can
pass the purchase-time stock check (since decrement only happens at
'completed', not at gate-evaluation time), so both proceed to attempt
payment; the race is decided at decrement_stock(), which now must reject
exactly one of them instead of silently overselling.
"""

import threading


def test_concurrent_purchase_on_last_unit_of_stock_does_not_oversell(helpers, api_client, monkeypatch):
    from conftest import auth_headers
    import main as merchant_main

    # This sandbox has no network route to the real Razorpay API, so an
    # unmocked create_order() would fail at the payment-call step for
    # BOTH requests -- before either ever reaches decrement_stock(), which
    # is exactly the code path this test needs to exercise. Force
    # create_order() to succeed so the race plays out where it actually
    # lives: in decrement_stock()'s rowcount check.
    def fake_create_order(amount, currency, receipt_id, simulate_failure=False):
        return True, f"order_fake_{receipt_id}"

    monkeypatch.setattr(merchant_main.razorpay_client, "create_order", fake_create_order)

    # Two separate quotes for the SAME single-unit-stock product -- this
    # is deliberately NOT the same quote_id (that's the idempotency case,
    # covered in test_idempotency.py). This is two distinct transactions
    # racing for the same physical unit of inventory.
    quote_a = api_client.post(
        "/quote", json={"product_id": "prod_test_laststock", "quantity": 1}
    ).json()
    quote_b = api_client.post(
        "/quote", json={"product_id": "prod_test_laststock", "quantity": 1}
    ).json()
    assert quote_a["quote_id"] != quote_b["quote_id"]

    results = []
    lock = threading.Lock()

    def fire(quote_id):
        resp = api_client.post(
            "/purchase",
            json={"quote_id": quote_id, "agent_id": "agent_test_high"},
            headers=auth_headers("agent_test_high"),
        )
        with lock:
            results.append(resp.json())

    threads = [
        threading.Thread(target=fire, args=(quote_a["quote_id"],)),
        threading.Thread(target=fire, args=(quote_b["quote_id"],)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert len(results) == 2

    statuses = sorted(r["transaction"]["status"] for r in results)
    reasons = [r["transaction"].get("decline_reason") for r in results]

    # Exactly one of the two genuinely distinct transactions may complete.
    # The BEGIN IMMEDIATE serialization added to close the daily-cap race
    # (see main.py) has a beneficial side effect here: it often narrows
    # the stock race so tightly that the second request's gate check
    # already sees stock=0 and declines cleanly with 'insufficient_stock'
    # BEFORE ever reaching decrement_stock(). Less commonly (depending on
    # thread scheduling), both requests pass the gate's stock check before
    # either decrements, and the race is instead caught by
    # decrement_stock()'s rowcount check, landing in 'failed' with
    # 'stock_race_lost'. Both are correct, safe outcomes -- what must NEVER
    # happen is both landing in 'completed'. This test accepts either safe
    # outcome; test_stock_race_is_caught_deterministically below forces the
    # decrement_stock() path specifically and unconditionally.
    assert statuses.count("completed") == 1, f"expected exactly one completed, got: {statuses}, full results: {results}"
    other_status = [s for s in statuses if s != "completed"][0]
    assert other_status in ("declined", "failed"), f"unexpected second status: {other_status}"
    assert "insufficient_stock" in reasons or "stock_race_lost" in reasons

    # The product's stock must have decremented exactly once, not twice
    # and not zero times -- proving no oversell and no lost sale.
    product = api_client.get("/catalog/prod_test_laststock").json()
    assert product["stock"] == 0


def test_stock_race_is_caught_deterministically(helpers, conn):
    """
    Unit-level test that forces the exact race decrement_stock() must
    catch, independent of thread scheduling: two sequential decrement
    attempts against a product with only 1 unit of stock. The first must
    succeed; the second must raise StockRaceLostError rather than
    silently succeeding or driving stock negative.
    """
    quote = helpers.create_quote("prod_test_laststock", 1)
    assert quote["quantity"] == 1

    product_before = helpers.get_product("prod_test_laststock", conn=conn)
    assert product_before["stock"] == 1

    # First decrement: succeeds, stock 1 -> 0.
    product_after_first = helpers.decrement_stock("prod_test_laststock", quantity=1, conn=conn)
    assert product_after_first["stock"] == 0

    # Second decrement (simulating a second transaction that already
    # passed its own earlier stock check before the first one committed):
    # must raise, not silently succeed and drive stock to -1.
    import pytest
    with pytest.raises(helpers.StockRaceLostError):
        helpers.decrement_stock("prod_test_laststock", quantity=1, conn=conn)

    product_final = helpers.get_product("prod_test_laststock", conn=conn)
    assert product_final["stock"] == 0  # never went negative


def test_concurrent_purchase_does_not_exceed_daily_spend_cap(helpers, api_client, monkeypatch):
    """
    Proves the BEGIN IMMEDIATE serialization in main.py's /purchase closes
    the daily-spend-cap read-check-write race: two truly concurrent
    purchases that individually pass the per-transaction cap but together
    exceed the daily cap must never BOTH be approved.
    """
    from conftest import auth_headers
    import main as merchant_main

    def fake_create_order(amount, currency, receipt_id, simulate_failure=False):
        return True, f"order_fake_{receipt_id}"

    monkeypatch.setattr(merchant_main.razorpay_client, "create_order", fake_create_order)

    # agent_test_low: per-txn cap 200.0, daily cap 1000.0.
    # Two purchases of 700.0 each (using prod_test_instock at price 1000.0
    # with quantity < 1 isn't allowed, so instead use two quotes priced
    # under the per-txn cap that together exceed the daily cap): we need a
    # per-unit price <= 200 with quantity 1, and two of them summing > 1000
    # -- 3 units at ~334 each would exceed daily cap while each unit alone
    # is under it, but our test product is priced at 1000.0/unit. Simpler:
    # create a dedicated low-priced product for this test via direct SQL.
    conn = helpers.get_connection()
    conn.execute(
        "INSERT INTO products (product_id, name, price, currency, stock) VALUES "
        "('prod_test_cheap', 'Test Cheap Item', 180.0, 'INR', 100)"
    )
    conn.commit()
    conn.close()

    # agent_test_low daily cap is 1000.0, per-txn cap 200.0.
    # 6 purchases of 180.0 = 1080.0, which exceeds the 1000.0 daily cap,
    # while each individual purchase (180.0) is under the 200.0 per-txn cap.
    quote_ids = []
    for _ in range(6):
        q = api_client.post(
            "/quote", json={"product_id": "prod_test_cheap", "quantity": 1}
        ).json()
        quote_ids.append(q["quote_id"])

    results = []
    lock = threading.Lock()

    def fire(quote_id):
        resp = api_client.post(
            "/purchase",
            json={"quote_id": quote_id, "agent_id": "agent_test_low"},
            headers=auth_headers("agent_test_low"),
        )
        with lock:
            results.append(resp.json())

    threads = [threading.Thread(target=fire, args=(qid,)) for qid in quote_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert len(results) == 6

    approved_amounts = [
        r["transaction"]["amount"]
        for r in results
        if r["transaction"]["decline_reason"] != "exceeds_daily_spend_cap"
    ]
    total_approved = sum(approved_amounts)

    # The sum of everything that got past the daily-cap gate check must
    # never exceed the cap -- this is the actual money-safety guarantee,
    # regardless of exactly how many individual requests got through.
    assert total_approved <= 1000.0, f"daily cap exceeded: {total_approved} from {len(approved_amounts)} approvals, full results: {results}"
    # At least one must have been approved (proving the fix doesn't just
    # decline everything).
    assert len(approved_amounts) >= 1
