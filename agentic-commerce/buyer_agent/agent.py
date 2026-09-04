"""
buyer_agent/agent.py

The AI buyer agent CLI. Talks to the merchant service exclusively over
HTTP (never touches the DB directly) — this is the honest simulation of a
separate agent-to-agent commerce participant.

Design notes:
- The client-side budget check below (`quantity * price <= budget`) is a
  COURTESY / early-exit only, so the agent doesn't waste a round trip on an
  obviously-doomed request. It is NOT the real enforcement — the merchant's
  gate.py is the only source of truth for spend limits, and it re-checks
  everything server-side regardless of what this client claims.
- LLM use for free-text goal parsing (--goal) is isolated in its own
  function below (parse_goal_to_product), is entirely optional (falls back
  to naive keyword matching if ANTHROPIC_API_KEY is unset or the call
  fails), and never influences whether a purchase is approved — its only
  possible output is "which existing catalog product_id to request," and
  that output is validated against the real catalog before use, so even a
  malformed or hallucinated response can't send a request for a
  nonexistent product. gate.py never sees or consults this function.
"""

import argparse
import json
import os
import sys

import requests

MERCHANT_API_BASE_URL = os.environ.get("MERCHANT_API_BASE_URL", "http://127.0.0.1:8000")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")


# ---------------------------------------------------------------------------
# Optional, isolated AI-assisted goal parsing.
# This function NEVER touches money logic — it only maps free text to a
# product_id, and its output is validated against the real catalog before
# use. If not used (--product provides the keyword directly) or if
# ANTHROPIC_API_KEY is unset, this falls back to naive keyword matching.
# ---------------------------------------------------------------------------
def parse_goal_to_product(goal_text: str, catalog: list) -> dict:
    """
    Maps a free-text goal to a specific product from the real catalog.

    If ANTHROPIC_API_KEY is set, calls the Claude API with the actual
    catalog (names + ids) and asks it to pick the single best-matching
    product_id, or "NONE" if nothing genuinely fits. The response is
    validated against the real catalog's product_ids before being trusted
    -- an LLM output that names a product_id not actually in the catalog,
    or any malformed/unparseable response, is treated as a miss and falls
    through to the naive keyword fallback below, exactly as if no API key
    were configured. This function has no path to influence approval
    logic -- it only ever returns a product dict (or None), for the
    caller to then request a quote for through the normal flow.

    If ANTHROPIC_API_KEY is unset, or the API call fails for any reason
    (network, auth, rate limit, etc.), falls back to naive substring
    keyword matching against product names/ids -- the CLI remains fully
    functional with zero external dependencies when no key is configured.
    """
    if ANTHROPIC_API_KEY:
        product = _parse_goal_via_claude(goal_text, catalog)
        if product is not None:
            return product
        print("[buyer_agent] Claude goal-parsing returned no confident match or failed — falling back to keyword matching.")

    return _parse_goal_via_keyword(goal_text, catalog)


def _parse_goal_via_claude(goal_text: str, catalog: list) -> dict | None:
    catalog_summary = "\n".join(
        f"- {p['product_id']}: {p['name']} ({p['price']} {p['currency']}, stock={p['stock']})"
        for p in catalog
    )
    prompt = (
        "You are matching a buyer's free-text goal to a single product from a fixed catalog. "
        "Respond with ONLY the product_id that best matches, or the literal word NONE if "
        "nothing in the catalog reasonably fits the goal. Do not explain your answer, do not "
        "add punctuation, do not invent a product_id that isn't listed below.\n\n"
        f"Catalog:\n{catalog_summary}\n\n"
        f"Buyer's goal: \"{goal_text}\"\n\n"
        "Answer (product_id or NONE):"
    )
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 20,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        answer = "".join(
            block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
        ).strip()
    except Exception as e:
        print(f"[buyer_agent] Claude API call failed ({type(e).__name__}: {e}) — falling back to keyword matching.")
        return None

    if answer.upper() == "NONE":
        return None

    # Validate against the REAL catalog -- never trust the model's output
    # blindly. A hallucinated or malformed product_id is treated as a miss.
    catalog_by_id = {p["product_id"]: p for p in catalog}
    if answer in catalog_by_id:
        print(f"[buyer_agent] Claude matched goal to: {catalog_by_id[answer]['name']} ({answer})")
        return catalog_by_id[answer]

    print(f"[buyer_agent] Claude returned '{answer}', which is not a real product_id — falling back to keyword matching.")
    return None


def _parse_goal_via_keyword(goal_text: str, catalog: list) -> dict | None:
    """
    Naive fallback: scores each catalog product by how many words it
    shares with the goal text, and returns the best-scoring match (ties
    broken by catalog order). This is a real word-overlap heuristic, not
    pure substring matching -- "I need a keyboard" correctly matches
    "Wireless Mechanical Keyboard" because "keyboard" overlaps, even
    though the full goal string isn't a substring of the product name.
    """
    goal_words = set(goal_text.lower().split())
    best_product = None
    best_score = 0
    for product in catalog:
        product_words = set(product["name"].lower().split()) | {product["product_id"].lower()}
        score = len(goal_words & product_words)
        if score > best_score:
            best_score = score
            best_product = product
    return best_product if best_score > 0 else None


# ---------------------------------------------------------------------------
# Merchant API calls
# ---------------------------------------------------------------------------
def fetch_catalog():
    resp = requests.get(f"{MERCHANT_API_BASE_URL}/catalog", timeout=10)
    resp.raise_for_status()
    return resp.json()


def fetch_related_products(product_id: str):
    resp = requests.get(f"{MERCHANT_API_BASE_URL}/catalog/{product_id}/related", timeout=10)
    if resp.status_code != 200:
        return []
    return resp.json()


def find_matching_product(catalog, keyword: str):
    keyword = keyword.lower()
    for product in catalog:
        if keyword in product["name"].lower() or keyword in product["product_id"].lower():
            return product
    return None


def request_quote(product_id: str, quantity: int):
    resp = requests.post(
        f"{MERCHANT_API_BASE_URL}/quote",
        json={"product_id": product_id, "quantity": quantity},
        timeout=10,
    )
    return resp.status_code, resp.json()


def request_purchase(
    quote_id: str,
    agent_id: str,
    agent_secret: str,
    simulate_razorpay_failure: bool = False,
    mandate_id: str = None,
    agent_key_path: str = None,
    amount: float = None,
):
    """
    If mandate_id and agent_key_path are both provided, signs this
    specific purchase intent (quote_id, agent_id, amount, mandate_id,
    timestamp) with the agent's own private key before sending -- see
    merchant_service/mandate.py for what the merchant verifies this
    against. Requires `amount` (the quote's total_price) since that's
    part of what gets signed; the merchant independently re-derives the
    amount from its own quote record and rejects if it doesn't match
    what was signed, so this CLI can't under-report an amount to itself.
    """
    body = {
        "quote_id": quote_id,
        "agent_id": agent_id,
        "simulate_razorpay_failure": simulate_razorpay_failure,
    }

    if mandate_id and agent_key_path:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "merchant_service"))
        import mandate as mandate_lib

        with open(agent_key_path, "r") as f:
            agent_private_key_hex = f.read().strip()

        signature, signed_at = mandate_lib.sign_purchase_request(
            quote_id, agent_id, amount, mandate_id, agent_private_key_hex
        )
        body["mandate_id"] = mandate_id
        body["signature"] = signature
        body["signed_at"] = signed_at
        print(f"[buyer_agent] Signed purchase intent with agent private key (mandate_id={mandate_id})")

    resp = requests.post(
        f"{MERCHANT_API_BASE_URL}/purchase",
        json=body,
        headers={"X-Agent-Secret": agent_secret},
        timeout=10,
    )
    return resp.status_code, resp.json()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser():
    parser = argparse.ArgumentParser(description="Agentic Commerce — Buyer Agent CLI")
    product_group = parser.add_mutually_exclusive_group(required=True)
    product_group.add_argument("--product", help="Keyword to match against the catalog (e.g. 'keyboard')")
    product_group.add_argument("--goal", help="Free-text goal, e.g. 'I want a docking station'")
    parser.add_argument(
        "--budget",
        type=float,
        required=True,
        help="Max amount this agent is willing to spend. ADVISORY ONLY: this is a local, "
             "client-side early-exit check so the agent doesn't waste a round trip on an "
             "obviously-doomed request. The merchant NEVER sees or trusts this value -- the "
             "server-side gate enforces the agent's real max_authorized_amount/daily_spend_cap "
             "regardless of what you pass here.",
    )
    parser.add_argument("--agent-id", required=True, help="Buyer agent ID, e.g. agent_high or agent_low")
    parser.add_argument(
        "--agent-secret",
        required=True,
        help="Shared secret for --agent-id, required by the merchant's X-Agent-Secret auth check. "
             "Printed by db/seed.py when the database is seeded.",
    )
    parser.add_argument("--quantity", type=int, default=1, help="Quantity to purchase (default: 1)")
    parser.add_argument(
        "--repeat",
        type=str,
        metavar="QUOTE_ID",
        help="Re-run /purchase against an already-used quote_id to demonstrate idempotency",
    )
    parser.add_argument(
        "--simulate-razorpay-failure",
        action="store_true",
        help="Force the merchant to simulate a Razorpay infra failure for this purchase",
    )
    parser.add_argument(
        "--mandate-id",
        help="AP2-inspired delegated mandate ID (printed by db/generate_agent_keys.py). "
             "If provided, --agent-key is also required.",
    )
    parser.add_argument(
        "--agent-key",
        help="Path to this agent's Ed25519 private key file (e.g. buyer_agent/keys/agent_high.key), "
             "used to sign the purchase request when --mandate-id is provided.",
    )
    return parser


def print_outcome(txn: dict, idempotent: bool = False):
    status = txn.get("status")
    print("\n" + "=" * 60)
    if idempotent:
        print(f"OUTCOME: IDEMPOTENT DUPLICATE — returned existing transaction")
    elif status == "completed":
        print(f"OUTCOME: COMPLETED ✅")
    elif status == "declined":
        print(f"OUTCOME: DECLINED ❌  reason: {txn.get('decline_reason')}")
    elif status == "failed":
        print(f"OUTCOME: FAILED (infra) ⚠️")
    else:
        print(f"OUTCOME: {status}")
    print(f"  txn_id:            {txn.get('txn_id')}")
    print(f"  quote_id:          {txn.get('quote_id')}")
    print(f"  agent_id:          {txn.get('agent_id')}")
    print(f"  amount:            {txn.get('amount')}")
    print(f"  razorpay_order_id: {txn.get('razorpay_order_id')}")
    print("=" * 60 + "\n")


def main():
    parser = build_parser()
    args = parser.parse_args()

    if bool(args.mandate_id) != bool(args.agent_key):
        print("[buyer_agent] --mandate-id and --agent-key must be provided together, or not at all.")
        sys.exit(1)

    # --- Idempotency demo path: skip catalog/quote, just re-fire /purchase ---
    if args.repeat:
        print(f"[buyer_agent] Re-submitting /purchase for existing quote_id={args.repeat} (idempotency demo)")
        # Note: re-signing for --repeat would need the original amount; since
        # this path exists purely to demonstrate idempotency (the merchant
        # returns the existing transaction regardless), it intentionally
        # does not re-attach a mandate signature.
        status_code, body = request_purchase(args.repeat, args.agent_id, args.agent_secret)
        print(f"[buyer_agent] HTTP {status_code}")
        if status_code >= 400:
            print(f"[buyer_agent] Error response: {body}")
            sys.exit(1)
        print_outcome(body["transaction"], idempotent=body.get("idempotent", False))
        return

    print(f"[buyer_agent] Fetching catalog from {MERCHANT_API_BASE_URL} ...")
    catalog = fetch_catalog()

    if args.goal:
        print(f"[buyer_agent] Parsing goal: \"{args.goal}\"" + (" (using Claude API)" if ANTHROPIC_API_KEY else " (ANTHROPIC_API_KEY not set — using keyword fallback)"))
        product = parse_goal_to_product(args.goal, catalog)
    else:
        product = find_matching_product(catalog, args.product)

    if product is None:
        keyword_desc = args.goal or args.product
        print(f"[buyer_agent] No product in catalog matched '{keyword_desc}'. Aborting.")
        sys.exit(1)

    print(f"[buyer_agent] Matched product: {product['name']} ({product['product_id']}) — {product['price']} {product['currency']}")

    related = fetch_related_products(product["product_id"])
    if related:
        related_desc = ", ".join(f"{r['name']} ({r['product_id']})" for r in related)
        print(f"[buyer_agent] You might also consider: {related_desc}")

    # --- Client-side courtesy check only. This is ADVISORY ONLY: the
    # merchant never sees --budget and does not enforce it. The gate
    # enforces the agent's real max_authorized_amount / daily_spend_cap
    # regardless of what's passed here. ---
    estimated_total = product["price"] * args.quantity
    if estimated_total > args.budget:
        print(
            f"[buyer_agent] ADVISORY client-side check failed: estimated total {estimated_total} "
            f"exceeds your stated --budget {args.budget}. Aborting locally before requesting a "
            f"quote. (This --budget value is NEVER sent to or enforced by the merchant -- it only "
            f"controls whether THIS CLI bothers making the request. The merchant enforces "
            f"agent_id's real spend caps independently, on the server side.)"
        )
        sys.exit(1)
    print(
        f"[buyer_agent] ADVISORY client-side check passed ({estimated_total} <= --budget {args.budget}). "
        f"Proceeding — final approval is still entirely up to the merchant's server-side gate."
    )

    print(f"[buyer_agent] Requesting quote for {args.quantity}x {product['product_id']} ...")
    status_code, quote = request_quote(product["product_id"], args.quantity)
    if status_code >= 400:
        print(f"[buyer_agent] Quote request failed: HTTP {status_code} — {quote}")
        sys.exit(1)
    print(f"[buyer_agent] Quote received: quote_id={quote['quote_id']}, total_price={quote['total_price']}, expires_at={quote['expires_at']}")

    print(f"[buyer_agent] Requesting purchase as agent_id={args.agent_id} ...")
    status_code, body = request_purchase(
        quote["quote_id"],
        args.agent_id,
        args.agent_secret,
        simulate_razorpay_failure=args.simulate_razorpay_failure,
        mandate_id=args.mandate_id,
        agent_key_path=args.agent_key,
        amount=quote["total_price"],
    )
    if status_code >= 400:
        print(f"[buyer_agent] Purchase request failed: HTTP {status_code} — {body}")
        sys.exit(1)

    print_outcome(body["transaction"], idempotent=body.get("idempotent", False))
    print(f"[buyer_agent] To demonstrate idempotency, re-run with: --repeat {quote['quote_id']} --agent-id {args.agent_id} --agent-secret {args.agent_secret}")


if __name__ == "__main__":
    main()
