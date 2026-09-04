"""
merchant_service/mandate.py

AP2-inspired delegated payment authorization. Two distinct signatures are
involved, matching AP2's own stated principle -- "verifiable intent, not
inferred action":

1. PRINCIPAL SIGNATURE (issued once, stored server-side as a `mandates`
   row): the human/principal who owns the money signs a binding of
   {agent_id, agent's public key, max_amount, currency, expiry}. This is
   the "I authorize this specific agent keypair to spend up to X" proof.
   Created by db/generate_agent_keys.py, verified once at issuance time
   and trusted from the stored row thereafter (the same way a merchant
   trusts a payment method they've already verified, not by re-deriving
   trust from the caller on every request).

2. PER-REQUEST AGENT SIGNATURE (checked on every /purchase call): the
   agent signs the SPECIFIC purchase intent {quote_id, agent_id, amount,
   mandate_id, signed_at} with its own private key. The merchant verifies
   this against the agent_public_key stored in the matching mandate row.
   This is the "prove THIS specific request genuinely came from the
   holder of that authorized scope, right now, not replayed from
   somewhere else" proof -- it's what makes this meaningfully stronger
   than the static X-Agent-Secret header alone.

Honest scope note: this demonstrates the cryptographic delegation pattern
end-to-end and is fully real, tested Ed25519 signing/verification -- it
does NOT integrate with any real bank rail, UPI mandate, or NPCI
infrastructure, which requires banking-partner access this project
doesn't have. See README "Delegated Payment Authorization" for the full
scope statement.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.exceptions import InvalidSignature

REQUEST_SIGNATURE_MAX_AGE_SECONDS = 300  # replay window: a signed request older than this is rejected


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def generate_keypair() -> tuple[str, str]:
    """Returns (private_key_hex, public_key_hex) for a new Ed25519 keypair."""
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    private_hex = private_key.private_bytes_raw().hex()
    public_hex = public_key.public_bytes_raw().hex()
    return private_hex, public_hex


def mandate_binding_payload(agent_id: str, agent_public_key: str, max_amount: float, currency: str, expires_at: str) -> bytes:
    """Canonical, deterministic payload the PRINCIPAL signs to issue a mandate."""
    payload = {
        "agent_id": agent_id,
        "agent_public_key": agent_public_key,
        "max_amount": max_amount,
        "currency": currency,
        "expires_at": expires_at,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_mandate(agent_id: str, agent_public_key: str, max_amount: float, currency: str, expires_at: str, principal_private_key_hex: str) -> str:
    """Principal signs the mandate binding. Returns hex-encoded signature."""
    private_key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(principal_private_key_hex))
    payload = mandate_binding_payload(agent_id, agent_public_key, max_amount, currency, expires_at)
    return private_key.sign(payload).hex()


def request_signing_payload(quote_id: str, agent_id: str, amount: float, mandate_id: str, signed_at: str) -> bytes:
    """Canonical, deterministic payload the AGENT signs for each individual purchase request."""
    payload = {
        "quote_id": quote_id,
        "agent_id": agent_id,
        "amount": amount,
        "mandate_id": mandate_id,
        "signed_at": signed_at,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_purchase_request(quote_id: str, agent_id: str, amount: float, mandate_id: str, agent_private_key_hex: str) -> tuple[str, str]:
    """
    Agent signs a specific purchase intent. Returns (signature_hex, signed_at)
    -- signed_at is generated here so the same timestamp is used in both the
    signed payload and the value sent to the server for verification.
    """
    signed_at = _now_iso()
    private_key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(agent_private_key_hex))
    payload = request_signing_payload(quote_id, agent_id, amount, mandate_id, signed_at)
    signature = private_key.sign(payload).hex()
    return signature, signed_at


class MandateVerificationResult:
    def __init__(self, valid: bool, reason: str, mandate: dict = None):
        self.valid = valid
        self.reason = reason
        self.mandate = mandate

    def to_dict(self):
        return {
            "valid": self.valid,
            "reason": self.reason,
            "mandate_id": self.mandate["mandate_id"] if self.mandate else None,
        }


def verify_purchase_mandate(
    mandate_id: str,
    agent_id: str,
    quote_id: str,
    amount: float,
    signature_hex: str,
    signed_at: str,
    conn: sqlite3.Connection,
) -> MandateVerificationResult:
    """
    Full verification chain for a mandate-backed purchase request. Runs
    BEFORE gate.evaluate_purchase() -- a request that fails mandate
    verification never reaches the spend-cap/stock logic at all.
    """
    row = conn.execute(
        "SELECT * FROM mandates WHERE mandate_id = ?", (mandate_id,)
    ).fetchone()
    if row is None:
        return MandateVerificationResult(False, "mandate_not_found")
    mandate = dict(row)

    if mandate["revoked"]:
        return MandateVerificationResult(False, "mandate_revoked", mandate)

    if mandate["agent_id"] != agent_id:
        return MandateVerificationResult(False, "mandate_agent_mismatch", mandate)

    now = datetime.now(timezone.utc)
    if now > _parse_iso(mandate["expires_at"]):
        return MandateVerificationResult(False, "mandate_expired", mandate)

    # Replay protection: reject signed requests whose timestamp is too old,
    # independent of whether the signature itself is valid.
    try:
        signed_at_dt = _parse_iso(signed_at)
    except (ValueError, TypeError):
        return MandateVerificationResult(False, "invalid_signed_at_format", mandate)

    age_seconds = (now - signed_at_dt).total_seconds()
    if age_seconds > REQUEST_SIGNATURE_MAX_AGE_SECONDS:
        return MandateVerificationResult(False, "signed_request_too_old", mandate)
    if age_seconds < -30:  # allow small clock skew, reject anything meaningfully "from the future"
        return MandateVerificationResult(False, "signed_request_timestamp_in_future", mandate)

    if amount > mandate["max_amount"]:
        return MandateVerificationResult(False, "amount_exceeds_mandate_scope", mandate)

    try:
        public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(mandate["agent_public_key"]))
        payload = request_signing_payload(quote_id, agent_id, amount, mandate_id, signed_at)
        public_key.verify(bytes.fromhex(signature_hex), payload)
    except (InvalidSignature, ValueError):
        return MandateVerificationResult(False, "invalid_signature", mandate)

    return MandateVerificationResult(True, "ok", mandate)
