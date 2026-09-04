"""
tests/test_mandate.py

Tests the AP2-inspired delegated payment mandate system: a principal-
signed authorization scope bound to an agent's Ed25519 keypair, plus
per-request signing by the agent itself. Covers both the pure
verification logic (merchant_service/mandate.py) and the full HTTP
integration through POST /purchase.
"""

from conftest import auth_headers


def _sign(mandate_setup, quote_id, amount):
    import mandate as mandate_lib
    return mandate_lib.sign_purchase_request(
        quote_id, mandate_setup["agent_id"], amount, mandate_setup["mandate_id"],
        mandate_setup["agent_private_key_hex"],
    )


def test_valid_mandate_and_signature_is_accepted(helpers, api_client, mandate_setup):
    quote = api_client.post(
        "/quote", json={"product_id": "prod_test_instock", "quantity": 1}
    ).json()  # amount = 1000.0, within the mandate's 5000.0 max_amount

    signature, signed_at = _sign(mandate_setup, quote["quote_id"], quote["total_price"])

    resp = api_client.post(
        "/purchase",
        json={
            "quote_id": quote["quote_id"],
            "agent_id": mandate_setup["agent_id"],
            "mandate_id": mandate_setup["mandate_id"],
            "signature": signature,
            "signed_at": signed_at,
        },
        headers=auth_headers("agent_test_high"),
    )
    assert resp.status_code == 200
    txn = resp.json()["transaction"]
    assert txn["status"] in ("approved", "payment_pending", "completed", "failed")  # gate approved it

    # The transaction's own audit trail should show the mandate check passed.
    audit = api_client.get(f"/audit/{txn['txn_id']}").json()
    steps = {entry["step"]: entry["detail"] for entry in audit}
    assert steps["mandate_verification"]["valid"] is True


def test_tampered_amount_is_rejected(helpers, api_client, mandate_setup):
    """Signature was produced for one amount; request claims a different one."""
    quote = api_client.post(
        "/quote", json={"product_id": "prod_test_instock", "quantity": 1}
    ).json()

    # Sign for 500.0, but the real quote amount is 1000.0 -- this exercises
    # signature mismatch, not the amount-exceeds-mandate-scope path (both
    # values are well within the mandate's 5000.0 max_amount).
    signature, signed_at = _sign(mandate_setup, quote["quote_id"], 500.0)

    resp = api_client.post(
        "/purchase",
        json={
            "quote_id": quote["quote_id"],
            "agent_id": mandate_setup["agent_id"],
            "mandate_id": mandate_setup["mandate_id"],
            "signature": signature,
            "signed_at": signed_at,
        },
        headers=auth_headers("agent_test_high"),
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "mandate verification failed: invalid_signature"


def test_amount_exceeding_mandate_scope_is_rejected(helpers, api_client, mandate_setup):
    quote = api_client.post(
        "/quote", json={"product_id": "prod_test_instock", "quantity": 10}
    ).json()  # amount = 10000.0, exceeds the mandate's 5000.0 max_amount

    signature, signed_at = _sign(mandate_setup, quote["quote_id"], quote["total_price"])

    resp = api_client.post(
        "/purchase",
        json={
            "quote_id": quote["quote_id"],
            "agent_id": mandate_setup["agent_id"],
            "mandate_id": mandate_setup["mandate_id"],
            "signature": signature,
            "signed_at": signed_at,
        },
        headers=auth_headers("agent_test_high"),
    )
    assert resp.status_code == 403
    assert "amount_exceeds_mandate_scope" in resp.json()["detail"]


def test_signature_from_wrong_key_is_rejected(helpers, api_client, mandate_setup):
    import mandate as mandate_lib

    quote = api_client.post(
        "/quote", json={"product_id": "prod_test_instock", "quantity": 1}
    ).json()

    wrong_private_hex, _ = mandate_lib.generate_keypair()  # a completely different keypair
    signature, signed_at = mandate_lib.sign_purchase_request(
        quote["quote_id"], mandate_setup["agent_id"], quote["total_price"],
        mandate_setup["mandate_id"], wrong_private_hex,
    )

    resp = api_client.post(
        "/purchase",
        json={
            "quote_id": quote["quote_id"],
            "agent_id": mandate_setup["agent_id"],
            "mandate_id": mandate_setup["mandate_id"],
            "signature": signature,
            "signed_at": signed_at,
        },
        headers=auth_headers("agent_test_high"),
    )
    assert resp.status_code == 403
    assert "invalid_signature" in resp.json()["detail"]


def test_nonexistent_mandate_is_rejected(helpers, api_client, mandate_setup):
    quote = api_client.post(
        "/quote", json={"product_id": "prod_test_instock", "quantity": 1}
    ).json()
    signature, signed_at = _sign(mandate_setup, quote["quote_id"], quote["total_price"])

    resp = api_client.post(
        "/purchase",
        json={
            "quote_id": quote["quote_id"],
            "agent_id": mandate_setup["agent_id"],
            "mandate_id": "mandate_does_not_exist",
            "signature": signature,
            "signed_at": signed_at,
        },
        headers=auth_headers("agent_test_high"),
    )
    assert resp.status_code == 403
    assert "mandate_not_found" in resp.json()["detail"]


def test_expired_signed_request_is_rejected_replay_protection(helpers, api_client, mandate_setup):
    import mandate as mandate_lib
    from datetime import datetime, timedelta, timezone

    quote = api_client.post(
        "/quote", json={"product_id": "prod_test_instock", "quantity": 1}
    ).json()

    old_signed_at = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = mandate_lib.request_signing_payload(
        quote["quote_id"], mandate_setup["agent_id"], quote["total_price"],
        mandate_setup["mandate_id"], old_signed_at,
    )
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    private_key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(mandate_setup["agent_private_key_hex"]))
    signature = private_key.sign(payload).hex()

    resp = api_client.post(
        "/purchase",
        json={
            "quote_id": quote["quote_id"],
            "agent_id": mandate_setup["agent_id"],
            "mandate_id": mandate_setup["mandate_id"],
            "signature": signature,
            "signed_at": old_signed_at,
        },
        headers=auth_headers("agent_test_high"),
    )
    assert resp.status_code == 403
    assert "signed_request_too_old" in resp.json()["detail"]


def test_mandate_id_without_signature_is_rejected(helpers, api_client, mandate_setup):
    """Providing mandate_id but omitting signature/signed_at must fail cleanly, not crash."""
    quote = api_client.post(
        "/quote", json={"product_id": "prod_test_instock", "quantity": 1}
    ).json()

    resp = api_client.post(
        "/purchase",
        json={
            "quote_id": quote["quote_id"],
            "agent_id": mandate_setup["agent_id"],
            "mandate_id": mandate_setup["mandate_id"],
        },
        headers=auth_headers("agent_test_high"),
    )
    assert resp.status_code == 400
