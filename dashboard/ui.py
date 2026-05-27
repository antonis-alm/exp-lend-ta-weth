from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

import streamlit as st
from almanak.framework.dashboard.templates import get_aave_v3_config, render_lending_dashboard

STRATEGY_TITLE = "TA-Lending-Swap-WETH"


def _to_decimal(value: Any, default: str = "0") -> Decimal:
    try:
        if value is None:
            return Decimal(default)
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)


def _state_value(session_state: dict[str, Any], key: str, default: Any) -> Any:
    if key in session_state:
        return session_state.get(key, default)
    persistent_state = session_state.get("persistent_state", {})
    if isinstance(persistent_state, dict):
        return persistent_state.get(key, default)
    return default


def _render_cycle_metrics(session_state: dict[str, Any], strategy_config: dict[str, Any]) -> None:
    health_factor = _to_decimal(session_state.get("health_factor", "0"))
    cycle_state = str(_state_value(session_state, "trade_state", "available_weth"))
    prev_zone = str(_state_value(session_state, "prev_zone", "neutral"))
    candle_bucket = str(_state_value(session_state, "last_processed_candle_key", "-"))

    base_inventory = _to_decimal(_state_value(session_state, "base_inventory_weth", "0"))
    bucket_weth = _to_decimal(_state_value(session_state, "excess_weth_bucket", "0"))
    cycle = _state_value(session_state, "cycle", {})
    cycle_sold_weth = _to_decimal(cycle.get("sold_weth", "0") if isinstance(cycle, dict) else "0")
    cycle_usdc_proceeds = _to_decimal(cycle.get("usdc_proceeds", "0") if isinstance(cycle, dict) else "0")

    target_hf = _to_decimal(strategy_config.get("target_health_factor", "1.5"))
    sell_floor = _to_decimal(strategy_config.get("sell_hf_floor", "1.3"))
    emergency_hf = _to_decimal(strategy_config.get("emergency_hf", "1.2"))
    rsi_low = _to_decimal(strategy_config.get("rsi_low", "45"))
    rsi_high = _to_decimal(strategy_config.get("rsi_high", "55"))
    repay_threshold = _to_decimal(strategy_config.get("repay_bucket_min_usd", "0.06"))

    st.subheader("Cycle State")
    top_row = st.columns(4)
    with top_row[0]:
        st.metric("Health Factor", f"{health_factor:.3f}", help=f"Target HF: {target_hf:.2f}")
    with top_row[1]:
        st.metric("Cycle State", cycle_state.replace("_", " ").title())
    with top_row[2]:
        st.metric("RSI Bucket", prev_zone.title(), help=f"Sell>{rsi_high}, Buy<{rsi_low}")
    with top_row[3]:
        st.metric("5m Candle Bucket", candle_bucket)

    second_row = st.columns(4)
    with second_row[0]:
        st.metric("Base Inventory (WETH)", f"{base_inventory:.6f}")
    with second_row[1]:
        st.metric("Excess WETH Bucket", f"{bucket_weth:.6f}", help=f"Repay threshold: ${repay_threshold}")
    with second_row[2]:
        st.metric("Cycle Sold (WETH)", f"{cycle_sold_weth:.6f}")
    with second_row[3]:
        st.metric("Cycle Proceeds (USDC)", f"{cycle_usdc_proceeds:.2f}")

    if health_factor > 0 and health_factor < emergency_hf:
        st.error(f"Emergency threshold breached: HF {health_factor:.3f} < {emergency_hf:.2f}")
    elif health_factor > 0 and health_factor < sell_floor:
        st.warning(f"HF below sell floor: {health_factor:.3f} < {sell_floor:.2f}")
    else:
        st.success(f"HF healthy for cycle execution (sell floor {sell_floor:.2f})")


def render_custom_dashboard(
    deployment_id: str,
    strategy_config: dict[str, Any],
    api_client: Any,
    session_state: dict[str, Any],
) -> None:
    st.title(STRATEGY_TITLE)

    _render_cycle_metrics(session_state, strategy_config)

    config = get_aave_v3_config(
        collateral_token=str(strategy_config.get("collateral_token", "USDC")),
        borrow_token=str(strategy_config.get("borrow_token", "WETH")),
        chain=str(strategy_config.get("chain", "base")),
    )

    render_lending_dashboard(deployment_id, strategy_config, session_state, config)
