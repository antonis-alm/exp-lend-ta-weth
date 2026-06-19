from __future__ import annotations

from contextlib import nullcontext
from unittest.mock import MagicMock, patch

from dashboard import ui


def _mock_columns(count: int):
    return [nullcontext() for _ in range(count)]


@patch("dashboard.ui.render_lending_dashboard")
@patch("dashboard.ui.get_aave_v3_config")
@patch("dashboard.ui.st.success")
@patch("dashboard.ui.st.error")
@patch("dashboard.ui.st.metric")
@patch("dashboard.ui.st.columns", side_effect=_mock_columns)
@patch("dashboard.ui.st.caption")
@patch("dashboard.ui.st.subheader")
@patch("dashboard.ui.st.title")
def test_render_custom_dashboard_uses_aave_template(
    mock_title: MagicMock,
    _mock_subheader: MagicMock,
    _mock_caption: MagicMock,
    _mock_columns_fn: MagicMock,
    mock_metric: MagicMock,
    _mock_error: MagicMock,
    _mock_success: MagicMock,
    mock_get_aave_v3_config: MagicMock,
    mock_render_lending_dashboard: MagicMock,
) -> None:
    strategy_config = {
        "chain": "base",
        "collateral_token": "USDC",
        "borrow_token": "WETH",
        "target_health_factor": "1.5",
        "sell_hf_floor": "1.3",
        "emergency_hf": "1.2",
        "rsi_low": "45",
        "rsi_high": "55",
        "repay_bucket_min_usd": "0.50",
        "max_slippage": "0.003",
        "max_price_impact": "0.05",
    }
    session_state = {
        "health_factor": "1.45",
        "trade_state": "sold_for_usdc",
        "pending_action": "buyback",
        "prev_zone": "high",
        "base_inventory_weth": "0.3",
        "excess_weth_bucket": "0.05",
        "cycle": {"sold_weth": "0.3", "usdc_proceeds": "800"},
    }

    mock_get_aave_v3_config.return_value = {"preset": "aave"}

    ui.render_custom_dashboard(
        deployment_id="dep-1",
        strategy_config=strategy_config,
        api_client=None,
        session_state=session_state,
    )

    mock_title.assert_called_once_with(ui.STRATEGY_TITLE)
    mock_get_aave_v3_config.assert_called_once_with(
        collateral_token="USDC",
        borrow_token="WETH",
        chain="base",
    )
    mock_render_lending_dashboard.assert_called_once_with(
        "dep-1",
        strategy_config,
        session_state,
        {"preset": "aave"},
    )

    metric_labels = [args[0] for args, _kwargs in mock_metric.call_args_list]
    assert "Health Factor" in metric_labels
    assert "Trade State" in metric_labels
    assert "Cycle Proceeds" in metric_labels
    assert "Max Price Impact" in metric_labels


def test_render_execution_overview_flags_emergency_threshold() -> None:
    strategy_config = {
        "target_health_factor": "1.5",
        "sell_hf_floor": "1.3",
        "emergency_hf": "1.2",
    }
    session_state = {
        "health_factor": "1.1",
        "persistent_state": {
            "trade_state": "available_weth",
            "pending_action": "",
            "prev_zone": "low",
        },
    }

    with (
        patch("dashboard.ui.st.columns", side_effect=_mock_columns),
        patch("dashboard.ui.st.metric"),
        patch("dashboard.ui.st.success") as mock_success,
        patch("dashboard.ui.st.error") as mock_error,
    ):
        ui._render_execution_overview(session_state, strategy_config)

    mock_error.assert_called_once()
    mock_success.assert_not_called()
