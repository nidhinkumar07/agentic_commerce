"""
dashboard/api_client.py

All merchant-service HTTP calls the dashboard makes, kept in one module
separate from Streamlit rendering code. This file contains NO business
logic or decisions of its own -- it only calls the FastAPI endpoints and
returns their responses. Every approval/decline decision still comes from
the merchant service's gate.py, exactly as it does for the buyer agent CLI.
"""

import os
import sys

import requests

MERCHANT_API_BASE_URL = os.environ.get("MERCHANT_API_BASE_URL", "http://127.0.0.1:8000")

# Path to the buyer agent's on-disk private keys, matching what
# db/generate_agent_keys.py writes and buyer_agent/agent.py reads. The
# dashboard signs mandate-backed requests the exact same way the CLI
# does (see sign_purchase_request_with_mandate below) -- this is still
# the AGENT's own signing step, not a merchant/gate decision, so it's
# consistent with this file's "no business logic" scope: the merchant
# service independently re-verifies the signature server-side regardless
# of what gets sent here.
_AGENT_KEYS_DIR = os.path.join(os.path.dirname(__file__), "..", "buyer_agent", "keys")

# Demo-only agent secrets, matching db/seed.py's fixed values, so the
# dashboard can drive purchases without asking the user to paste secrets
# into the UI. In a real system these would never be hardcoded in a
# frontend -- see README "Security Considerations".
DEMO_AGENT_SECRETS = {
    "agent_high": "demo-secret-high-8f2a1c",
    "agent_low": "demo-secret-low-3e9b7d",
}


def get_catalog():
    resp = requests.get(f"{MERCHANT_API_BASE_URL}/catalog", timeout=10)
    resp.raise_for_status()
    return resp.json()


def get_related_products(product_id: str):
    resp = requests.get(f"{MERCHANT_API_BASE_URL}/catalog/{product_id}/related", timeout=10)
    if resp.status_code != 200:
        return []
    return resp.json()


def get_transactions(status: str = None):
    params = {"status": status} if status else {}
    resp = requests.get(f"{MERCHANT_API_BASE_URL}/transactions", params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


def get_audit_trail(txn_id: str):
    resp = requests.get(f"{MERCHANT_API_BASE_URL}/audit/{txn_id}", timeout=10)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def get_metrics():
    resp = requests.get(f"{MERCHANT_API_BASE_URL}/metrics", timeout=10)
    resp.raise_for_status()
    return resp.json()


def get_mandates(agent_id: str = None):
    params = {"agent_id": agent_id} if agent_id else {}
    resp = requests.get(f"{MERCHANT_API_BASE_URL}/mandates", params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


def revoke_mandate(mandate_id: str):
    resp = requests.post(f"{MERCHANT_API_BASE_URL}/mandates/{mandate_id}/revoke", timeout=10)
    resp.raise_for_status()
    return resp.json()


def get_mandate_attempts(agent_id: str = None, mandate_id: str = None, valid: bool = None):
    params = {}
    if agent_id:
        params["agent_id"] = agent_id
    if mandate_id:
        params["mandate_id"] = mandate_id
    if valid is not None:
        # Sent as lowercase "true"/"false" explicitly -- FastAPI's bool
        # query parsing is case/format sensitive to what it's given, and
        # this avoids relying on requests' default str(bool) -> "True".
        params["valid"] = "true" if valid else "false"
    resp = requests.get(f"{MERCHANT_API_BASE_URL}/mandate-attempts", params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


def mask_signature(signature_hex: str) -> str:
    """
    Displays a signature as a short, unmistakably-truncated stand-in
    (e.g. 'a1b2c3d4…e5f6a7b8') rather than the full 128-hex-char Ed25519
    signature. Purely a display convenience for the dashboard -- an
    Ed25519 signature isn't secret material the way a private key is
    (it can't be used to derive the key or forge other signatures), but
    showing 128 hex characters per row makes every other column
    unreadable and invites people to habitually copy/paste raw
    signatures around, which is a bad habit to encourage even here.
    """
    if not signature_hex:
        return "—"
    if len(signature_hex) <= 16:
        return "•" * len(signature_hex)
    return f"{signature_hex[:8]}…{signature_hex[-8:]}"


def agent_key_path(agent_id: str) -> str:
    return os.path.join(_AGENT_KEYS_DIR, f"{agent_id}.key")


def has_local_agent_key(agent_id: str) -> bool:
    return os.path.isfile(agent_key_path(agent_id))


def sign_purchase_request_with_mandate(mandate_id: str, agent_id: str, quote_id: str, amount: float, tamper: bool = False):
    """
    Signs the purchase intent {quote_id, agent_id, amount, mandate_id,
    signed_at} with the agent's own Ed25519 private key -- identical
    payload and identical library call to what buyer_agent/agent.py uses
    (merchant_service/mandate.sign_purchase_request), so the dashboard
    demonstrates the real signing path rather than a lookalike. Reads
    the private key straight from buyer_agent/keys/<agent_id>.key -- in
    a real system this would live on the AGENT's machine, never here;
    see README "Security Considerations".

    tamper=True deliberately corrupts one byte of the signature after
    signing, purely to drive the "invalid_signature" decline path live
    from the dashboard (see mandate.py's verify_purchase_mandate).
    """
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "merchant_service"))
    import mandate as mandate_lib

    key_path = agent_key_path(agent_id)
    with open(key_path, "r") as f:
        agent_private_key_hex = f.read().strip()

    signature, signed_at = mandate_lib.sign_purchase_request(
        quote_id, agent_id, amount, mandate_id, agent_private_key_hex
    )

    if tamper:
        # Flip the last hex character so the signature is well-formed
        # hex but cryptographically invalid -- exercises verify's
        # InvalidSignature branch rather than a parsing error.
        flipped = "0" if signature[-1] != "0" else "1"
        signature = signature[:-1] + flipped

    return signature, signed_at


def request_quote(product_id: str, quantity: int = 1):
    resp = requests.post(
        f"{MERCHANT_API_BASE_URL}/quote",
        json={"product_id": product_id, "quantity": quantity},
        timeout=10,
    )
    return resp.status_code, resp.json()


def request_purchase(
    quote_id: str,
    agent_id: str,
    simulate_razorpay_failure: bool = False,
    mandate_id: str = None,
    signature: str = None,
    signed_at: str = None,
):
    body = {
        "quote_id": quote_id,
        "agent_id": agent_id,
        "simulate_razorpay_failure": simulate_razorpay_failure,
    }
    if mandate_id:
        body["mandate_id"] = mandate_id
        body["signature"] = signature
        body["signed_at"] = signed_at

    resp = requests.post(
        f"{MERCHANT_API_BASE_URL}/purchase",
        json=body,
        headers={"X-Agent-Secret": DEMO_AGENT_SECRETS.get(agent_id, "")},
        timeout=10,
    )
    return resp.status_code, resp.json()


def run_agent_flow(
    product_id: str,
    quantity: int,
    agent_id: str,
    simulate_razorpay_failure: bool = False,
    mandate_id: str = None,
    tamper_signature: bool = False,
):
    """
    Convenience wrapper matching what the buyer agent CLI does: quote then
    purchase. Still no gate decisions made here -- just sequencing API
    calls (plus, when mandate_id is given, the agent-side Ed25519 signing
    step -- see sign_purchase_request_with_mandate) and returning the
    combined result for the dashboard to display. The merchant's gate.py
    and mandate.py remain the only source of truth for approve/decline.
    """
    quote_status, quote_body = request_quote(product_id, quantity)
    if quote_status >= 400:
        return {"stage": "quote", "status_code": quote_status, "body": quote_body}

    signature = signed_at = None
    if mandate_id:
        amount = quote_body["total_price"]
        signature, signed_at = sign_purchase_request_with_mandate(
            mandate_id, agent_id, quote_body["quote_id"], amount, tamper=tamper_signature
        )

    purchase_status, purchase_body = request_purchase(
        quote_body["quote_id"],
        agent_id,
        simulate_razorpay_failure=simulate_razorpay_failure,
        mandate_id=mandate_id,
        signature=signature,
        signed_at=signed_at,
    )
    return {
        "stage": "purchase",
        "status_code": purchase_status,
        "quote": quote_body,
        "body": purchase_body,
        "mandate_id": mandate_id,
    }
