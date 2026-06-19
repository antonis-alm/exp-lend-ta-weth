from __future__ import annotations

from contextlib import nullcontext
from decimal import Decimal, InvalidOperation
from typing import Any

try:
    import streamlit as st
except ModuleNotFoundError:
    class _StreamlitFallback:
        def title(self, *_: Any, **__: Any) -> None:
            return None

        def subheader(self, *_: Any, **__: Any) -> None:
            return None

        def caption(self, *_: Any, **__: Any) -> None:
            return None

        def metric(self, *_: Any, **__: Any) -> None:
            return None

        def success(self, *_: Any, **__: Any) -> None:
            return None

        def warning(self, *_: Any, **__: Any) -> None:
            return None

        def error(self, *_: Any, **__: Any) -> None:
            return None

        def columns(self, count: int) -> list[Any]:
            return [nullcontext() for _ in range(count)]

    st = _StreamlitFallback()

from almanak.framework.dashboard.templates import get_aave_v3_config, render_lending_dashboard

STRATEGY_TITLE = "TA Lending Swap WETH"


def _to_decimal(value: Any, default: str = "0") -> Decimal:
    try:
        if value is None:
            return Decimal(default)
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _state_value(session_state: dict[str, Any], key: str, default: Any) -> Any:
    if key in session_state:
        return session_state.get(key, default)
    persistent_state = session_state.get("persistent_state")
    if isinstance(persistent_state, dict):
        return persistent_state.get(key, default)
    return default


def _render_execution_overview(session_state: dict[str, Any], strategy_config: dict[str, Any]) -> None:
    health_factor = _to_decimal(session_state.get("health_factor"), "0")
    target_hf = _to_decimal(strategy_config.get("target_health_factor"), "1.5")
    emergency_hf = _to_decimal(strategy_config.get("emergency_hf"), "1.2")

    trade_state = str(_state_value(session_state, "trade_state", "available_weth"))
    pending_action = str(_state_value(session_state, "pending_action", "none"))
    rsi_zone = str(_state_value(session_state, "prev_zone", "neutral"))

    cols = st.columns(4)
    with cols[0]:
        st.metric("Health Factor", f"{health_factor:.3f}", help=f"Target {target_hf:.2f}")
    with cols[1]:
        st.metric("Trade State", trade_state.replace("_", " ").title())
    with cols[2]:
        st.metric("Pending", pending_action)
    with cols[3]:
        st.metric("RSI Zone", rsi_zone.title())

    if 0 < health_factor < emergency_hf:
        st.error(f"Emergency mode active: HF {health_factor:.3f} < {emergency_hf:.2f}")
    elif health_factor > 0:
        st.success("Health factor is above emergency threshold")


def _render_cycle_state(session_state: dict[str, Any], strategy_config: dict[str, Any]) -> None:
    base_inventory = _to_decimal(_state_value(session_state, "base_inventory_weth", "0"))
    excess_bucket = _to_decimal(_state_value(session_state, "excess_weth_bucket", "0"))

    cycle = _state_value(session_state, "cycle", {})
    cycle_sold_weth = _to_decimal(cycle.get("sold_weth", "0") if isinstance(cycle, dict) else "0")
    cycle_usdc_proceeds = _to_decimal(cycle.get("usdc_proceeds", "0") if isinstance(cycle, dict) else "0")

    repay_bucket_min_usd = _to_decimal(strategy_config.get("repay_bucket_min_usd", "0.50"))
    rsi_low = _to_decimal(strategy_config.get("rsi_low", "45"))
    rsi_high = _to_decimal(strategy_config.get("rsi_high", "55"))

    st.subheader("Cycle State")
    cols = st.columns(4)
    with cols[0]:
        st.metric("Base WETH", f"{base_inventory:.6f}")
    with cols[1]:
        st.metric("Excess Bucket", f"{excess_bucket:.6f}", help=f"Repay threshold ${repay_bucket_min_usd}")
    with cols[2]:
        st.metric("Cycle Sold", f"{cycle_sold_weth:.6f}")
    with cols[3]:
        st.metric("Cycle Proceeds", f"{cycle_usdc_proceeds:.2f} USDC")

    st.caption(f"Sell if RSI > {rsi_high}; buy back if RSI < {rsi_low}")


def _render_guardrails(strategy_config: dict[str, Any]) -> None:
    target_hf = _to_decimal(strategy_config.get("target_health_factor", "1.5"))
    sell_hf_floor = _to_decimal(strategy_config.get("sell_hf_floor", "1.3"))
    emergency_hf = _to_decimal(strategy_config.get("emergency_hf", "1.2"))
    max_slippage = _to_decimal(strategy_config.get("max_slippage", "0.003")) * Decimal("100")
    max_price_impact = _to_decimal(strategy_config.get("max_price_impact", "0.05")) * Decimal("100")

    st.subheader("Configured Guardrails")
    cols = st.columns(5)
    with cols[0]:
        st.metric("Target HF", f"{target_hf:.2f}")
    with cols[1]:
        st.metric("Sell HF Floor", f"{sell_hf_floor:.2f}")
    with cols[2]:
        st.metric("Emergency HF", f"{emergency_hf:.2f}")
    with cols[3]:
        st.metric("Max Slippage", f"{max_slippage:.2f}%")
    with cols[4]:
        st.metric("Max Price Impact", f"{max_price_impact:.2f}%")


def render_custom_dashboard(
    deployment_id: str,
    strategy_config: dict[str, Any],
    api_client: Any,
    session_state: dict[str, Any],
) -> None:
    strategy_config = _as_dict(strategy_config)
    session_state = _as_dict(session_state)

    st.title(STRATEGY_TITLE)

    _render_execution_overview(session_state, strategy_config)
    _render_cycle_state(session_state, strategy_config)
    _render_guardrails(strategy_config)

    template_config = get_aave_v3_config(
        collateral_token=str(strategy_config.get("collateral_token", "USDC")),
        borrow_token=str(strategy_config.get("borrow_token", "WETH")),
        chain=str(strategy_config.get("chain", "base")),
    )

    render_lending_dashboard(deployment_id, strategy_config, session_state, template_config)
