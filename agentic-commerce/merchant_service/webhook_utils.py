"""
merchant_service/webhook_utils.py

Razorpay webhook signature verification. Razorpay signs every webhook
payload with HMAC-SHA256 using a webhook secret configured in the
Razorpay Dashboard (separate from the API key/secret) and sends it in the
X-Razorpay-Signature header. This module verifies that signature so an
attacker who doesn't know the webhook secret can't forge a fake
"payment.captured" event and trick the merchant into marking a
transaction completed without a real payment ever happening.

This is the real, standard Razorpay webhook verification pattern (HMAC
over the raw request body -- signature verification MUST happen against
the raw bytes, before any JSON parsing, since re-serializing and
re-hashing parsed JSON is not guaranteed to reproduce the same bytes).
"""

import hashlib
import hmac
import os


def verify_webhook_signature(raw_body: bytes, signature_header: str, webhook_secret: str) -> bool:
    """
    Returns True if signature_header is a valid HMAC-SHA256 of raw_body
    using webhook_secret. Uses hmac.compare_digest to avoid timing-attack
    leakage of how much of the signature matched.
    """
    if not webhook_secret or not signature_header:
        return False
    expected = hmac.new(
        webhook_secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def sign_payload_for_testing(raw_body: bytes, webhook_secret: str) -> str:
    """
    Constructs a valid signature for a given payload -- used only by
    tests/demo scripts to prove the verification logic actually rejects
    tampered payloads and accepts correctly-signed ones, without needing
    a real Razorpay account to send us a real webhook.
    """
    return hmac.new(webhook_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
