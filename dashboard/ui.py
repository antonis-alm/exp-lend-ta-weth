from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

import streamlit as st
from almanak.framework.dashboard.templates import get_aave_v3_config, render_lending_dashboard

STRATEGY_TITLE = "TA Lending Swap WETH"


def _decimal(value: Any, default: str = "0") -> Decimal:
    try:
        if value is None:
            return Decimal(default)
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def _persistent(session_state: dict[str, Any], key: str, default: Any) -> Any:
    if key in session_state:
        return session_state.get(key, default)
    persistent = session_state.get("persistent_state", {})
    if isinstance(persistent, dict):
        return persistent.get(key, default)
    return default


def _render_header_metrics(session_state: dict[str, Any], strategy_config: dict[str, Any]) -> None:
    health_factor = _decimal(session_state.get("health_factor", "0"))
    trade_state = str(_persistent(session_state, "trade_state", "available_weth"))
    pending_action = str(_persistent(session_state, "pending_action", ""))
    prev_zone = str(_persistent(session_state, "prev_zone", "neutral"))

    target_hf = _decimal(strategy_config.get("target_health_factor", "1.5"))
    sell_floor = _decimal(strategy_config.get("sell_hf_floor", "1.3"))
    emergency_hf = _decimal(strategy_config.get("emergency_hf", "1.2"))

    cols = st.columns(4)
    with cols[0]:
        st.metric("Health Factor", f"{health_factor:.3f}", help=f"Target: {target_hf:.2f}")
    with cols[1]:
        st.metric("Trade State", trade_state.replace("_", " ").title())
    with cols[2]:
        st.metric("Pending Action", pending_action or "none")
    with cols[3]:
        st.metric("RSI Zone", prev_zone.title())

    if 0 < health_factor < emergency_hf:
        st.error(f"Emergency threshold breached: HF {health_factor:.3f} < {emergency_hf:.2f}")
    elif 0 < health_factor < sell_floor:
        st.warning(f"HF below sell floor: {health_factor:.3f} < {sell_floor:.2f}")
    else:
        st.success("Health factor is within configured guardrails")


def _render_cycle_metrics(session_state: dict[str, Any], strategy_config: dict[str, Any]) -> None:
    base_inventory = _decimal(_persistent(session_state, "base_inventory_weth", "0"))
    excess_bucket = _decimal(_persistent(session_state, "excess_weth_bucket", "0"))
    cycle = _persistent(session_state, "cycle", {})
    cycle_sold = _decimal(cycle.get("sold_weth", "0") if isinstance(cycle, dict) else "0")
    cycle_proceeds = _decimal(cycle.get("usdc_proceeds", "0") if isinstance(cycle, dict) else "0")
    candle_key = str(_persistent(session_state, "last_processed_candle_key", "-"))

    rsi_low = _decimal(strategy_config.get("rsi_low", "45"))
    rsi_high = _decimal(strategy_config.get("rsi_high", "55"))
    repay_threshold = _decimal(strategy_config.get("repay_bucket_min_usd", "0.06"))

    st.subheader("Cycle")
    cols = st.columns(4)
    with cols[0]:
        st.metric("Base Inventory (WETH)", f"{base_inventory:.6f}")
    with cols[1]:
        st.metric("Excess Bucket (WETH)", f"{excess_bucket:.6f}", help=f"Repay threshold: ${repay_threshold}")
    with cols[2]:
        st.metric("Cycle Sold (WETH)", f"{cycle_sold:.6f}")
    with cols[3]:
        st.metric("Cycle Proceeds (USDC)", f"{cycle_proceeds:.2f}")

    st.caption(f"RSI sell > {rsi_high} | RSI buy < {rsi_low} | Last 5m candle: {candle_key}")


def render_custom_dashboard(
    deployment_id: str,
    strategy_config: dict[str, Any],
    api_client: Any,
    session_state: dict[str, Any],
) -> None:
    st.title(STRATEGY_TITLE)

    _render_header_metrics(session_state, strategy_config)
    _render_cycle_metrics(session_state, strategy_config)

    template_config = get_aave_v3_config(
        collateral_token=str(strategy_config.get("collateral_token", "USDC")),
        borrow_token=str(strategy_config.get("borrow_token", "WETH")),
        chain=str(strategy_config.get("chain", "base")),
    )

    render_lending_dashboard(deployment_id, strategy_config, session_state, template_config)
