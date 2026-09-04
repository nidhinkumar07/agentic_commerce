# Agentic Commerce Demo
### Razorpay "AI Growth & Agentic Commerce" Track — Hybrid Buyer Agent + Merchant Endpoint

## Problem Statement

Agent-to-agent commerce needs a merchant side that can say no. As AI buyer
agents get delegated real spending authority, the critical trust question
isn't "can an agent buy something" — it's "can the merchant prove, after
the fact, exactly why a purchase was approved or declined, with zero
chance of a duplicate charge or a stuck payment." This project is a
minimal but complete round trip: a buyer agent discovers a product, gets a
locked quote, requests a purchase, and a deterministic merchant gate
approves or declines it against real spend caps and stock — with a
real Razorpay test-mode payment on approval and a full, queryable audit
trail at every step.

## Architecture

```
                     ┌─────────────────────┐
                     │   Buyer Agent CLI    │
                     │  (buyer_agent/)      │
                     │  - optional goal     │
                     │    parsing (isolated)│
                     │  - client budget     │
                     │    courtesy check    │
                     └──────────┬───────────┘
                                │ HTTP
                                ▼
                     ┌─────────────────────┐        ┌──────────────────┐
                     │  Merchant FastAPI    │───────▶│  gate.py         │
                     │  (merchant_service/) │        │  NO AI. Rules-   │
                     │  /catalog /quote     │◀───────│  based only.     │
                     │  /purchase /audit    │        └──────────────────┘
                     └──────────┬───────────┘
                                │
                    ┌───────────┼────────────┐
                    ▼                        ▼
          ┌──────────────────┐   ┌───────────────────────┐
          │  db/helpers.py    │   │  razorpay_client.py   │
          │  (all SQL lives   │   │  (isolated Razorpay    │
          │  here)            │   │  test-mode SDK calls)  │
          └─────────┬─────────┘   └───────────────────────┘
                    ▼
          ┌──────────────────┐
          │  SQLite           │
          │  products,        │
          │  buyer_agents,    │
          │  quotes,          │
          │  transactions,    │
          │  audit_log        │
          └──────────────────┘
                    ▲
                    │ reads via same FastAPI endpoints
          ┌──────────────────┐
          │  Streamlit         │
          │  Dashboard         │
          │  (dashboard/)      │
          │  - catalog view    │
          │  - run agent       │
          │  - transactions    │
          │  - audit drill-down│
          │  - growth metrics  │
          └──────────────────┘
```

## Growth Metrics

`GET /metrics` and the dashboard's "📈 Growth Metrics" tab surface real,
computed-from-actual-data business numbers: GMV completed/attempted,
completion rate, average order value, and a breakdown of *why* money
didn't move (declines by policy reason, failures by infra reason). Nothing
here is estimated or simulated — it's a direct SQL aggregation over the
`transactions` table. This exists because a merchant-facing "growth" story
needs to answer "how much, and why not more," not just "it works."

**Known gap, stated plainly**: the related-products cross-sell surface
(`GET /catalog/{id}/related`, shown in both the CLI and dashboard before a
purchase) is not yet instrumented for attach-rate measurement — there's no
tracking of whether a shown suggestion led to a purchase. Building that
properly needs an impressions log correlated to subsequent purchases,
which is a real, scoped piece of future work, not a hidden claim.

## Delegated Payment Authorization (AP2-Inspired Mandates)

The original build only created Razorpay orders — it never modeled how an
agent's authority to spend actually gets established and proven. This
section closes that gap with a real, tested, two-layer cryptographic
signing scheme modeled on the actual architecture of Google's [Agent
Payments Protocol (AP2)](https://ap2-protocol.org/) — specifically its
stated core principle, "verifiable intent, not inferred action," via
signed Mandates — and informed by how Razorpay's own real agentic-payments
infrastructure (built on **UPI Reserve Pay**, which blocks funds upfront
and debits as value is delivered) frames consent-based, pre-authorized
agent spending.

**Two distinct signatures, not one:**

1. **Principal Mandate** (`db/generate_agent_keys.py`, issued once): the
   human/principal generates an Ed25519 keypair for each buyer agent and
   signs a binding of `{agent_id, agent's public key, max_amount,
   currency, expiry}` with their own key. This is stored server-side in
   the `mandates` table — the merchant trusts its own verified record of
   this authorization, never a claim the caller makes about itself at
   request time.
2. **Per-request agent signature** (checked on every `POST /purchase`
   that includes a `mandate_id`): the agent signs the *specific* purchase
   intent — `{quote_id, agent_id, amount, mandate_id, signed_at}` — with
   its own private key. The merchant verifies this against the
   `agent_public_key` stored in the matching mandate, checks the mandate
   isn't expired or revoked, checks the amount is within the mandate's
   authorized scope, and rejects signed requests older than 5 minutes
   (replay protection).

This is **strictly additional** to the existing `X-Agent-Secret` header —
not a replacement. `mandate_id` is optional on `/purchase`; when present,
every check above runs *before* `gate.evaluate_purchase()` is ever called.

**Real, tested guarantees** (see `tests/test_mandate.py`, 7 tests): a
signature for one amount can't be replayed onto a different amount or
quote; a mandate can't authorize more than its own `max_amount`; signing
with the wrong keypair is rejected; a 10-minute-old signed request is
rejected even if the signature itself is valid.

**Try it yourself:**
```bash
python3 db/generate_agent_keys.py   # prints mandate_id per agent, writes keys to buyer_agent/keys/
python3 buyer_agent/agent.py --product keyboard --budget 10000 \
  --agent-id agent_high --agent-secret <printed by seed.py> \
  --mandate-id <printed above> --agent-key buyer_agent/keys/agent_high.key
```

**Honest scope statement**: this demonstrates the cryptographic
delegation *pattern* end-to-end, fully real and independently verifiable
(Ed25519 signing/verification, no mocked crypto). It does **not** integrate
with any real bank rail, UPI mandate, or NPCI infrastructure — Reserve
Pay, UPI Circle, and NPCI's proposed Unified Agent Protocol all require
banking-partner access this project doesn't have. The gap this closes is
specifically the one raised in review: showing *how* an agent
cryptographically proves a delegated payment, not claiming production
integration with a live payment rail.

## Settlement Confirmation (Webhooks)

The original build treated a successful `client.order.create()` response
as if it meant the payment was complete — it doesn't. Creating an order
and a customer actually paying it are different events, and in production
Razorpay confirms the latter via a signed webhook, not a synchronous
response to order creation.

`POST /webhooks/razorpay` (see `merchant_service/webhook_utils.py` +
`main.py`) implements the real pattern: verifies `X-Razorpay-Signature`
as an HMAC-SHA256 of the **raw** request body (verified before any JSON
parsing, since re-serializing parsed JSON isn't guaranteed to reproduce
the exact signed bytes) against `RAZORPAY_WEBHOOK_SECRET`, then processes
`payment.captured` / `payment.failed` events idempotently — a webhook
delivered twice (Razorpay retries by design) is a documented no-op the
second time, not a double stock-decrement.

**Design choice, stated plainly**: the existing synchronous path (order
created → immediately treated as settled) remains the *primary* way a
transaction reaches `completed` in this demo, because Razorpay's real
servers can't reach a webhook on `localhost` without a public tunnel,
which isn't reasonable to require for a local pitch demo. The webhook is
a genuine, fully-tested *secondary* reconciliation path — verified with 5
tests (`tests/test_webhook.py`) covering signature rejection, valid
processing, idempotent duplicate delivery, and unknown-order handling. In
a real deployment with a public endpoint, the webhook would typically be
the *only* authoritative source of truth, and the synchronous shortcut
would be removed.

## How to Run

```bash
./run.sh
```

This single command creates a virtualenv, installs `requirements.txt`,
creates `.env` from `.env.example` if missing, seeds the database (safely
skipped if data already exists), issues delegated-payment mandates for
each agent (Ed25519 keypairs + signed authorizations — see "Delegated
Payment Authorization" above), and starts the merchant FastAPI service
on `http://127.0.0.1:8000`. It then prints two follow-up commands to run
in separate terminals:

```bash
# Terminal 2 — buyer agent CLI (secrets are printed by db/seed.py, or use
# the fixed demo values below)
source .venv/bin/activate
python3 buyer_agent/agent.py --product keyboard --budget 10000 \
  --agent-id agent_high --agent-secret demo-secret-high-8f2a1c

# Terminal 3 — dashboard
source .venv/bin/activate
streamlit run dashboard/app.py
```

**Before a real (non-simulated) purchase will complete**, open `.env` and
replace the placeholder `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` with your
own Razorpay **test-mode** keys from the [Razorpay Dashboard](https://dashboard.razorpay.com/)
(Settings → API Keys → Test Mode). Until then, real (non-simulated)
purchases will fail at the payment step — which is itself proof the
failure-handling path works correctly, not a bug.

Run the test suite with:
```bash
pytest -v
```

## AI Judgment: Where We Did and Didn't Use a Model

**Gate (`merchant_service/gate.py`) — NO AI.** This is the only place in
the system that decides whether real money moves. It is deliberately pure,
deterministic, rule-based Python: idempotency check → quote validity →
per-transaction cap → daily cap → stock check. The file has a comment
block at the top stating this explicitly. An LLM call here would make
approval decisions probabilistic and non-reproducible, which is
unacceptable on a payment-approval path — the same (quote, agent, DB
state) input must always yield the same decision, every time, under audit.

**Buyer agent's goal parsing (`buyer_agent/agent.py`) — AI-assisted,
optional, isolated, and genuinely wired to a real model.** The `--goal`
flag routes free text through `parse_goal_to_product()`, which calls the
real Claude API (`api.anthropic.com`, `claude-haiku-4-5-20251001` by
default) when `ANTHROPIC_API_KEY` is set in `.env`, asking it to pick the
single best-matching `product_id` from the actual live catalog. Two
safeguards keep this fully isolated from the money path:

1. **The model's output is validated against the real catalog before
   use.** A hallucinated, malformed, or nonexistent `product_id` is
   treated as a miss and falls through to the fallback below — it can
   never cause a request for something that doesn't exist.
2. **This function's only possible output is "which product to request a
   quote for."** It has no path to gate.py, no visibility into caps or
   stock decisions, and cannot approve or influence anything
   money-related. Even a fully compromised or adversarial model response
   here can, at worst, pick the wrong (but real) product to quote — never
   authorize a purchase.

If `ANTHROPIC_API_KEY` is unset, or the API call fails for any reason
(network, auth, rate limit), it falls back to a local word-overlap
heuristic (`_parse_goal_via_keyword`) with zero external dependencies —
verified live: an intentionally invalid API key produced a real `401`
from Anthropic's actual server (confirming the call genuinely reaches the
API, not just that the code compiles), then fell back correctly and
completed the rest of the flow normally.

**Honest caveat**: I was not able to test the *successful* Claude API
response path in the environment this was built in, since I didn't have
access to a real key to test against. The failure/fallback path is
proven live; the success path is implemented and should work with a real
key, but wasn't verified end-to-end with an actual model response before
shipping. Test this yourself with a real `ANTHROPIC_API_KEY` before
relying on it for a live demo.

**What this project actually is, stated plainly**: the core transaction
system — quote, gate, payment, audit trail — is a rules-based approval API
with a structured audit log, not an AI system. The only place a real
model touches this project at all is the optional natural-language layer
described above, which affects product selection, never money. "Agentic
commerce" here means "designed for autonomous callers to transact against
a trust boundary," not "AI makes the trust decisions" — which, for a
payment-approval system, is the correct design, not a compromise.

## What Broke and How We Fixed It

Two real issues surfaced during the build (documented honestly, as they
happened):

1. **Step 3 — import path bug.** The first attempt to launch `uvicorn`
   failed with `ModuleNotFoundError: No module named 'gate'`, because
   `gate.py`'s sibling import needed `merchant_service/` explicitly added
   to `sys.path` (Python doesn't auto-add a script's own directory when
   imported as part of a package path like `merchant_service.main:app`).
   Fixed by inserting that path in `main.py`.

2. **Step 4/5 — real, unplanned Razorpay failure.** During integration
   testing, an actual (non-simulated) call to the Razorpay SDK failed with
   a real `SSLCertVerificationError` (a build-environment network
   constraint, not a Razorpay-side issue). This wasn't the designed
   failure demo — it was a genuine unexpected error, and it's valuable
   evidence: the `except Exception` catch-all in `razorpay_client.py`
   caught it correctly, the transaction landed cleanly in `failed`, no API
   keys were logged anywhere (verified directly in the audit log), and no
   stock was decremented. This is a stronger proof of the failure path
   than the injected simulation alone, because it wasn't anticipated when
   the code was written.

3. **Post-Step-8 — a genuine concurrency bug, found by testing what the
   spec actually asked for.** The idempotency requirement said the
   `UNIQUE(quote_id)` constraint "must survive a race between two
   near-simultaneous requests" — up to that point it had only been proven
   with *sequential* inserts. Firing 8 truly concurrent `/purchase`
   requests at the same `quote_id` (via real threads hitting a live
   server, see `db/race_test.py`) surfaced a real bug: all 8 correctly
   deduped to a single transaction row (the DB constraint held), but
   because several requests could pass the gate's idempotency pre-check
   *before any row existed yet*, every one of them then independently ran
   the full approve → payment_pending → Razorpay-call → status-update
   pipeline on that same row — overwriting each other's status
   (`approved`/`failed`/`declined` all appeared for one `txn_id` across
   different responses) and, more seriously, each independently calling
   the Razorpay client for the same logical purchase. That's a real
   double-charge risk under concurrent duplicate submission.

   **Fix**: `db/helpers.py` gained `try_create_transaction()`, which
   returns `(transaction, created: bool)` instead of just the transaction.
   `merchant_service/main.py`'s `/purchase` handler now only runs the
   status-transition/payment pipeline when `created` is `True` — i.e.,
   only the one request that actually performed the insert. Every other
   concurrent duplicate immediately returns the current transaction state
   as an idempotent read, without touching it further. Re-running the same
   8-thread race after the fix confirmed exactly one execution of the
   payment pipeline per transaction. `pytest -v` was re-run afterward and
   still passes 8/8.

4. **Post-review — a real, silent overselling bug (external review
   finding, then verified and fixed).** `decrement_stock()` executed an
   `UPDATE ... WHERE stock >= ?` but never checked whether the UPDATE
   actually matched a row. If two transactions both passed their
   purchase-time stock check (a real possibility, since decrement only
   happens at `completed`, not at gate evaluation), the second
   transaction's decrement would silently affect 0 rows — yet the code
   still marked that transaction `completed` and returned a real Razorpay
   `order_id`, meaning money moved with no stock ever actually reserved.
   **Fix**: `decrement_stock()` now checks `cur.rowcount` and raises
   `StockRaceLostError` on 0 rows; `/purchase` catches this and fails the
   transaction to `stock_race_lost`, logging a loud audit warning that a
   Razorpay order was already created and needs manual reconciliation.
   Verified with both a deterministic unit test
   (`test_stock_race_is_caught_deterministically`) and a live
   concurrent-thread test against a real running server
   (`test_concurrent_purchase_on_last_unit_of_stock_does_not_oversell`).

5. **Post-review — a real daily-spend-cap race condition** (same
   external review, same TOCTOU pattern as #4 but on money instead of
   stock). `gate.py`'s daily-cap check reads today's spend, then a
   separate later step writes a new transaction that counts toward it.
   Verified this was a genuine gap by firing two truly concurrent
   `/purchase` requests for the same agent, each individually under the
   per-transaction cap but together over the daily cap — **both were
   approved**, confirmed live via `db/race_test.py` before any fix was
   applied. **Fix**: `/purchase` now wraps gate evaluation through the
   `payment_pending` write in a single `BEGIN IMMEDIATE` SQLite
   transaction, serializing the read-check-write sequence. Re-ran the
   identical concurrent scenario five times after the fix: every run
   showed exactly one approval and one correct `exceeds_daily_spend_cap`
   decline. This fix has a documented cost — see Security Considerations.

6. **Self-caught — a test script that polluted the real demo database.**
   `db/race_test.py` (a standalone diagnostic, not part of the app) ran
   its concurrency demo directly against the real `db/merchant.db`, using
   `RAZORPAY_FORCE_SUCCESS=1` to force payment successes for testing. This
   meant every run of that script left real-looking (but fake) completed
   and declined transactions sitting in the actual demo database —
   discovered when a later manual verification pass showed transaction
   counts and amounts that didn't match what had actually just been
   created, and tracing it back led to this script. **Fix**: it now
   creates and seeds an isolated temp SQLite file per run (via
   `tempfile.mkstemp`) and points the subprocess at that file via
   `DATABASE_PATH`, and also moved off the default port (8099 instead of
   8000) to avoid any ambiguity with a real dev server. Verified
   `db/merchant.db`'s transaction count was identical (and zero) before
   and after running the script.

7. **Self-caught — `run.sh` couldn't recover from a corrupted virtual
   environment.** While testing this session's changes, a partially-deleted
   `.venv` (directory present, but `.venv/bin/activate` missing) caused
   `run.sh` to fail with a cryptic `line 30: .venv/bin/activate: No such
   file or directory` — because the script only checked `[ -d ".venv" ]`,
   which is true for a corrupted venv just as much as a valid one. **Fix**:
   check for `.venv/bin/activate` specifically, and if it's missing,
   `rm -rf .venv` before recreating rather than trying to reuse a broken
   directory. Verified by deliberately reproducing the exact corrupted
   state (a `.venv` dir with `bin/activate` removed) and confirming
   `run.sh` detected it and rebuilt cleanly, then ran end-to-end
   successfully on the next attempt.

**The two designed failure demos** (both verified live with real output):

- **(a) Policy failure**: `agent_low` (per-transaction cap ₹5,000)
  requesting the ₹6,999 docking station → gate declines with
  `exceeds_per_transaction_cap`, no Razorpay call ever attempted.
- **(b) Infra failure**: an approved purchase with
  `simulate_razorpay_failure: true` → transaction correctly writes
  `payment_pending` before the (simulated) call, then lands in `failed`
  when it errors. Stock untouched, no `razorpay_order_id`, full 3-step
  audit trail (`gate_decision` → `payment_pending` → `razorpay_call_failed`).

## Security Considerations

What's deliberately out of scope for a demo of this size, documented
rather than left for someone else to discover. Ordered roughly by how much
it should worry someone before using this beyond a demo:

- **Auth exists but is weak, and specifically has no replay/MITM
  protection.** `/purchase` requires a static per-agent `X-Agent-Secret`
  header, checked against a value stored in **plaintext** in the
  `buyer_agents` table. This does close the specific gap of "any caller
  who knows an `agent_id` string can impersonate that agent" — agent IDs
  are not secret, they appear in API responses and CLI output, so without
  this check no proof of identity existed at all. But: the secret is
  static (same value every request, no rotation), unsigned (the request
  body itself isn't authenticated, just the header's presence), and this
  demo runs over **plain HTTP with no TLS** — meaning the secret travels
  in cleartext and a network observer or anyone who intercepts one request
  can replay it indefinitely. A real system needs per-request HMAC
  signing (secret signs the request body + a timestamp/nonce, preventing
  replay), TLS on every hop, and rotation. Treat the current mechanism as
  "stops casual impersonation," not "stops a motivated attacker."
- **The stock-race is contained, not eliminated — this distinction
  matters.** Two transactions can still both pass the purchase-time stock
  check simultaneously (decrement only happens at `completed`, so the
  check-then-act window is real and still open). What changed is what
  happens when that race resolves: `decrement_stock()` now detects the
  loser via a `rowcount` check and fails that transaction cleanly to
  `stock_race_lost` instead of silently reporting `completed` with no
  stock actually reserved. If the loser's Razorpay order was already
  created before the failure is detected, this demo does **not**
  automatically refund it — that's flagged in the audit log for manual
  reconciliation, not handled. So: no oversell, but also no automatic
  money-back — a real system needs both an actual stock reservation at
  quote time (not just a check) and an automated compensating-transaction
  (refund/void) path.
- **Razorpay failure categorization is a real taxonomy now, but still
  coarse.** `razorpay_client.py` classifies failures into
  `razorpay_bad_request` / `razorpay_gateway_error` / `razorpay_server_error`
  (Razorpay's own error types) and `network_timeout` /
  `network_unreachable` / `tls_handshake_failure` (connection-level, via
  `requests.exceptions`), falling back to a generic
  `razorpay_unexpected_error` for anything unrecognized — written to
  `decline_reason` so failed transactions are filterable by cause via
  `GET /transactions?status=failed`, not just an opaque string. What's
  still missing: a distinct category for Razorpay-side *authentication*
  failures specifically (bad API keys currently surface as whatever HTTP
  error the SDK happens to raise for that case, not a dedicated
  `razorpay_auth_error` category) — genuinely untested here since testing
  it needs a deliberately-revoked real key, which wasn't available in this
  build environment.
- **SQLite, not load-tested beyond this project's own test suite.** The
  `BEGIN IMMEDIATE` fix for the daily-cap race has a real cost: SQLite has
  no row-level locking, so it serializes ALL writes database-wide for the
  (short) duration of gate evaluation through the `payment_pending` write
  — for every agent and every product, not just the one being purchased.
  This was verified correct for the concurrency levels exercised in this
  project's own tests (single-digit concurrent requests); it was **not**
  load-tested at any higher concurrency, and SQLite's single-writer model
  means it will not scale the way a real multi-writer database would. At
  real scale this needs Postgres (or similar) with row-level/advisory
  locking scoped to the specific agent's spend, not a single-file
  database-wide lock.
- **No rate limiting** on any endpoint.
- **No TLS** — this demo runs over plain HTTP on `127.0.0.1`. Called out
  again here because it compounds the auth weakness above: without TLS,
  the shared secret itself is exposed in transit, not just replayable.
- **Dashboard-embedded demo secrets**: `dashboard/api_client.py` hardcodes
  the same fixed demo secrets as `db/seed.py` so the dashboard can drive
  purchases without a secrets-entry UI. This is a demo convenience only —
  a real frontend should never embed credentials that authorize spending.

## Known Limitations

- **Test-mode only.** No real payment data or real money is ever involved.
- **Quote-to-purchase stock race is now caught, not silently ignored** —
  see "What Broke and How We Fixed It" #4. Multiple transactions can still
  both pass the purchase-time stock check when stock is tight (decrement
  only happens at `completed`), but the loser now fails cleanly to
  `stock_race_lost` instead of silently overselling. What's still a real
  gap: if the Razorpay order was already created before the stock-race
  failure is detected, this demo does not automatically issue a refund —
  that's a genuine compensating-action gap noted in the audit log
  (`"warning"` field) for manual reconciliation, deliberately out of scope
  for this build.
- **Related-products pairings are static and manually curated** (see
  `db/seed.py`'s `RELATED_PRODUCTS`), not personalized or usage-driven.
  This directly answers the track's "grow the merchant's revenue" angle at
  a hackathon-appropriate scope; a real system would base pairings on
  purchase co-occurrence data.
- **Daily spend cap is a calendar-day window (UTC)**, not a rolling 24
  hours — an agent's cap resets at UTC midnight regardless of when it last
  spent.
- **No multi-currency support.** All products are priced in INR.
- **Minimal ACP-inspired protocol subset**, not full spec compliance —
  this is a hackathon-scoped demonstration of the approval/audit pattern,
  not a production-grade implementation of any formal agentic-commerce
  protocol.
- **Dashboard visual verification**: the dashboard was built and every
  data function behind it was verified against the live merchant service
  with real returned data; actual browser-rendered screenshots were not
  captured during development (no browser was available in the build
  environment) — the wiring is proven correct, the pixels weren't manually
  inspected.

## What We'd Improve With More Time

- Move off SQLite to a database with real row-level locking (e.g.
  Postgres), so the daily-cap-race fix's database-wide serialization can
  be scoped to just the affected agent, and so a compensating refund could
  be issued automatically when a stock-race loss is detected after an
  order was already created.
- Real LLM-based goal parsing for the buyer agent's `--goal` flag (the
  isolation boundary is already in place; only the naive keyword fallback
  would need swapping out).
- Webhook-based Razorpay payment confirmation instead of a synchronous
  order-creation call, so `completed` reflects an actual captured payment
  rather than just successful order creation.
- Multi-currency and multi-agent-per-purchase (e.g. shared budget pools)
  support.
- A retry-with-backoff policy for `failed` transactions, explicitly opted
  into per-transaction rather than automatic (to preserve the current
  no-double-charge guarantee).
- Usage-driven (not manually curated) related-product pairings.
- Per-agent API keys hashed at rest, replacing the current plaintext
  shared secret (see Security Considerations).
- Live polling/websocket updates for the dashboard instead of the current
  manual refresh + optional 5-second auto-refresh checkbox.

## Data & Privacy Note

This project uses Razorpay **test mode exclusively**. No real payment
instruments, no real customer data, and no real money are involved at any
point. `.env` (containing test-mode API keys) is excluded from version
control via `.gitignore`, and the merchant service never logs, prints, or
stores API keys anywhere — including inside `audit_log` detail fields,
which were manually inspected during testing to confirm this.
