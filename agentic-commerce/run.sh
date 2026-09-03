#!/usr/bin/env bash
# run.sh — one-command startup for the Agentic Commerce demo.
#
# Safe to re-run: it will NOT wipe existing demo data. db/seed.py only
# inserts seed rows if the products table is empty (pass --force to
# db/seed.py yourself if you deliberately want to reset).
#
# This script starts the merchant FastAPI service and then prints the two
# follow-up commands (buyer agent CLI, Streamlit dashboard) rather than
# starting them itself, since they are interactive/foreground processes
# best run in their own terminal tabs — see the printed instructions below.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== Agentic Commerce Demo — startup ==="
echo ""

# --- 1. venv setup ---
if [ ! -d ".venv" ]; then
    echo "[1/4] Creating virtual environment (.venv)..."
    python3 -m venv .venv
else
    echo "[1/4] Virtual environment already exists — reusing .venv"
fi

# shellcheck disable=SC1091
source .venv/bin/activate

# --- 2. install requirements ---
echo "[2/4] Installing requirements.txt..."
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

# --- 3. .env check ---
if [ ! -f ".env" ]; then
    echo ""
    echo "  NOTE: .env not found. Copying .env.example -> .env"
    echo "  Edit .env and add your real Razorpay TEST-MODE keys before"
    echo "  running a real (non-simulated) purchase."
    cp .env.example .env
fi

# --- 4. seed DB if missing ---
echo "[3/4] Seeding database (skipped automatically if data already exists)..."
python3 db/seed.py

# --- 5. start the merchant FastAPI service ---
echo "[4/4] Starting merchant service on http://127.0.0.1:8000 ..."
echo ""
echo "======================================================================"
echo " Merchant service starting now (this terminal will stay attached to it)."
echo ""
echo " In TWO SEPARATE terminals, run:"
echo ""
echo "   1) Buyer agent CLI (example):"
echo "      source .venv/bin/activate"
echo "      python3 buyer_agent/agent.py --product keyboard --budget 10000 --agent-id agent_high"
echo ""
echo "   2) Streamlit dashboard:"
echo "      source .venv/bin/activate"
echo "      streamlit run dashboard/app.py"
echo ""
echo " These are separate manual commands because they are interactive"
echo " foreground processes best kept in their own terminal windows."
echo "======================================================================"
echo ""

exec python3 -m uvicorn merchant_service.main:app --host 127.0.0.1 --port 8000
