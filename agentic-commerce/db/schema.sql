-- Agentic Commerce Demo — SQLite schema
-- All timestamps are stored as ISO 8601 UTC strings (e.g. 2026-08-24T14:09:00Z)
-- for consistency across quotes, transactions, and audit_log.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- products
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS products (
    product_id  TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    price       REAL NOT NULL CHECK (price >= 0),
    currency    TEXT NOT NULL DEFAULT 'INR',
    stock       INTEGER NOT NULL CHECK (stock >= 0)
);

-- ---------------------------------------------------------------------------
-- buyer_agents
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS buyer_agents (
    agent_id             TEXT PRIMARY KEY,
    name                 TEXT NOT NULL,
    max_authorized_amount REAL NOT NULL CHECK (max_authorized_amount >= 0),
    daily_spend_cap      REAL NOT NULL CHECK (daily_spend_cap >= 0),
    shared_secret        TEXT NOT NULL DEFAULT ''
    -- Static per-agent shared secret checked against the X-Agent-Secret
    -- header on POST /purchase, so a caller can't impersonate an agent
    -- just by knowing its agent_id. Plaintext storage is a known,
    -- documented limitation for this demo scope -- see README's
    -- "Security Considerations" section.
);

-- ---------------------------------------------------------------------------
-- quotes
-- Locks a price for 2 minutes from creation. `consumed` flips to 1 the
-- moment a transaction is approved off this quote (belt-and-suspenders
-- alongside the UNIQUE(quote_id) constraint on transactions below).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS quotes (
    quote_id     TEXT PRIMARY KEY,
    product_id   TEXT NOT NULL REFERENCES products(product_id),
    quantity     INTEGER NOT NULL CHECK (quantity > 0),
    total_price  REAL NOT NULL CHECK (total_price >= 0),
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    consumed     INTEGER NOT NULL DEFAULT 0 CHECK (consumed IN (0, 1))
);

-- ---------------------------------------------------------------------------
-- transactions
-- status is constrained to the 6 valid lifecycle states.
-- UNIQUE(quote_id) is the DB-level idempotency guarantee: it must survive a
-- race between two near-simultaneous /purchase calls for the same quote,
-- not just an application-level check.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS transactions (
    txn_id             TEXT PRIMARY KEY,
    quote_id           TEXT NOT NULL REFERENCES quotes(quote_id),
    agent_id           TEXT NOT NULL REFERENCES buyer_agents(agent_id),
    amount             REAL NOT NULL CHECK (amount >= 0),
    status             TEXT NOT NULL CHECK (status IN (
                            'pending_gate',
                            'declined',
                            'approved',
                            'payment_pending',
                            'completed',
                            'failed'
                        )),
    decline_reason     TEXT,
    razorpay_order_id  TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    UNIQUE (quote_id)
);

-- ---------------------------------------------------------------------------
-- audit_log
-- Append-only. Every gate decision and payment event is logged here,
-- including approvals, so the full reasoning trail is always visible.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
    log_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    txn_id     TEXT NOT NULL,
    step       TEXT NOT NULL,
    detail     TEXT NOT NULL,  -- JSON-serialized dict. NEVER put API keys here.
    timestamp  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_log_txn_id ON audit_log (txn_id);

-- ---------------------------------------------------------------------------
-- mandates
-- AP2-inspired delegated payment authorization. A principal (the human
-- who owns the money) issues a Mandate binding a specific agent's Ed25519
-- public key to a spending scope (max_amount, expiry). This is stored
-- here as the merchant's trusted record of that authorization -- the
-- merchant verifies against ITS OWN stored copy, never against anything
-- the caller asserts about itself at request time.
--
-- Per-purchase, the agent signs the SPECIFIC request (quote_id, amount,
-- timestamp) with its own private key; the merchant verifies that
-- signature against agent_public_key here (see merchant_service/mandate.py).
-- This two-layer design -- one signature authorizing a spending scope,
-- a second signature proving a specific request genuinely came from the
-- holder of that scope -- mirrors AP2's Mandate architecture in spirit,
-- without claiming wire-format compatibility with the real protocol.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mandates (
    mandate_id            TEXT PRIMARY KEY,
    agent_id              TEXT NOT NULL REFERENCES buyer_agents(agent_id),
    principal_id          TEXT NOT NULL,
    agent_public_key      TEXT NOT NULL,  -- hex-encoded Ed25519 public key
    principal_public_key  TEXT NOT NULL,  -- hex-encoded Ed25519 public key, for audit
    principal_signature   TEXT NOT NULL,  -- hex-encoded signature over the binding below
    max_amount            REAL NOT NULL CHECK (max_amount >= 0),
    currency              TEXT NOT NULL DEFAULT 'INR',
    issued_at             TEXT NOT NULL,
    expires_at            TEXT NOT NULL,
    revoked               INTEGER NOT NULL DEFAULT 0 CHECK (revoked IN (0, 1))
);

CREATE INDEX IF NOT EXISTS idx_mandates_agent_id ON mandates (agent_id);
