from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from almanak.framework.market import MarketSnapshot, RSIData, TokenBalance
from almanak.framework.teardown import TeardownMode
from strategy import CycleRecord, RSIZone, TALendingSwapWETHStrategy, TradeState


def _config() -> dict:
    return {
        "chain": "base",
        "collateral_token": "USDC",
        "borrow_token": "WETH",
        "swap_protocol": "uniswap_v3",
        "rsi_period": 9,
        "rsi_timeframe": "5m",
        "rsi_high": "55",
        "rsi_low": "45",
        "target_health_factor": "1.5",
        "sell_hf_floor": "1.3",
        "emergency_hf": "1.2",
        "repay_bucket_min_usd": "0.06",
        "min_supply_usdc": "1",
        "min_trade_weth": "0.00001",
        "min_borrow_weth": "0.00001",
        "initial_ltv_fallback": "0.6",
        "max_slippage": "0.003",
        "max_price_impact": "0.10",
        "swap_fee_tier_bps": 500,
        "swap_pool_selection_mode": "fixed",
        "force_action": "",
    }


def _strategy(cfg: dict | None = None) -> TALendingSwapWETHStrategy:
    return TALendingSwapWETHStrategy(
        config=cfg or _config(),
        chain="base",
        wallet_address="0x" + "1" * 40,
    )


def _market(
    *,
    timestamp: datetime,
    usdc: Decimal,
    weth: Decimal,
    weth_price: Decimal,
    rsi: Decimal,
    hf: Decimal,
    collateral_usd: Decimal,
    debt_usd: Decimal,
    max_borrow_usd: Decimal,
) -> MarketSnapshot:
    market = MarketSnapshot(chain="base", wallet_address="0x" + "1" * 40, timestamp=timestamp)
    market.set_price("WETH", weth_price)
    market.set_balance(
        "USDC",
        TokenBalance(symbol="USDC", balance=usdc, balance_usd=usdc),
    )
    market.set_balance(
        "WETH",
        TokenBalance(symbol="WETH", balance=weth, balance_usd=weth * weth_price),
    )
    market.set_rsi("WETH", RSIData(value=rsi, period=9), timeframe="5m")
    market.set_position_health(
        "aave_v3",
        "",
        SimpleNamespace(
            health_factor=hf,
            collateral_value_usd=collateral_usd,
            debt_value_usd=debt_usd,
            max_borrow_usd=max_borrow_usd,
        ),
    )
    return market


def test_supply_all_usdc_initially() -> None:
    strategy = _strategy()
    market = _market(
        timestamp=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        usdc=Decimal("1000"),
        weth=Decimal("0"),
        weth_price=Decimal("2500"),
        rsi=Decimal("50"),
        hf=Decimal("999"),
        collateral_usd=Decimal("0"),
        debt_usd=Decimal("0"),
        max_borrow_usd=Decimal("0"),
    )

    intent = strategy.decide(market)
    assert intent is not None
    assert intent.intent_type.value == "SUPPLY"
    assert intent.token == "USDC"
    assert intent.amount == "all"


def test_borrow_to_target_hf_after_supply() -> None:
    strategy = _strategy()
    market = _market(
        timestamp=datetime(2026, 1, 1, 12, 5, tzinfo=UTC),
        usdc=Decimal("0"),
        weth=Decimal("0"),
        weth_price=Decimal("2500"),
        rsi=Decimal("50"),
        hf=Decimal("999"),
        collateral_usd=Decimal("1000"),
        debt_usd=Decimal("0"),
        max_borrow_usd=Decimal("800"),
    )

    intent = strategy.decide(market)
    assert intent is not None
    assert intent.intent_type.value == "BORROW"
    assert intent.borrow_token == "WETH"
    assert Decimal(str(intent.borrow_amount)) > Decimal("0")


def test_sell_on_neutral_to_high_cross() -> None:
    strategy = _strategy()
    strategy.base_inventory_weth = Decimal("1")
    strategy.prev_zone = RSIZone.NEUTRAL
    market = _market(
        timestamp=datetime(2026, 1, 1, 12, 10, tzinfo=UTC),
        usdc=Decimal("0"),
        weth=Decimal("1"),
        weth_price=Decimal("2500"),
        rsi=Decimal("60"),
        hf=Decimal("1.4"),
        collateral_usd=Decimal("1500"),
        debt_usd=Decimal("500"),
        max_borrow_usd=Decimal("800"),
    )

    intent = strategy.decide(market)
    assert intent is not None
    assert intent.intent_type.value == "SWAP"
    assert intent.from_token == "WETH"
    assert intent.to_token == "USDC"
    assert Decimal(str(intent.amount)) == Decimal("1")
    assert strategy.trade_state == TradeState.SOLD_FOR_USDC


def test_sell_on_low_to_high_cross() -> None:
    strategy = _strategy()
    strategy.base_inventory_weth = Decimal("1")
    strategy.prev_zone = RSIZone.LOW
    market = _market(
        timestamp=datetime(2026, 1, 1, 12, 10, tzinfo=UTC),
        usdc=Decimal("0"),
        weth=Decimal("1"),
        weth_price=Decimal("2500"),
        rsi=Decimal("60"),
        hf=Decimal("1.4"),
        collateral_usd=Decimal("1500"),
        debt_usd=Decimal("500"),
        max_borrow_usd=Decimal("800"),
    )

    intent = strategy.decide(market)
    assert intent is not None
    assert intent.intent_type.value == "SWAP"
    assert intent.from_token == "WETH"
    assert intent.to_token == "USDC"
    assert Decimal(str(intent.amount)) == Decimal("1")
    assert strategy.trade_state == TradeState.SOLD_FOR_USDC


def test_sell_allowed_even_when_hf_below_sell_floor() -> None:
    strategy = _strategy()
    strategy.base_inventory_weth = Decimal("1")
    strategy.prev_zone = RSIZone.NEUTRAL
    market = _market(
        timestamp=datetime(2026, 1, 1, 12, 10, tzinfo=UTC),
        usdc=Decimal("0"),
        weth=Decimal("1"),
        weth_price=Decimal("2500"),
        rsi=Decimal("60"),
        hf=Decimal("1.25"),
        collateral_usd=Decimal("1500"),
        debt_usd=Decimal("500"),
        max_borrow_usd=Decimal("800"),
    )

    intent = strategy.decide(market)
    assert intent is not None
    assert intent.intent_type.value == "SWAP"
    assert intent.from_token == "WETH"
    assert intent.to_token == "USDC"


def test_buyback_only_when_profitable() -> None:
    strategy = _strategy()
    strategy.trade_state = TradeState.SOLD_FOR_USDC
    strategy.prev_zone = RSIZone.NEUTRAL
    strategy.cycle = CycleRecord(sold_weth=Decimal("1"), usdc_proceeds=Decimal("2600"))

    market = _market(
        timestamp=datetime(2026, 1, 1, 12, 15, tzinfo=UTC),
        usdc=Decimal("2600"),
        weth=Decimal("0"),
        weth_price=Decimal("2500"),
        rsi=Decimal("40"),
        hf=Decimal("1.4"),
        collateral_usd=Decimal("1500"),
        debt_usd=Decimal("500"),
        max_borrow_usd=Decimal("800"),
    )
    market.best_dex_price = lambda **_: SimpleNamespace(amount_out_decimal=Decimal("1.01"))

    intent = strategy.decide(market)
    assert intent is not None
    assert intent.intent_type.value == "SWAP"
    assert intent.from_token == "USDC"
    assert intent.to_token == "WETH"


def test_buyback_executes_even_when_not_profitable_quote() -> None:
    strategy = _strategy()
    strategy.trade_state = TradeState.SOLD_FOR_USDC
    strategy.prev_zone = RSIZone.NEUTRAL
    strategy.cycle = CycleRecord(sold_weth=Decimal("1"), usdc_proceeds=Decimal("2500"))

    market = _market(
        timestamp=datetime(2026, 1, 1, 12, 20, tzinfo=UTC),
        usdc=Decimal("2500"),
        weth=Decimal("0"),
        weth_price=Decimal("2500"),
        rsi=Decimal("40"),
        hf=Decimal("1.4"),
        collateral_usd=Decimal("1500"),
        debt_usd=Decimal("500"),
        max_borrow_usd=Decimal("800"),
    )
    market.best_dex_price = lambda **_: SimpleNamespace(amount_out_decimal=Decimal("1"))

    intent = strategy.decide(market)
    assert intent is not None
    assert intent.intent_type.value == "SWAP"
    assert intent.from_token == "USDC"
    assert intent.to_token == "WETH"


def test_repay_from_excess_bucket_threshold() -> None:
    strategy = _strategy()
    strategy.excess_weth_bucket = Decimal("0.00003")
    market = _market(
        timestamp=datetime(2026, 1, 1, 12, 25, tzinfo=UTC),
        usdc=Decimal("0"),
        weth=Decimal("0"),
        weth_price=Decimal("2500"),
        rsi=Decimal("50"),
        hf=Decimal("1.4"),
        collateral_usd=Decimal("1500"),
        debt_usd=Decimal("300"),
        max_borrow_usd=Decimal("800"),
    )

    intent = strategy.decide(market)
    assert intent is not None
    assert intent.intent_type.value == "REPAY"


def test_emergency_hf_prioritizes_repay() -> None:
    strategy = _strategy()
    strategy.excess_weth_bucket = Decimal("0.01")
    market = _market(
        timestamp=datetime(2026, 1, 1, 12, 25, tzinfo=UTC),
        usdc=Decimal("0"),
        weth=Decimal("0"),
        weth_price=Decimal("2500"),
        rsi=Decimal("60"),
        hf=Decimal("1.1"),
        collateral_usd=Decimal("1500"),
        debt_usd=Decimal("300"),
        max_borrow_usd=Decimal("800"),
    )

    intent = strategy.decide(market)
    assert intent is not None
    assert intent.intent_type.value == "REPAY"


def test_buyback_does_not_wait_for_next_candle() -> None:
    strategy = _strategy()
    strategy.trade_state = TradeState.SOLD_FOR_USDC
    strategy.cycle = CycleRecord(sold_weth=Decimal("1"), usdc_proceeds=Decimal("2500"))

    market = _market(
        timestamp=datetime(2026, 1, 1, 12, 30, tzinfo=UTC),
        usdc=Decimal("2500"),
        weth=Decimal("0"),
        weth_price=Decimal("2500"),
        rsi=Decimal("40"),
        hf=Decimal("1.4"),
        collateral_usd=Decimal("1200"),
        debt_usd=Decimal("400"),
        max_borrow_usd=Decimal("800"),
    )

    first = strategy.decide(market)
    second = strategy.decide(market)
    assert first is not None
    assert second is not None
    assert first.intent_type.value == "SWAP"
    assert second.intent_type.value == "SWAP"


def test_buyback_execution_adds_only_extra_to_bucket() -> None:
    strategy = _strategy()
    strategy.base_inventory_weth = Decimal("1")
    strategy.prev_zone = RSIZone.NEUTRAL

    sell_market = _market(
        timestamp=datetime(2026, 1, 1, 12, 35, tzinfo=UTC),
        usdc=Decimal("0"),
        weth=Decimal("1"),
        weth_price=Decimal("2500"),
        rsi=Decimal("60"),
        hf=Decimal("1.4"),
        collateral_usd=Decimal("1200"),
        debt_usd=Decimal("400"),
        max_borrow_usd=Decimal("800"),
    )
    sell_intent = strategy.decide(sell_market)
    strategy.on_intent_executed(
        sell_intent,
        True,
        SimpleNamespace(swap_amounts=SimpleNamespace(amount_out_decimal=Decimal("2600"))),
    )

    strategy.prev_zone = RSIZone.NEUTRAL
    buy_market = _market(
        timestamp=datetime(2026, 1, 1, 12, 40, tzinfo=UTC),
        usdc=Decimal("2600"),
        weth=Decimal("0"),
        weth_price=Decimal("2500"),
        rsi=Decimal("40"),
        hf=Decimal("1.4"),
        collateral_usd=Decimal("1200"),
        debt_usd=Decimal("400"),
        max_borrow_usd=Decimal("800"),
    )
    buy_market.best_dex_price = lambda **_: SimpleNamespace(amount_out_decimal=Decimal("1.02"))
    buy_intent = strategy.decide(buy_market)
    strategy.on_intent_executed(
        buy_intent,
        True,
        SimpleNamespace(swap_amounts=SimpleNamespace(amount_out_decimal=Decimal("1.02"))),
    )

    assert strategy.base_inventory_weth == Decimal("1")
    assert strategy.excess_weth_bucket == Decimal("0.02")
    assert strategy.trade_state == TradeState.AVAILABLE_WETH


def test_teardown_intent_order() -> None:
    strategy = _strategy()
    strategy.has_collateral_position = True
    strategy.has_borrow_position = True
    strategy.trade_state = TradeState.SOLD_FOR_USDC
    strategy.cycle = CycleRecord(sold_weth=Decimal("1"), usdc_proceeds=Decimal("120"))

    intents = strategy.generate_teardown_intents(mode=TeardownMode.SOFT)
    types = [intent.intent_type.value for intent in intents]
    assert types == ["SWAP", "REPAY", "WITHDRAW"]
