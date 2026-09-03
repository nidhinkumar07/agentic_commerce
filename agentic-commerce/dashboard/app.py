"""
dashboard/app.py

Streamlit dashboard for the Agentic Commerce demo. Reads exclusively via
the merchant service's FastAPI endpoints (see api_client.py) -- the same
interface the buyer agent CLI uses. This file contains NO business logic:
it only displays what the merchant service already decided and recorded.

Run with:
    streamlit run dashboard/app.py
"""

import os
import sys

import streamlit as st

sys.path.insert(0, os.path.dirname(__file__))
import api_client as api

st.set_page_config(page_title="Agentic Commerce Demo", layout="wide")

STATUS_BADGE_COLORS = {
    "pending_gate": "gray",
    "declined": "red",
    "approved": "orange",
    "payment_pending": "yellow",
    "completed": "green",
    "failed": "red",
}

LOW_STOCK_THRESHOLD = 3  # at or below this (but > 0) is flagged as low, not just "out"


def render_status_badge(container, status: str):
    """
    Renders a colored status badge using Streamlit's native st.badge
    (available since Streamlit 1.4x) instead of hand-rolled HTML+inline
    CSS. Avoids unsafe_allow_html entirely for this. `container` is
    anything with a .badge() method -- st itself, or a column/expander.
    """
    color = STATUS_BADGE_COLORS.get(status, "gray")
    container.badge(status or "unknown", color=color)


def looks_like_txn_id(value: str) -> bool:
    """Loose format check so garbage input gets a helpful message instead
    of just a bare 404 -- txn_ids are always 'txn_' + a hex suffix."""
    return value.startswith("txn_") and len(value) > 4


def render_audit_trail(container, trail: list):
    """Shared rendering used by both the dedicated Audit tab and the
    inline per-row expanders in the Transactions tab, so the two views
    never drift out of sync."""
    for i, entry in enumerate(trail):
        container.markdown(f"**Step {i+1}: `{entry['step']}`**  \n🕒 {entry['timestamp']}")
        container.json(entry["detail"])
        if i < len(trail) - 1:
            container.markdown("&nbsp;&nbsp;&nbsp;&nbsp;⬇️", unsafe_allow_html=True)


st.title("🤖 Agentic Commerce — Live Demo Dashboard")

top_cols = st.columns([4, 1.4, 1.6])
top_cols[1].button("🔄 Refresh Now", use_container_width=True, on_click=lambda: st.rerun())
auto_refresh = top_cols[2].checkbox("Auto-refresh (5s)", key="auto_refresh")
if auto_refresh:
    st.caption(
        "🔁 Auto-refresh is on — the page will reload every 5s so changes made from another "
        "terminal (e.g. the buyer agent CLI) show up here without manual interaction."
    )

tab_catalog, tab_run_agent, tab_transactions, tab_audit, tab_metrics = st.tabs(
    ["📦 Catalog", "▶️ Run Agent", "💳 Transactions", "🔍 Audit Trail", "📈 Growth Metrics"]
)

# ---------------------------------------------------------------------------
# Catalog view
# ---------------------------------------------------------------------------
with tab_catalog:
    st.subheader("Product Catalog")
    try:
        products = api.get_catalog()
        for p in products:
            cols = st.columns([3, 1, 1.5])
            cols[0].markdown(f"**{p['name']}**  \n`{p['product_id']}`")
            cols[1].markdown(f"{p['price']} {p['currency']}")
            if p["stock"] == 0:
                cols[2].markdown("🔴 **OUT OF STOCK**")
            elif p["stock"] <= LOW_STOCK_THRESHOLD:
                cols[2].markdown(f"🟡 **Low stock: {p['stock']} left**")
            else:
                cols[2].markdown(f"Stock: {p['stock']}")
        if not products:
            st.info("No products found.")
        if any(0 < p["stock"] <= LOW_STOCK_THRESHOLD for p in products):
            st.caption(
                "🟡 Low-stock items are exactly where the quote-to-purchase stock race can "
                "surface — see README 'Security Considerations' for what's and isn't handled."
            )
    except Exception as e:
        st.error(f"Could not reach merchant service at {api.MERCHANT_API_BASE_URL}: {e}")

# ---------------------------------------------------------------------------
# Run Agent view
# ---------------------------------------------------------------------------
with tab_run_agent:
    st.subheader("Trigger a Buyer Agent Purchase")
    st.caption(
        "⚠️ Every action below fires a REAL request against the merchant service and, on "
        "approval, a REAL Razorpay test-mode payment call. There is no undo."
    )

    st.markdown("**Quick demo presets** — reliable one-click triggers for the pitch:")
    preset_confirm = st.checkbox(
        "I understand the preset buttons below will fire a real purchase request", key="preset_confirm"
    )
    preset_cols = st.columns(2)

    if preset_cols[0].button(
        "🚫 Run Policy-Failure Demo", use_container_width=True, disabled=not preset_confirm
    ):
        result = api.run_agent_flow(
            product_id="prod_003",  # 4K monitor, 28999.0
            quantity=1,
            agent_id="agent_low",  # per-txn cap 5000.0 -- will be declined
            simulate_razorpay_failure=False,
        )
        st.session_state["last_run_result"] = result
        st.rerun()

    if preset_cols[1].button(
        "⚠️ Run Infra-Failure Demo", use_container_width=True, disabled=not preset_confirm
    ):
        result = api.run_agent_flow(
            product_id="prod_005",  # headphones, in budget/cap
            quantity=1,
            agent_id="agent_high",
            simulate_razorpay_failure=True,
        )
        st.session_state["last_run_result"] = result
        st.rerun()

    st.divider()
    st.markdown("**Custom run:**")

    try:
        products = api.get_catalog()
        product_options = {f"{p['name']} ({p['product_id']})": p["product_id"] for p in products}
    except Exception:
        product_options = {}

    selected_label = st.selectbox(
        "Product", list(product_options.keys()) if product_options else ["<no catalog>"], key="custom_product_select"
    )
    if product_options:
        selected_product_id = product_options[selected_label]
        related = api.get_related_products(selected_product_id)
        if related:
            related_desc = ", ".join(f"{r['name']} ({r['product_id']})" for r in related)
            st.caption(f"🔗 You might also consider: {related_desc}")

    with st.form("run_agent_form"):
        col1, col2 = st.columns(2)
        agent_id = col1.selectbox("Agent", ["agent_high", "agent_low"])
        quantity = col2.number_input("Quantity", min_value=1, value=1)
        simulate_failure = st.checkbox("Simulate Razorpay infra failure")
        confirm_custom = st.checkbox("I understand this will fire a real purchase request")
        submitted = st.form_submit_button("Run Agent Purchase", disabled=not confirm_custom)

        if submitted and product_options:
            if not confirm_custom:
                st.warning("Please confirm before running a real purchase.")
            else:
                result = api.run_agent_flow(selected_product_id, int(quantity), agent_id, simulate_failure)
                st.session_state["last_run_result"] = result
                st.rerun()

    if "last_run_result" in st.session_state:
        result = st.session_state["last_run_result"]
        st.divider()
        st.markdown("### Result")
        if result["stage"] == "quote":
            st.error(f"Quote request failed: HTTP {result['status_code']} — {result['body']}")
        else:
            txn = result["body"].get("transaction", {})
            idempotent = result["body"].get("idempotent", False)
            status_row = st.container()
            status_row.markdown("**Status:**")
            render_status_badge(status_row, txn.get("status"))
            if idempotent:
                st.info("This was an idempotent duplicate — no new transaction was created.")
            st.json(txn)
            if txn.get("txn_id"):
                st.caption(f"Full audit trail for this transaction is shown below:")
                try:
                    trail = api.get_audit_trail(txn["txn_id"])
                    if trail:
                        with st.expander(f"🔍 Audit trail for `{txn['txn_id']}`", expanded=True):
                            render_audit_trail(st, trail)
                except Exception as e:
                    st.caption(f"(Could not load audit trail inline: {e}. Try the 🔍 Audit Trail tab.)")

# ---------------------------------------------------------------------------
# Transactions view
# ---------------------------------------------------------------------------
with tab_transactions:
    st.subheader("All Transactions")
    filter_cols = st.columns([2, 1])
    status_filter = filter_cols[0].selectbox(
        "Filter by status",
        ["(all)", "pending_gate", "declined", "approved", "payment_pending", "completed", "failed"],
    )
    page_size = filter_cols[1].selectbox("Rows per page", [10, 25, 50, 100], index=1)

    try:
        txns = api.get_transactions(status=None if status_filter == "(all)" else status_filter)
        if not txns:
            st.info("No transactions yet.")
        else:
            total = len(txns)
            max_page = max(1, (total - 1) // page_size + 1)
            if "txn_page" not in st.session_state:
                st.session_state["txn_page"] = 1
            st.session_state["txn_page"] = min(st.session_state["txn_page"], max_page)

            page_nav_cols = st.columns([1, 2, 1])
            if page_nav_cols[0].button("← Prev", disabled=st.session_state["txn_page"] <= 1):
                st.session_state["txn_page"] -= 1
                st.rerun()
            page_nav_cols[1].markdown(
                f"<div style='text-align:center'>Page {st.session_state['txn_page']} of {max_page} "
                f"({total} total)</div>",
                unsafe_allow_html=True,
            )
            if page_nav_cols[2].button("Next →", disabled=st.session_state["txn_page"] >= max_page):
                st.session_state["txn_page"] += 1
                st.rerun()

            start = (st.session_state["txn_page"] - 1) * page_size
            page_txns = txns[start:start + page_size]

            for t in page_txns:
                cols = st.columns([2, 1.5, 1.3, 1, 2])
                cols[0].markdown(f"`{t['txn_id']}`")
                cols[1].markdown(f"agent: `{t['agent_id']}`")
                cols[2].markdown(f"amount: {t['amount']}")
                render_status_badge(cols[3], t["status"])
                cols[4].markdown(t.get("decline_reason") or t.get("razorpay_order_id") or "—")
                with st.expander(f"🔍 View audit trail for `{t['txn_id']}`"):
                    try:
                        trail = api.get_audit_trail(t["txn_id"])
                        if trail is None:
                            st.warning("Transaction not found (may have been deleted).")
                        elif not trail:
                            st.info("No audit log entries yet.")
                        else:
                            render_audit_trail(st, trail)
                    except Exception as e:
                        st.error(f"Could not reach merchant service: {e}")
    except Exception as e:
        st.error(f"Could not reach merchant service: {e}")

# ---------------------------------------------------------------------------
# Audit trail drill-down
# ---------------------------------------------------------------------------
with tab_audit:
    st.subheader("Audit Trail Drill-Down")
    st.caption(
        "Tip: you can also expand '🔍 View audit trail' directly under any row in the "
        "Transactions tab — no need to copy-paste a txn_id there."
    )
    txn_id_input = st.text_input("Enter a txn_id to view its full step-by-step trail")
    if txn_id_input:
        if not looks_like_txn_id(txn_id_input):
            st.warning(
                f"'{txn_id_input}' doesn't look like a txn_id (expected format: `txn_` followed "
                f"by a hex string). Looking it up anyway, but this is likely a typo."
            )
        try:
            trail = api.get_audit_trail(txn_id_input)
        except Exception as e:
            st.error(f"Could not reach merchant service at {api.MERCHANT_API_BASE_URL}: {e}")
        else:
            if trail is None:
                st.warning(f"No transaction found with txn_id `{txn_id_input}`.")
            elif not trail:
                st.info("Transaction found but has no audit log entries yet.")
            else:
                render_audit_trail(st, trail)

# ---------------------------------------------------------------------------
# Growth metrics
# ---------------------------------------------------------------------------
with tab_metrics:
    st.subheader("Growth Metrics")
    st.caption(
        "Every number here is a direct aggregation over real transaction rows -- nothing "
        "estimated or simulated. This view answers the question a merchant actually asks: "
        "how much money moved, how much didn't, and why."
    )
    try:
        m = api.get_metrics()

        metric_cols = st.columns(4)
        metric_cols[0].metric("GMV Completed", f"₹{m['gmv_completed']:,.2f}")
        metric_cols[1].metric("GMV Attempted", f"₹{m['gmv_attempted']:,.2f}")
        completion_pct = f"{m['completion_rate']*100:.1f}%" if m["completion_rate"] is not None else "—"
        metric_cols[2].metric("Completion Rate", completion_pct)
        aov = f"₹{m['avg_order_value_completed']:,.2f}" if m["avg_order_value_completed"] is not None else "—"
        metric_cols[3].metric("Avg Order Value", aov)

        st.divider()
        count_cols = st.columns(4)
        count_cols[0].metric("Completed", m["completed_count"])
        count_cols[1].metric("Declined", m["declined_count"])
        count_cols[2].metric("Failed", m["failed_count"])
        count_cols[3].metric("In Flight", m["in_flight_count"])

        st.divider()
        reason_cols = st.columns(2)
        with reason_cols[0]:
            st.markdown("**Declines by reason** (policy gate)")
            if m["decline_reasons"]:
                for reason, count in sorted(m["decline_reasons"].items(), key=lambda x: -x[1]):
                    st.markdown(f"- `{reason}`: {count}")
            else:
                st.caption("No declines yet.")
        with reason_cols[1]:
            st.markdown("**Failures by reason** (payment/infra)")
            if m["failure_reasons"]:
                for reason, count in sorted(m["failure_reasons"].items(), key=lambda x: -x[1]):
                    st.markdown(f"- `{reason}`: {count}")
            else:
                st.caption("No failures yet.")

        st.caption(
            "Note: related-product suggestions (see Run Agent tab) are not yet instrumented "
            "for attach-rate measurement -- that's a real gap, not a hidden metric. See README "
            "'What We'd Improve' for what a real cross-sell attribution pipeline would need."
        )
    except Exception as e:
        st.error(f"Could not reach merchant service at {api.MERCHANT_API_BASE_URL}: {e}")

if auto_refresh:
    import time
    time.sleep(5)
    st.rerun()
