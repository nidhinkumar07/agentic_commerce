"""
merchant_service/razorpay_client.py

Thin, isolated wrapper around the Razorpay Python SDK. Kept separate from
gate.py — this module NEVER makes an approval/decline decision, it only
attempts to create a Razorpay order for a purchase the gate has already
approved.

Key handling: RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are read from the
environment via python-dotenv, loaded from .env. They are never printed,
logged, or written to the audit_log — only used to construct the SDK
client.

FAILURE SIMULATION:
For the required infra-failure demo, this module supports an injectable
"simulate failure" mode via the RAZORPAY_SIMULATE_FAILURE env var, or by
passing simulate_failure=True directly to create_order(). This lets us
reliably trigger the failed-payment path live in a demo without needing
to actually break network connectivity.
"""

import logging
import os
import ssl
import uuid

from dotenv import load_dotenv
import razorpay
from razorpay.errors import BadRequestError, ServerError, GatewayError

try:
    import requests.exceptions as requests_exc
except ImportError:
    requests_exc = None

load_dotenv()

logger = logging.getLogger("razorpay_client")

RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET")


class RazorpaySimulatedTimeoutError(Exception):
    """Raised only when failure simulation is deliberately triggered."""
    pass


def _get_client():
    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        raise RuntimeError(
            "RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET not set. Add real Razorpay "
            "test-mode keys to your .env file (see .env.example)."
        )
    return razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))


def _classify_exception(e: Exception) -> str:
    """
    Maps an exception to a short, stable category string used as the
    transaction's decline_reason on a failed payment call. This is
    deliberately a real taxonomy, not just the raw exception class name --
    'bad request', 'network unreachable', and 'TLS/cert failure' are
    operationally different problems (one is a code bug, one is
    infrastructure, one is often a proxy/cert config issue) and a human
    triaging failed transactions via GET /transactions?status=failed
    should be able to tell them apart from decline_reason alone, without
    opening server logs.

    Deliberately conservative: unrecognized exception types fall through
    to 'razorpay_unexpected_error' rather than being guessed at, since a
    wrong category is worse than an honest "unclassified."
    """
    if requests_exc is not None:
        if isinstance(e, requests_exc.Timeout):
            return "network_timeout"
        if isinstance(e, requests_exc.SSLError):
            return "tls_handshake_failure"
        if isinstance(e, requests_exc.ConnectionError):
            return "network_unreachable"
    if isinstance(e, ssl.SSLError):
        return "tls_handshake_failure"
    if isinstance(e, (ConnectionError, TimeoutError, OSError)):
        return "network_unreachable"
    return "razorpay_unexpected_error"


def create_order(amount: float, currency: str, receipt_id: str, simulate_failure: bool = False):
    """
    Creates a Razorpay order for the given amount (converted to paise, since
    Razorpay amounts are integer smallest-currency-unit values).

    Returns a tuple: (success: bool, order_id_or_error: str)

    On failure, order_id_or_error is formatted as "<category>: <detail>"
    where <category> is one of a small fixed set (see _classify_exception
    and the explicit except clauses below) -- main.py extracts the
    category as the transaction's decline_reason, so a failed transaction
    is queryable/filterable by WHY it failed, not just left as an opaque
    string. Categories: simulated_timeout, razorpay_bad_request,
    razorpay_gateway_error, razorpay_server_error, network_timeout,
    network_unreachable, tls_handshake_failure, razorpay_unexpected_error.

    simulate_failure=True (or env var RAZORPAY_SIMULATE_FAILURE=1) forces a
    simulated timeout/connection-style failure without touching the network,
    for the reliable live infra-failure demo required by the spec.

    RAZORPAY_FORCE_SUCCESS=1 (env var only -- strictly a test/diagnostic
    aid, never exposed as a request parameter) forces a simulated
    successful order without touching the network. Used by db/race_test.py
    to test money-cap concurrency behavior, which requires transactions to
    actually reach 'completed'/'payment_pending' -- a 'failed' transaction
    correctly does NOT count toward the daily spend cap, so testing the
    cap race requires forcing successes, not failures.

    Error messages returned here get written straight into audit_log and
    displayed in the dashboard's audit trail drill-down (which is by
    design -- the whole point is a visible failure trail). For Razorpay's
    OWN error responses (BadRequestError/ServerError/GatewayError) that's
    fine, since Razorpay's messages are meant to be shown to the
    integrator. For any OTHER exception (connection errors, SSL errors,
    etc.), the full exception string can leak internal infrastructure
    details (hostnames, cert paths, proxy configuration) into that same
    externally-visible audit trail. Those are logged in full server-side
    via `logger.error`, but only the category + exception class name is
    ever returned to the caller / written to the DB.
    """
    if simulate_failure or os.environ.get("RAZORPAY_SIMULATE_FAILURE") == "1":
        return False, "simulated_timeout: Razorpay API did not respond in time"

    if os.environ.get("RAZORPAY_FORCE_SUCCESS") == "1":
        return True, f"order_test_forced_{receipt_id}"

    amount_paise = int(round(amount * 100))

    try:
        client = _get_client()
        order = client.order.create(
            {
                "amount": amount_paise,
                "currency": currency,
                "receipt": receipt_id,
                "payment_capture": 1,
            }
        )
        return True, order["id"]
    except BadRequestError as e:
        # Malformed request (e.g. bad params) or, depending on SDK/API
        # version, invalid credentials -- Razorpay's own message is safe
        # to pass through as-is.
        return False, f"razorpay_bad_request: {str(e)}"
    except GatewayError as e:
        return False, f"razorpay_gateway_error: {str(e)}"
    except ServerError as e:
        return False, f"razorpay_server_error: {str(e)}"
    except Exception as e:
        # Catch-all for connection errors, timeouts, SSL errors, and
        # anything else unanticipated — never let an unhandled exception
        # propagate out of the payment call and leave the transaction in
        # an ambiguous state. Full detail goes to server-side logs only.
        logger.error(f"Unexpected error calling Razorpay for receipt_id={receipt_id}: {e}", exc_info=True)
        category = _classify_exception(e)
        return False, f"{category}: {type(e).__name__} (see server logs for detail)"
