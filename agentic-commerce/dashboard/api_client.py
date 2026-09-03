"""
dashboard/api_client.py

All merchant-service HTTP calls the dashboard makes, kept in one module
separate from Streamlit rendering code. This file contains NO business
logic or decisions of its own -- it only calls the FastAPI endpoints and
returns their responses. Every approval/decline decision still comes from
the merchant service's gate.py, exactly as it does for the buyer agent CLI.
"""

import os
import requests

MERCHANT_API_BASE_URL = os.environ.get("MERCHANT_API_BASE_URL", "http://127.0.0.1:8000")

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


def request_quote(product_id: str, quantity: int = 1):
    resp = requests.post(
        f"{MERCHANT_API_BASE_URL}/quote",
        json={"product_id": product_id, "quantity": quantity},
        timeout=10,
    )
    return resp.status_code, resp.json()


def request_purchase(quote_id: str, agent_id: str, simulate_razorpay_failure: bool = False):
    resp = requests.post(
        f"{MERCHANT_API_BASE_URL}/purchase",
        json={
            "quote_id": quote_id,
            "agent_id": agent_id,
            "simulate_razorpay_failure": simulate_razorpay_failure,
        },
        headers={"X-Agent-Secret": DEMO_AGENT_SECRETS.get(agent_id, "")},
        timeout=10,
    )
    return resp.status_code, resp.json()


def run_agent_flow(product_id: str, quantity: int, agent_id: str, simulate_razorpay_failure: bool = False):
    """
    Convenience wrapper matching what the buyer agent CLI does: quote then
    purchase. Still no decisions made here -- just sequencing two API calls
    and returning their combined result for the dashboard to display.
    """
    quote_status, quote_body = request_quote(product_id, quantity)
    if quote_status >= 400:
        return {"stage": "quote", "status_code": quote_status, "body": quote_body}

    purchase_status, purchase_body = request_purchase(
        quote_body["quote_id"], agent_id, simulate_razorpay_failure=simulate_razorpay_failure
    )
    return {
        "stage": "purchase",
        "status_code": purchase_status,
        "quote": quote_body,
        "body": purchase_body,
    }
