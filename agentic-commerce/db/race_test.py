"""
One-off script (not part of the app) to test genuine concurrent-request
DAILY-SPEND-CAP safety: starts the merchant service as a subprocess
against an ISOLATED throwaway database (never the real db/merchant.db),
fires two truly concurrent HTTP threads at /purchase for TWO DIFFERENT
quotes that individually pass the per-transaction cap but together
exceed the daily spend cap -- proving the BEGIN IMMEDIATE serialization
in main.py's /purchase closes the read-check-write race on money, not
just on stock.

IMPORTANT: this script previously ran against the real db/merchant.db by
default, which meant a forced-success test run could leave test
transactions sitting in the actual demo database. Fixed to always use a
disposable temp DB file, seeded fresh on every run and left in /tmp
(not cleaned up automatically, so you can inspect it after a run, but
never touching your real demo data).
"""
import subprocess
import sys
import time
import threading
import requests
import os
import signal
import sqlite3
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8099  # deliberately non-default, avoid colliding with a real dev server on 8000
BASE = f"http://127.0.0.1:{PORT}"
proc = None

# --- Isolated throwaway DB, seeded fresh, never the real demo DB ---
tmp_db_fd, tmp_db_path = tempfile.mkstemp(suffix=".db", prefix="race_test_")
os.close(tmp_db_fd)
os.remove(tmp_db_path)  # seed.py/schema.sql expect to create it fresh

with open(os.path.join(PROJECT_ROOT, "db", "schema.sql")) as f:
    schema_sql = f.read()
seed_conn = sqlite3.connect(tmp_db_path)
seed_conn.executescript(schema_sql)
seed_conn.executemany(
    "INSERT INTO products (product_id, name, price, currency, stock) VALUES (?, ?, ?, ?, ?)",
    [("prod_001", "Wireless Mechanical Keyboard", 4499.00, "INR", 25)],
)
seed_conn.executemany(
    "INSERT INTO buyer_agents (agent_id, name, max_authorized_amount, daily_spend_cap, shared_secret) "
    "VALUES (?, ?, ?, ?, ?)",
    [("agent_low", "Sandbox Agent (Low Trust)", 5000.00, 8000.00, "demo-secret-low-3e9b7d")],
)
seed_conn.commit()
seed_conn.close()
print(f"Seeded isolated throwaway DB at: {tmp_db_path}")

try:
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "merchant_service.main:app",
         "--host", "127.0.0.1", "--port", str(PORT)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        cwd=PROJECT_ROOT,
        env={**os.environ, "RAZORPAY_FORCE_SUCCESS": "1", "DATABASE_PATH": tmp_db_path},
        preexec_fn=os.setsid,
    )

    ready = False
    for _ in range(20):
        try:
            r = requests.get(f"{BASE}/catalog", timeout=1)
            if r.status_code == 200:
                ready = True
                break
        except Exception:
            pass
        time.sleep(0.5)

    if not ready:
        print("Server never became ready.")
        sys.exit(1)

    print("Server is up.")

    # agent_low: per-txn cap 5000, daily cap 8000.
    # Two keyboards at 4499 each: each individually passes the per-txn
    # cap (4499 < 5000), but together (8998) exceed the daily cap (8000).
    q1 = requests.post(f"{BASE}/quote", json={"product_id": "prod_001", "quantity": 1}, timeout=5).json()
    q2 = requests.post(f"{BASE}/quote", json={"product_id": "prod_001", "quantity": 1}, timeout=5).json()
    print(f"Quote A: {q1['quote_id']} (${q1['total_price']})")
    print(f"Quote B: {q2['quote_id']} (${q2['total_price']})")
    print(f"Combined: {q1['total_price'] + q2['total_price']} vs agent_low daily cap 8000.0")

    results = []
    lock = threading.Lock()

    def fire(quote_id):
        try:
            resp = requests.post(
                f"{BASE}/purchase",
                json={"quote_id": quote_id, "agent_id": "agent_low"},
                headers={"X-Agent-Secret": "demo-secret-low-3e9b7d"},
                timeout=10,
            )
            with lock:
                results.append(resp.json())
        except Exception as e:
            with lock:
                results.append({"error": str(e)})

    threads = [
        threading.Thread(target=fire, args=(q1["quote_id"],)),
        threading.Thread(target=fire, args=(q2["quote_id"],)),
    ]
    print("Firing both /purchase requests concurrently ...")
    start = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    print(f"Both threads finished in {time.time() - start:.2f}s")

    for i, r in enumerate(results):
        txn = r.get("transaction", {})
        print(f"  result {i}: status={txn.get('status')} decline_reason={txn.get('decline_reason')} amount={txn.get('amount')}")

    statuses = sorted(r.get("transaction", {}).get("status") for r in results)
    reasons = [r.get("transaction", {}).get("decline_reason") for r in results]
    print(f"\nStatuses: {statuses}")
    print(f"Reasons: {reasons}")

    # Note: simulate_razorpay_failure=True means the winner lands in
    # 'failed' (not 'completed') for this demo -- that's fine, the point
    # here is proving the GATE decision (approved vs declined) never lets
    # both purchases through, which is decided before the Razorpay call.
    approved_count = sum(1 for r in results if r.get("transaction", {}).get("decline_reason") != "exceeds_daily_spend_cap")
    print(f"\nRequests that passed the daily-cap gate check: {approved_count} (must be exactly 1)")
    print("PASS" if approved_count == 1 else "FAIL: daily cap race not closed!")

finally:
    if proc is not None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            pass
        proc.wait(timeout=5)
    print("Server subprocess terminated.")

