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

from datetime import datetime, timezone

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


def mandate_is_usable(m: dict) -> bool:
    """Client-side convenience filter only -- purely so the picker doesn't
    offer obviously-dead mandates. The merchant's mandate.py re-checks
    revocation/expiry/scope server-side regardless, so this is not a
    security boundary, just UX."""
    if m.get("revoked"):
        return False
    try:
        expires = datetime.strptime(m["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, KeyError, TypeError):
        return False
    return datetime.now(timezone.utc) < expires


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

tab_catalog, tab_run_agent, tab_mandates, tab_transactions, tab_audit, tab_metrics = st.tabs(
    ["📦 Catalog", "▶️ Run Agent", "🔐 Mandates", "💳 Transactions", "🔍 Audit Trail", "📈 Growth Metrics"]
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
    preset_cols = st.columns(3)

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

    if preset_cols[2].button(
        "🔐 Run Mandate-Signed Demo", use_container_width=True, disabled=not preset_confirm
    ):
        try:
            mandates = [m for m in api.get_mandates("agent_high") if mandate_is_usable(m)]
        except Exception as e:
            mandates = []
            st.session_state["last_run_result"] = {
                "stage": "quote",
                "status_code": 0,
                "body": f"Could not load mandates: {e}",
            }
        if mandates and api.has_local_agent_key("agent_high"):
            result = api.run_agent_flow(
                product_id="prod_001",  # keyboard, well within agent_high's mandate scope
                quantity=1,
                agent_id="agent_high",
                mandate_id=mandates[0]["mandate_id"],
            )
            st.session_state["last_run_result"] = result
        elif "last_run_result" not in st.session_state:
            st.session_state["last_run_result"] = {
                "stage": "quote",
                "status_code": 0,
                "body": "No usable mandate/key found for agent_high — run db/generate_agent_keys.py first.",
            }
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

    # Mandate picker lives outside the form so selecting an agent immediately
    # refreshes which of that agent's mandates are offered -- Streamlit forms
    # don't rerun on widget change until submit, which would otherwise let a
    # stale agent/mandate pairing sit in the UI.
    agent_id = st.selectbox("Agent", ["agent_high", "agent_low"], key="custom_run_agent")

    try:
        agent_mandates = api.get_mandates(agent_id)
    except Exception:
        agent_mandates = []
    usable_mandates = [m for m in agent_mandates if mandate_is_usable(m)]

    use_mandate = st.checkbox(
        "🔐 Sign with delegated mandate (Ed25519)",
        key="use_mandate",
        disabled=not usable_mandates,
        help=(
            "Requires an unrevoked, unexpired mandate for this agent (see the 🔐 Mandates tab) "
            "and its private key on disk under buyer_agent/keys/. Adds a per-request Ed25519 "
            "signature verified BEFORE the spend-cap/stock gate runs -- see merchant_service/mandate.py."
        ),
    )
    if not usable_mandates:
        st.caption(f"No usable mandate found for `{agent_id}`. Run `python3 db/generate_agent_keys.py` first.")

    selected_mandate_id = None
    tamper_signature = False
    if use_mandate and usable_mandates:
        mandate_labels = {
            f"{m['mandate_id']}  (cap ₹{m['max_amount']:,.0f}, expires {m['expires_at']})": m["mandate_id"]
            for m in usable_mandates
        }
        chosen_label = st.selectbox("Mandate", list(mandate_labels.keys()), key="chosen_mandate_label")
        selected_mandate_id = mandate_labels[chosen_label]
        tamper_signature = st.checkbox(
            "😈 Tamper with signature after signing (demonstrate invalid_signature decline)",
            key="tamper_signature",
        )

    with st.form("run_agent_form"):
        col1, col2 = st.columns(2)
        col1.markdown(f"**Agent:** `{agent_id}`" + (f" · **Mandate:** `{selected_mandate_id}`" if selected_mandate_id else ""))
        quantity = col2.number_input("Quantity", min_value=1, value=1)
        simulate_failure = st.checkbox("Simulate Razorpay infra failure")
        confirm_custom = st.checkbox("I understand this will fire a real purchase request")
        submitted = st.form_submit_button("Run Agent Purchase", disabled=not confirm_custom)

        if submitted and product_options:
            if not confirm_custom:
                st.warning("Please confirm before running a real purchase.")
            else:
                result = api.run_agent_flow(
                    selected_product_id,
                    int(quantity),
                    agent_id,
                    simulate_failure,
                    mandate_id=selected_mandate_id,
                    tamper_signature=tamper_signature,
                )
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
            if result.get("mandate_id"):
                st.caption(f"🔐 Signed with delegated mandate `{result['mandate_id']}` (Ed25519, verified server-side).")
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
# Mandates view (AP2-inspired delegated payment authorization)
# ---------------------------------------------------------------------------
with tab_mandates:
    st.subheader("Delegated Payment Mandates")
    st.caption(
        "A mandate is the principal's one-time, Ed25519-signed authorization binding a specific "
        "agent keypair to a spending scope (max amount, currency, expiry). Every mandate-backed "
        "purchase is then signed per-request by the agent's own private key and verified here "
        "server-side, BEFORE the spend-cap/stock gate runs — see merchant_service/mandate.py."
    )

    agent_filter = st.selectbox("Filter by agent", ["(all agents)", "agent_high", "agent_low"], key="mandate_agent_filter")
    filter_agent_id = None if agent_filter == "(all agents)" else agent_filter

    try:
        mandates = api.get_mandates(filter_agent_id)
    except Exception as e:
        mandates = None
        st.error(f"Could not reach merchant service: {e}")

    if mandates is not None:
        if not mandates:
            st.info(
                "No mandates found for this filter. Run `python3 db/generate_agent_keys.py` from a "
                "terminal to issue one per seeded agent."
            )
        for m in mandates:
            usable = mandate_is_usable(m)
            has_key = api.has_local_agent_key(m["agent_id"])
            cols = st.columns([2, 1.3, 1, 1.4, 1.3])
            cols[0].markdown(f"`{m['mandate_id']}`  \nagent: `{m['agent_id']}`")
            cols[1].markdown(f"cap: ₹{m['max_amount']:,.0f} {m['currency']}")
            cols[2].markdown("🔴 revoked" if m["revoked"] else ("🟢 active" if usable else "⏱️ expired"))
            cols[3].markdown(f"expires:  \n{m['expires_at']}")
            revoke_disabled = bool(m["revoked"])
            if cols[4].button("Revoke", key=f"revoke_{m['mandate_id']}", disabled=revoke_disabled):
                try:
                    api.revoke_mandate(m["mandate_id"])
                    st.rerun()
                except Exception as e:
                    st.error(f"Could not revoke: {e}")
            if usable and not has_key:
                st.caption(
                    f"⚠️ No local private key found at `buyer_agent/keys/{m['agent_id']}.key` — "
                    "signing from this dashboard will fail even though the mandate itself is valid."
                )
            with st.expander(f"🔍 Signed binding payload for `{m['mandate_id']}`"):
                st.json({
                    "agent_id": m["agent_id"],
                    "agent_public_key": m["agent_public_key"],
                    "principal_id": m["principal_id"],
                    "principal_public_key": m["principal_public_key"],
                    "principal_signature (masked)": api.mask_signature(m["principal_signature"]),
                    "max_amount": m["max_amount"],
                    "currency": m["currency"],
                    "issued_at": m["issued_at"],
                    "expires_at": m["expires_at"],
                })
            st.divider()

    st.subheader("Mandate-Signed Purchase Attempts")
    st.caption(
        "Every purchase request that carried a mandate_id, whether the Ed25519 signature verified "
        "or not — including attempts that never became a transaction at all, like a revoked mandate "
        "or a tampered signature. Signatures are shown masked here for readability; the merchant "
        "verified the full value server-side regardless of what's displayed."
    )
    outcome_filter = st.selectbox("Filter by outcome", ["(all)", "✅ success", "❌ failed"], key="mandate_attempt_outcome_filter")
    valid_param = {"✅ success": True, "❌ failed": False}.get(outcome_filter)

    try:
        attempts = api.get_mandate_attempts(agent_id=filter_agent_id, valid=valid_param)
    except Exception as e:
        attempts = None
        st.error(f"Could not reach merchant service: {e}")

    if attempts is not None:
        if not attempts:
            st.info("No mandate-signed purchase attempts yet for this filter.")
        for a in attempts:
            cols = st.columns([1.6, 1, 1, 1.6, 1.3, 1.8])
            cols[0].markdown(f"`{a['mandate_id']}`  \nagent: `{a['agent_id']}`")
            cols[1].markdown(f"₹{a['amount']:,.0f}")
            cols[2].markdown("✅ valid" if a["valid"] else "❌ invalid")
            cols[3].markdown(f"reason:  \n`{a['reason']}`")
            cols[4].markdown(f"txn: `{a['txn_id']}`" if a["txn_id"] else "— (no txn)")
            cols[5].markdown(f"sig: `{api.mask_signature(a['signature'])}`  \n🕒 {a['created_at']}")
        st.divider()

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
