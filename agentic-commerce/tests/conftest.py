"""
tests/conftest.py

Shared pytest fixtures. Every test uses a throwaway SQLite DB created fresh
in a pytest tmp_path — the real seeded demo DB (db/merchant.db) is never
touched by the test suite.
"""

import importlib
import os
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_DIR = os.path.join(ROOT, "db")
MERCHANT_DIR = os.path.join(ROOT, "merchant_service")

for p in (DB_DIR, MERCHANT_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

SCHEMA_PATH = os.path.join(DB_DIR, "schema.sql")

# Test fixture data — deliberately different IDs from the real seed data so
# a bug that accidentally pointed at the real DB would be obvious.
TEST_PRODUCTS = [
    ("prod_test_instock", "Test Widget (In Stock)", 1000.0, "INR", 10),
    ("prod_test_oos", "Test Widget (Out of Stock)", 500.0, "INR", 0),
    ("prod_test_laststock", "Test Widget (Last Unit)", 100.0, "INR", 1),
]

TEST_AGENT_SECRETS = {
    "agent_test_high": "test-secret-high",
    "agent_test_low": "test-secret-low",
}

TEST_AGENTS = [
    # per-txn cap 5000, daily cap 6000 -- daily cap is the tighter constraint
    ("agent_test_high", "Test Agent High", 5000.0, 6000.0, TEST_AGENT_SECRETS["agent_test_high"]),
    # per-txn cap 200 -- tight, for per-transaction-cap decline tests
    ("agent_test_low", "Test Agent Low", 200.0, 1000.0, TEST_AGENT_SECRETS["agent_test_low"]),
]


@pytest.fixture()
def test_db_path(tmp_path):
    """Creates a fresh throwaway SQLite DB file with schema + minimal seed data."""
    db_file = tmp_path / "test_merchant.db"
    conn = sqlite3.connect(str(db_file))
    with open(SCHEMA_PATH, "r") as f:
        conn.executescript(f.read())
    conn.executemany(
        "INSERT INTO products (product_id, name, price, currency, stock) VALUES (?, ?, ?, ?, ?)",
        TEST_PRODUCTS,
    )
    conn.executemany(
        "INSERT INTO buyer_agents (agent_id, name, max_authorized_amount, daily_spend_cap, shared_secret) "
        "VALUES (?, ?, ?, ?, ?)",
        TEST_AGENTS,
    )
    conn.commit()
    conn.close()
    return str(db_file)


@pytest.fixture()
def helpers(test_db_path, monkeypatch):
    """
    Returns the db.helpers module, reloaded so its module-level DB_PATH
    points at the throwaway test DB rather than the real seeded DB.
    """
    monkeypatch.setenv("DATABASE_PATH", test_db_path)
    import helpers as h
    importlib.reload(h)  # re-reads DATABASE_PATH from the now-patched env
    return h


@pytest.fixture()
def gate(helpers):
    """
    Returns the gate module. Reloaded after `helpers` so gate's internal
    `import helpers as db` reference is guaranteed fresh (reload updates
    the same cached module object in sys.modules, so this is actually
    belt-and-suspenders, but explicit is better than relying on import
    caching order).
    """
    import gate as g
    importlib.reload(g)
    return g


@pytest.fixture()
def conn(helpers, test_db_path):
    c = helpers.get_connection(test_db_path)
    yield c
    c.close()


@pytest.fixture()
def api_client(helpers, gate, monkeypatch, test_db_path):
    """
    A FastAPI TestClient wired to the throwaway test DB. Used for tests
    that need to exercise the real HTTP layer (unknown-agent 404 handling,
    end-to-end failure-path behavior) rather than calling helpers/gate
    functions directly.
    """
    monkeypatch.setenv("DATABASE_PATH", test_db_path)
    import razorpay_client
    importlib.reload(razorpay_client)
    import main as merchant_main
    importlib.reload(merchant_main)

    from fastapi.testclient import TestClient

    return TestClient(merchant_main.app)


def auth_headers(agent_id: str) -> dict:
    """X-Agent-Secret header for the given test agent_id, for use with api_client."""
    return {"X-Agent-Secret": TEST_AGENT_SECRETS[agent_id]}


@pytest.fixture()
def mandate_setup(helpers, conn):
    """
    Generates a real Ed25519 keypair for agent_test_high and a matching
    active mandate row in the test DB. Returns a dict with mandate_id,
    agent_private_key_hex, and the mandate's max_amount, for tests to
    build signed purchase requests against.
    """
    import mandate as mandate_lib
    import uuid
    from datetime import datetime, timedelta, timezone

    agent_private_hex, agent_public_hex = mandate_lib.generate_keypair()
    principal_private_hex, principal_public_hex = mandate_lib.generate_keypair()

    mandate_id = f"mandate_test_{uuid.uuid4().hex[:8]}"
    issued_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    expires_at = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    max_amount = 5000.0
    currency = "INR"

    signature = mandate_lib.sign_mandate(
        "agent_test_high", agent_public_hex, max_amount, currency, expires_at, principal_private_hex
    )

    conn.execute(
        "INSERT INTO mandates (mandate_id, agent_id, principal_id, agent_public_key, "
        "principal_public_key, principal_signature, max_amount, currency, issued_at, "
        "expires_at, revoked) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
        (mandate_id, "agent_test_high", "test_principal", agent_public_hex, principal_public_hex,
         signature, max_amount, currency, issued_at, expires_at),
    )
    conn.commit()

    return {
        "mandate_id": mandate_id,
        "agent_id": "agent_test_high",
        "agent_private_key_hex": agent_private_hex,
        "max_amount": max_amount,
    }
