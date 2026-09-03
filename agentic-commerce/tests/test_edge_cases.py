"""
tests/test_edge_cases.py

Covers ambiguous/edge behaviors the spec calls out explicitly: an expired
quote must be rejected, and an unknown agent_id must not crash the service.
"""

from datetime import datetime, timedelta, timezone


def test_expired_quote_is_rejected(helpers, gate, conn):
    quote = helpers.create_quote("prod_test_instock", 1)

    # Force the quote into the past directly via SQL -- this is the one
    # place raw SQL is acceptable in tests, since helpers.py intentionally
    # has no "backdate a quote" function (that's not a real app behavior).
    past_time = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "UPDATE quotes SET expires_at = ? WHERE quote_id = ?", (past_time, quote["quote_id"])
    )
    conn.commit()

    expired_quote = helpers.get_quote(quote["quote_id"], conn=conn)
    agent = helpers.get_agent("agent_test_high")

    valid, reason = helpers.is_quote_valid(expired_quote)
    assert valid is False
    assert reason == "quote_expired"

    result = gate.evaluate_purchase(expired_quote, agent, conn=conn)
    assert result.decision == "declined"
    assert result.reason == "quote_expired"


def test_unknown_agent_id_does_not_crash(helpers, api_client):
    quote_resp = api_client.post(
        "/quote", json={"product_id": "prod_test_instock", "quantity": 1}
    )
    assert quote_resp.status_code == 200
    quote = quote_resp.json()

    purchase_resp = api_client.post(
        "/purchase",
        json={"quote_id": quote["quote_id"], "agent_id": "agent_does_not_exist"},
    )

    # Must return a clean 404, not a 500 or an unhandled exception.
    assert purchase_resp.status_code == 404
    assert "agent_does_not_exist" in purchase_resp.json()["detail"]


def test_purchase_without_secret_header_is_rejected(helpers, api_client):
    quote_resp = api_client.post(
        "/quote", json={"product_id": "prod_test_instock", "quantity": 1}
    )
    quote = quote_resp.json()

    purchase_resp = api_client.post(
        "/purchase",
        json={"quote_id": quote["quote_id"], "agent_id": "agent_test_high"},
        # no X-Agent-Secret header at all
    )
    assert purchase_resp.status_code == 401


def test_purchase_with_wrong_secret_header_is_rejected(helpers, api_client):
    quote_resp = api_client.post(
        "/quote", json={"product_id": "prod_test_instock", "quantity": 1}
    )
    quote = quote_resp.json()

    purchase_resp = api_client.post(
        "/purchase",
        json={"quote_id": quote["quote_id"], "agent_id": "agent_test_high"},
        headers={"X-Agent-Secret": "definitely-not-the-right-secret"},
    )
    assert purchase_resp.status_code == 401


def test_quote_quantity_above_max_is_rejected(helpers, api_client):
    resp = api_client.post(
        "/quote", json={"product_id": "prod_test_instock", "quantity": 999}
    )
    assert resp.status_code == 400
