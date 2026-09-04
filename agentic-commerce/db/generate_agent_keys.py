"""
db/generate_agent_keys.py

Sets up the cryptographic material for the delegated-mandate demo:
- Generates an Ed25519 keypair for a demo "principal" (the human/operator
  authorizing spending) and for each buyer agent.
- Issues a signed Mandate binding each agent's public key to a spending
  scope (max_amount = the agent's existing max_authorized_amount, for a
  sensible default), inserts it into the `mandates` table.
- Writes each agent's PRIVATE key to buyer_agent/keys/<agent_id>.key --
  these are demo secrets, gitignored, and analogous to how a real agent
  would hold its own signing key locally, never sharing it with the
  merchant.

Safe to re-run: pass --force to reissue mandates for agents that already
have one; otherwise existing non-revoked mandates are left alone.
"""

import os
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "merchant_service"))
import mandate as mandate_lib

DB_PATH = os.environ.get("DATABASE_PATH", os.path.join(os.path.dirname(__file__), "merchant.db"))
KEYS_DIR = os.path.join(os.path.dirname(__file__), "..", "buyer_agent", "keys")

MANDATE_VALIDITY_DAYS = 30
PRINCIPAL_ID = "demo_principal_1"


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def issue_mandate_for_agent(conn, agent_id: str, max_amount: float, force: bool = False):
    existing = conn.execute(
        "SELECT * FROM mandates WHERE agent_id = ? AND revoked = 0", (agent_id,)
    ).fetchone()
    if existing and not force:
        print(f"  {agent_id}: already has an active mandate ({existing['mandate_id']}) -- skipping (use --force to reissue)")
        return

    os.makedirs(KEYS_DIR, exist_ok=True)

    # Agent keypair -- private key stays local to the agent, public key goes to the merchant.
    agent_private_hex, agent_public_hex = mandate_lib.generate_keypair()
    key_path = os.path.join(KEYS_DIR, f"{agent_id}.key")
    with open(key_path, "w") as f:
        f.write(agent_private_hex)
    os.chmod(key_path, 0o600)

    # Principal keypair -- reused across agents in this demo (one "human" authorizing multiple agents).
    principal_private_hex, principal_public_hex = mandate_lib.generate_keypair()

    mandate_id = f"mandate_{uuid.uuid4().hex[:10]}"
    issued_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    expires_at = (datetime.now(timezone.utc) + timedelta(days=MANDATE_VALIDITY_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    currency = "INR"

    signature = mandate_lib.sign_mandate(
        agent_id, agent_public_hex, max_amount, currency, expires_at, principal_private_hex
    )

    if existing:
        conn.execute("UPDATE mandates SET revoked = 1 WHERE mandate_id = ?", (existing["mandate_id"],))

    conn.execute(
        "INSERT INTO mandates (mandate_id, agent_id, principal_id, agent_public_key, "
        "principal_public_key, principal_signature, max_amount, currency, issued_at, "
        "expires_at, revoked) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
        (mandate_id, agent_id, PRINCIPAL_ID, agent_public_hex, principal_public_hex,
         signature, max_amount, currency, issued_at, expires_at),
    )
    conn.commit()

    print(f"  {agent_id}: issued mandate {mandate_id}")
    print(f"    max_amount={max_amount} {currency}, expires={expires_at}")
    print(f"    agent private key written to: {key_path}")


if __name__ == "__main__":
    force = "--force" in sys.argv
    conn = get_connection()

    print("Issuing delegated-payment mandates for all seeded agents...\n")
    agents = conn.execute("SELECT agent_id, max_authorized_amount FROM buyer_agents").fetchall()
    if not agents:
        print("No buyer_agents found -- run db/seed.py first.")
        sys.exit(1)

    for agent in agents:
        issue_mandate_for_agent(conn, agent["agent_id"], agent["max_authorized_amount"], force=force)

    print("\nTo use a mandate with the buyer agent CLI:")
    print("  python3 buyer_agent/agent.py --product keyboard --budget 10000 \\")
    print("    --agent-id agent_high --agent-secret <secret> \\")
    print("    --mandate-id <mandate_id printed above> --agent-key buyer_agent/keys/agent_high.key")

    conn.close()
