"""
tests/test_gate.py

Tests the deterministic gate logic directly (gate.evaluate_purchase),
independent of the HTTP layer. Uses a throwaway test DB, never the real
seeded demo DB.
"""


def _make_quote(helpers, product_id, quantity=1):
    return helpers.create_quote(product_id, quantity)


def test_gate_approves_within_caps(helpers, gate, conn):
    quote = _make_quote(helpers, "prod_test_instock", quantity=1)  # 1000.0
    agent = helpers.get_agent("agent_test_high")  # per-txn cap 5000, daily cap 6000

    result = gate.evaluate_purchase(quote, agent, conn=conn)

    assert result.decision == "approved"
    assert result.reason == "all_checks_passed"


def test_gate_declines_over_per_txn_cap(helpers, gate, conn):
    quote = _make_quote(helpers, "prod_test_instock", quantity=1)  # 1000.0
    agent = helpers.get_agent("agent_test_low")  # per-txn cap 200.0

    result = gate.evaluate_purchase(quote, agent, conn=conn)

    assert result.decision == "declined"
    assert result.reason == "exceeds_per_transaction_cap"


def test_gate_declines_over_daily_cap(helpers, gate, conn):
    agent = helpers.get_agent("agent_test_high")  # daily cap 6000.0, per-txn cap 5000.0

    # Simulate prior spend today: create + complete a transaction for
    # 5000.0 so the agent has already spent right up near its daily cap.
    prior_quote = _make_quote(helpers, "prod_test_instock", quantity=5)  # 5000.0
    prior_txn = helpers.create_transaction(
        prior_quote["quote_id"], agent["agent_id"], prior_quote["total_price"], "pending_gate"
    )
    helpers.update_transaction_status(prior_txn["txn_id"], "completed")

    daily_spend_so_far = helpers.get_agent_daily_spend(agent["agent_id"], conn=conn)
    assert daily_spend_so_far == 5000.0  # sanity check on the fixture itself

    # New quote for another 1000.0 -- individually within the 5000 per-txn
    # cap, but 5000 + 1000 = 6000 which is AT the daily cap; push it over
    # with one more unit to force a real decline.
    new_quote = _make_quote(helpers, "prod_test_instock", quantity=2)  # 2000.0
    result = gate.evaluate_purchase(new_quote, agent, conn=conn)

    assert result.decision == "declined"
    assert result.reason == "exceeds_daily_spend_cap"


def test_gate_declines_out_of_stock(helpers, gate, conn):
    quote = _make_quote(helpers, "prod_test_oos", quantity=1)  # stock is 0
    agent = helpers.get_agent("agent_test_high")

    result = gate.evaluate_purchase(quote, agent, conn=conn)

    assert result.decision == "declined"
    assert result.reason == "insufficient_stock"
