from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from almanak.framework.data import DataUnavailableError
from almanak.framework.intents import Intent
from almanak.framework.market import (
    BalanceUnavailableError,
    DexQuoteUnavailableError,
    HealthUnavailableError,
    MarketSnapshot,
    PriceUnavailableError,
    RSIUnavailableError,
)
from almanak.framework.strategies import IntentStrategy, almanak_strategy
from almanak.framework.teardown import PositionInfo, PositionType, TeardownMode, TeardownPositionSummary

logger = logging.getLogger(__name__)


class TradeState(StrEnum):
    AVAILABLE_WETH = "available_weth"
    SOLD_FOR_USDC = "sold_for_usdc"


class RSIZone(StrEnum):
    LOW = "low"
    NEUTRAL = "neutral"
    HIGH = "high"


@dataclass
class CycleRecord:
    sold_weth: Decimal = Decimal("0")
    usdc_proceeds: Decimal = Decimal("0")
    sold_price: Decimal = Decimal("0")
    sold_at: datetime | None = None


@almanak_strategy(
    name="t_a_lending_swap_w_e_t_h",
    description="TA lending+swap strategy: USDC collateral, WETH borrow inventory on Base",
    version="1.0.0",
    author="Almanak",
    tags=["lending", "swap", "aave_v3", "uniswap_v3", "rsi"],
    supported_chains=["base"],
    supported_protocols=["aave_v3", "uniswap_v3"],
    intent_types=["SUPPLY", "BORROW", "SWAP", "REPAY", "WITHDRAW", "HOLD"],
    default_chain="base",
)
class TALendingSwapWETHStrategy(IntentStrategy):
    def supports_teardown(self) -> bool:
        return True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)

        self.collateral_token = str(self.get_config("collateral_token", "USDC"))
        self.borrow_token = str(self.get_config("borrow_token", "WETH"))
        self.swap_protocol = str(self.get_config("swap_protocol", "uniswap_v3"))
        self.rsi_period = int(self.get_config("rsi_period", 9))
        self.rsi_timeframe = str(self.get_config("rsi_timeframe", "5m"))
        self.rsi_high = Decimal(str(self.get_config("rsi_high", "55")))
        self.rsi_low = Decimal(str(self.get_config("rsi_low", "45")))
        self.target_health_factor = Decimal(str(self.get_config("target_health_factor", "1.5")))
        self.sell_hf_floor = Decimal(str(self.get_config("sell_hf_floor", "1.3")))
        self.emergency_hf = Decimal(str(self.get_config("emergency_hf", "1.2")))
        self.repay_bucket_min_usd = Decimal(str(self.get_config("repay_bucket_min_usd", "0.06")))
        self.min_supply_usdc = Decimal(str(self.get_config("min_supply_usdc", "1")))
        self.min_trade_weth = Decimal(str(self.get_config("min_trade_weth", "0.00001")))
        self.min_borrow_weth = Decimal(str(self.get_config("min_borrow_weth", "0.00001")))
        self.initial_ltv_fallback = Decimal(str(self.get_config("initial_ltv_fallback", "0.6")))
        self.max_slippage = Decimal(str(self.get_config("max_slippage", "0.003")))
        self.max_price_impact = Decimal(str(self.get_config("max_price_impact", "0.10")))
        self.swap_fee_tier_bps = int(self.get_config("swap_fee_tier_bps", 500))
        self.swap_pool_selection_mode = str(self.get_config("swap_pool_selection_mode", "fixed"))
        self.force_action = str(self.get_config("force_action", "") or "").lower()

        self.trade_state = TradeState.AVAILABLE_WETH
        self.prev_zone = RSIZone.NEUTRAL
        self.last_processed_candle_key = ""
        self.base_inventory_weth = Decimal("0")
        self.excess_weth_bucket = Decimal("0")
        self.cycle = CycleRecord()
        self.pending_action = ""
        self.has_collateral_position = False
        self.has_borrow_position = False
        self.last_rsi_value: Decimal | None = None

    def decide(self, market: MarketSnapshot) -> Intent | None:
        if self.force_action:
            return self._forced_intent(market)

        try:
            market_data = self._read_market_data(market)
        except ValueError as exc:
            return Intent.hold(reason=str(exc))

        current_rsi = Decimal(str(market_data["rsi_value"]))
        previous_rsi = self.last_rsi_value
        self.last_rsi_value = current_rsi
        rsi_trace = self._format_rsi_trace(current_rsi=current_rsi, previous_rsi=previous_rsi)
        logger.info(
            "RSI snapshot: %s | prev_zone=%s | trade_state=%s",
            rsi_trace,
            self.prev_zone.value,
            self.trade_state.value,
        )

        if market_data["health_factor"] < self.emergency_hf:
            emergency_intent = self._emergency_intent(market_data)
            if emergency_intent is not None:
                return emergency_intent

        if self._should_supply_all_usdc(market_data):
            self.has_collateral_position = True
            return Intent.supply(
                protocol="aave_v3",
                token=self.collateral_token,
                amount="all",
                use_as_collateral=True,
                chain=self.chain,
            )

        borrow_intent = self._maybe_borrow_to_target_hf(market_data)
        if borrow_intent is not None:
            self.has_borrow_position = True
            return borrow_intent

        repay_intent = self._maybe_repay_from_bucket(market_data)
        if repay_intent is not None:
            return repay_intent

        candle_key = self._candle_key_5m(market_data["timestamp"])
        if candle_key == self.last_processed_candle_key:
            return Intent.hold(reason=f"Waiting for next 5m candle close ({rsi_trace})")

        current_zone = self._rsi_zone(current_rsi)

        if self.trade_state == TradeState.AVAILABLE_WETH and self.prev_zone != RSIZone.HIGH and current_zone == RSIZone.HIGH:
            if market_data["health_factor"] < self.sell_hf_floor:
                self.last_processed_candle_key = candle_key
                self.prev_zone = current_zone
                return Intent.hold(reason=f"HF below sell floor ({rsi_trace})")

            if self.base_inventory_weth <= 0:
                self.base_inventory_weth = market_data["weth_balance"]

            if self.base_inventory_weth >= self.min_trade_weth:
                self.cycle = CycleRecord(
                    sold_weth=self.base_inventory_weth,
                    sold_price=market_data["weth_price"],
                    sold_at=market_data["timestamp"],
                )
                self.trade_state = TradeState.SOLD_FOR_USDC
                self.pending_action = "sell"
                self.last_processed_candle_key = candle_key
                self.prev_zone = current_zone
                return Intent.swap(
                    from_token=self.borrow_token,
                    to_token=self.collateral_token,
                    amount=self.base_inventory_weth,
                    max_slippage=self.max_slippage,
                    max_price_impact=self.max_price_impact,
                    protocol=self.swap_protocol,
                    chain=self.chain,
                )

        if self.trade_state == TradeState.SOLD_FOR_USDC and self.prev_zone == RSIZone.NEUTRAL and current_zone == RSIZone.LOW:
            estimated_weth = self._estimate_buyback_weth(self.cycle.usdc_proceeds, market_data["weth_price"], market)
            if estimated_weth > self.cycle.sold_weth:
                self.pending_action = "buyback"
                self.last_processed_candle_key = candle_key
                self.prev_zone = current_zone
                return Intent.swap(
                    from_token=self.collateral_token,
                    to_token=self.borrow_token,
                    amount=self.cycle.usdc_proceeds,
                    max_slippage=self.max_slippage,
                    max_price_impact=self.max_price_impact,
                    protocol=self.swap_protocol,
                    chain=self.chain,
                )

        self.last_processed_candle_key = candle_key
        self.prev_zone = current_zone
        return Intent.hold(reason=f"No signal ({rsi_trace})")

    def on_intent_executed(self, intent: Any, success: bool, result: Any) -> None:
        if not success:
            self.pending_action = ""
            return

        intent_type = getattr(getattr(intent, "intent_type", None), "value", "")

        if intent_type == "SUPPLY":
            self.has_collateral_position = True
            return

        if intent_type == "BORROW":
            self.has_borrow_position = True
            if self.base_inventory_weth <= 0 and hasattr(intent, "borrow_amount"):
                self.base_inventory_weth = Decimal(str(intent.borrow_amount))
            return

        if intent_type == "REPAY":
            repaid = Decimal(str(getattr(intent, "amount", "0") or "0"))
            if repaid > 0:
                self.excess_weth_bucket = max(Decimal("0"), self.excess_weth_bucket - repaid)
            return

        if intent_type == "SWAP" and self.pending_action == "sell":
            sold = self.cycle.sold_weth
            proceeds = self._extract_swap_out_amount(result)
            if proceeds <= 0 and self.cycle.sold_price > 0:
                proceeds = sold * self.cycle.sold_price
            self.cycle.usdc_proceeds = proceeds
            self.base_inventory_weth = sold
            self.pending_action = ""
            return

        if intent_type == "SWAP" and self.pending_action == "buyback":
            bought_weth = self._extract_swap_out_amount(result)
            extra_weth = max(Decimal("0"), bought_weth - self.cycle.sold_weth)
            self.excess_weth_bucket += extra_weth
            self.base_inventory_weth = self.cycle.sold_weth
            self.trade_state = TradeState.AVAILABLE_WETH
            self.cycle = CycleRecord()
            self.pending_action = ""

    def save_persistent_state(self) -> dict[str, Any]:
        return {
            "trade_state": self.trade_state.value,
            "prev_zone": self.prev_zone.value,
            "last_processed_candle_key": self.last_processed_candle_key,
            "base_inventory_weth": str(self.base_inventory_weth),
            "excess_weth_bucket": str(self.excess_weth_bucket),
            "cycle": {
                "sold_weth": str(self.cycle.sold_weth),
                "usdc_proceeds": str(self.cycle.usdc_proceeds),
                "sold_price": str(self.cycle.sold_price),
                "sold_at": self.cycle.sold_at.isoformat() if self.cycle.sold_at else None,
            },
            "has_collateral_position": self.has_collateral_position,
            "has_borrow_position": self.has_borrow_position,
            "last_rsi_value": str(self.last_rsi_value) if self.last_rsi_value is not None else None,
        }

    def load_persistent_state(self, state: dict[str, Any]) -> None:
        self.trade_state = TradeState(state.get("trade_state", TradeState.AVAILABLE_WETH.value))
        self.prev_zone = RSIZone(state.get("prev_zone", RSIZone.NEUTRAL.value))
        self.last_processed_candle_key = str(state.get("last_processed_candle_key", ""))
        self.base_inventory_weth = Decimal(str(state.get("base_inventory_weth", "0")))
        self.excess_weth_bucket = Decimal(str(state.get("excess_weth_bucket", "0")))

        cycle_state = state.get("cycle", {})
        sold_at_raw = cycle_state.get("sold_at")
        sold_at = datetime.fromisoformat(sold_at_raw) if sold_at_raw else None
        self.cycle = CycleRecord(
            sold_weth=Decimal(str(cycle_state.get("sold_weth", "0"))),
            usdc_proceeds=Decimal(str(cycle_state.get("usdc_proceeds", "0"))),
            sold_price=Decimal(str(cycle_state.get("sold_price", "0"))),
            sold_at=sold_at,
        )

        self.has_collateral_position = bool(state.get("has_collateral_position", False))
        self.has_borrow_position = bool(state.get("has_borrow_position", False))

        last_rsi_value = state.get("last_rsi_value")
        self.last_rsi_value = Decimal(str(last_rsi_value)) if last_rsi_value is not None else None

    def get_status(self) -> dict[str, Any]:
        return {
            "strategy": "t_a_lending_swap_w_e_t_h",
            "chain": self.chain,
            "trade_state": self.trade_state.value,
            "base_inventory_weth": str(self.base_inventory_weth),
            "excess_weth_bucket": str(self.excess_weth_bucket),
            "cycle_sold_weth": str(self.cycle.sold_weth),
            "cycle_usdc_proceeds": str(self.cycle.usdc_proceeds),
            "last_rsi_value": str(self.last_rsi_value) if self.last_rsi_value is not None else None,
        }

    def get_open_positions(self) -> TeardownPositionSummary:
        positions: list[PositionInfo] = []

        if self.has_borrow_position:
            positions.append(
                PositionInfo(
                    position_type=PositionType.BORROW,
                    position_id=f"aave-borrow-{self.borrow_token.lower()}",
                    chain=self.chain,
                    protocol="aave_v3",
                    value_usd=Decimal("1"),
                    details={"token": self.borrow_token},
                )
            )

        if self.has_collateral_position:
            positions.append(
                PositionInfo(
                    position_type=PositionType.SUPPLY,
                    position_id=f"aave-supply-{self.collateral_token.lower()}",
                    chain=self.chain,
                    protocol="aave_v3",
                    value_usd=Decimal("1"),
                    details={"token": self.collateral_token},
                )
            )

        return TeardownPositionSummary(
            deployment_id=self.deployment_id,
            timestamp=datetime.now(UTC),
            positions=positions,
        )

    def generate_teardown_intents(self, mode: TeardownMode = TeardownMode.SOFT, market: MarketSnapshot | None = None) -> list[Intent]:
        intents: list[Intent] = []

        if self.trade_state == TradeState.SOLD_FOR_USDC and self.cycle.usdc_proceeds > 0:
            intents.append(
                Intent.swap(
                    from_token=self.collateral_token,
                    to_token=self.borrow_token,
                    amount=self.cycle.usdc_proceeds,
                    max_slippage=Decimal("0.01") if mode == TeardownMode.HARD else self.max_slippage,
                    max_price_impact=self.max_price_impact,
                    protocol=self.swap_protocol,
                    chain=self.chain,
                )
            )

        if self.has_borrow_position:
            intents.append(
                Intent.repay(
                    protocol="aave_v3",
                    token=self.borrow_token,
                    repay_full=True,
                    interest_rate_mode="variable",
                    chain=self.chain,
                )
            )

        if self.has_collateral_position:
            intents.append(
                Intent.withdraw(
                    protocol="aave_v3",
                    token=self.collateral_token,
                    amount=Decimal("0"),
                    withdraw_all=True,
                    chain=self.chain,
                )
            )

        return intents

    def _forced_intent(self, market: MarketSnapshot) -> Intent:
        if self.force_action == "supply":
            return Intent.supply(
                protocol="aave_v3",
                token=self.collateral_token,
                amount="all",
                use_as_collateral=True,
                chain=self.chain,
            )
        if self.force_action == "borrow":
            data = self._read_market_data(market)
            borrow = self._maybe_borrow_to_target_hf(data)
            if borrow is None:
                raise ValueError("force_action=borrow requires positive borrow headroom")
            return borrow
        if self.force_action == "sell":
            amount = self.base_inventory_weth if self.base_inventory_weth > 0 else self._read_market_data(market)["weth_balance"]
            if amount < self.min_trade_weth:
                raise ValueError("force_action=sell requires WETH inventory")
            self.pending_action = "sell"
            self.cycle = CycleRecord(sold_weth=amount)
            self.trade_state = TradeState.SOLD_FOR_USDC
            return Intent.swap(
                from_token=self.borrow_token,
                to_token=self.collateral_token,
                amount=amount,
                max_slippage=self.max_slippage,
                max_price_impact=self.max_price_impact,
                protocol=self.swap_protocol,
                chain=self.chain,
            )
        if self.force_action == "buyback":
            if self.cycle.usdc_proceeds <= 0:
                raise ValueError("force_action=buyback requires cycle.usdc_proceeds > 0")
            self.pending_action = "buyback"
            return Intent.swap(
                from_token=self.collateral_token,
                to_token=self.borrow_token,
                amount=self.cycle.usdc_proceeds,
                max_slippage=self.max_slippage,
                max_price_impact=self.max_price_impact,
                protocol=self.swap_protocol,
                chain=self.chain,
            )
        if self.force_action == "repay":
            amount = self.excess_weth_bucket if self.excess_weth_bucket > 0 else self.min_trade_weth
            return Intent.repay(
                protocol="aave_v3",
                token=self.borrow_token,
                amount=amount,
                repay_full=False,
                interest_rate_mode="variable",
                chain=self.chain,
            )
        if self.force_action == "withdraw":
            return Intent.withdraw(
                protocol="aave_v3",
                token=self.collateral_token,
                amount=Decimal("0"),
                withdraw_all=True,
                chain=self.chain,
            )
        raise ValueError(f"Unknown force_action: {self.force_action!r}")

    def _read_market_data(self, market: MarketSnapshot) -> dict[str, Decimal | datetime]:
        try:
            usdc_balance = Decimal(str(market.balance(self.collateral_token).balance))
            weth_balance = Decimal(str(market.balance(self.borrow_token).balance))
            weth_price = Decimal(str(market.price(self.borrow_token)))
            rsi_value = Decimal(str(market.rsi(self.borrow_token, period=self.rsi_period, timeframe=self.rsi_timeframe).value))
            health = market.position_health(protocol="aave_v3", market_id="")
        except (
            PriceUnavailableError,
            BalanceUnavailableError,
            RSIUnavailableError,
            HealthUnavailableError,
            DataUnavailableError,
            ValueError,
        ) as exc:
            logger.warning("Market data unavailable: %s", exc)
            raise ValueError(f"Market data unavailable: {exc}") from exc

        return {
            "timestamp": market.timestamp,
            "usdc_balance": usdc_balance,
            "weth_balance": weth_balance,
            "weth_price": weth_price,
            "rsi_value": rsi_value,
            "health_factor": Decimal(str(getattr(health, "health_factor", "999"))),
            "collateral_value_usd": Decimal(str(getattr(health, "collateral_value_usd", "0"))),
            "debt_value_usd": Decimal(str(getattr(health, "debt_value_usd", "0"))),
            "max_borrow_usd": Decimal(str(getattr(health, "max_borrow_usd", "0"))),
        }

    def _should_supply_all_usdc(self, data: dict[str, Decimal | datetime]) -> bool:
        collateral_value = Decimal(str(data["collateral_value_usd"]))
        usdc_balance = Decimal(str(data["usdc_balance"]))
        if collateral_value > 0:
            self.has_collateral_position = True
            return False
        return usdc_balance >= self.min_supply_usdc

    def _maybe_borrow_to_target_hf(self, data: dict[str, Decimal | datetime]) -> Intent | None:
        collateral_usd = Decimal(str(data["collateral_value_usd"]))
        debt_usd = Decimal(str(data["debt_value_usd"]))
        max_borrow_usd = Decimal(str(data["max_borrow_usd"]))
        weth_price = Decimal(str(data["weth_price"]))

        if collateral_usd <= 0 or debt_usd > 0:
            if debt_usd > 0:
                self.has_borrow_position = True
            return None

        if max_borrow_usd > 0:
            target_debt_usd = max_borrow_usd / self.target_health_factor
            projected_hf = max_borrow_usd / target_debt_usd if target_debt_usd > 0 else Decimal("999")
        else:
            target_debt_usd = collateral_usd * self.initial_ltv_fallback
            projected_hf = collateral_usd / target_debt_usd if target_debt_usd > 0 else Decimal("999")

        if projected_hf < self.target_health_factor:
            return None

        borrow_amount = target_debt_usd / weth_price if weth_price > 0 else Decimal("0")
        if borrow_amount < self.min_borrow_weth:
            return None

        return Intent.borrow(
            protocol="aave_v3",
            collateral_token=self.collateral_token,
            collateral_amount=Decimal("0"),
            borrow_token=self.borrow_token,
            borrow_amount=borrow_amount,
            interest_rate_mode="variable",
            chain=self.chain,
        )

    def _maybe_repay_from_bucket(self, data: dict[str, Decimal | datetime]) -> Intent | None:
        bucket_usd = self.excess_weth_bucket * Decimal(str(data["weth_price"]))
        debt_usd = Decimal(str(data["debt_value_usd"]))
        if debt_usd <= 0:
            return None
        if bucket_usd < self.repay_bucket_min_usd:
            return None

        debt_weth = debt_usd / Decimal(str(data["weth_price"]))
        repay_amount = min(self.excess_weth_bucket, debt_weth)
        return Intent.repay(
            protocol="aave_v3",
            token=self.borrow_token,
            amount=repay_amount,
            repay_full=False,
            interest_rate_mode="variable",
            chain=self.chain,
        )

    def _emergency_intent(self, data: dict[str, Decimal | datetime]) -> Intent | None:
        debt_usd = Decimal(str(data["debt_value_usd"]))
        if debt_usd <= 0:
            return None

        if self.excess_weth_bucket > 0:
            debt_weth = debt_usd / Decimal(str(data["weth_price"]))
            repay_amount = min(self.excess_weth_bucket, debt_weth)
            return Intent.repay(
                protocol="aave_v3",
                token=self.borrow_token,
                amount=repay_amount,
                repay_full=False,
                interest_rate_mode="variable",
                chain=self.chain,
            )

        if self.trade_state == TradeState.SOLD_FOR_USDC and self.cycle.usdc_proceeds > 0:
            return Intent.swap(
                from_token=self.collateral_token,
                to_token=self.borrow_token,
                amount=self.cycle.usdc_proceeds,
                max_slippage=Decimal("0.01"),
                max_price_impact=self.max_price_impact,
                protocol=self.swap_protocol,
                chain=self.chain,
            )

        return Intent.hold(reason="Emergency mode with no repay inventory")

    def _estimate_buyback_weth(self, usdc_amount: Decimal, weth_price: Decimal, market: MarketSnapshot) -> Decimal:
        if usdc_amount <= 0:
            return Decimal("0")
        try:
            quote = market.best_dex_price(
                token_in=self.collateral_token,
                token_out=self.borrow_token,
                amount=usdc_amount,
                dexs=[self.swap_protocol],
            )
        except (DexQuoteUnavailableError, DataUnavailableError, ValueError, NotImplementedError):
            return Decimal("0")

        candidate = self._extract_quote_amount_out(quote)
        if candidate > 0:
            return candidate

        if weth_price <= 0:
            return Decimal("0")
        return usdc_amount / weth_price

    def _extract_quote_amount_out(self, quote: Any) -> Decimal:
        paths = [
            ("amount_out_decimal",),
            ("amount_out",),
            ("best_amount_out_decimal",),
            ("best_amount_out",),
            ("best_quote", "amount_out_decimal"),
            ("best_quote", "amount_out"),
            ("quote", "amount_out_decimal"),
            ("quote", "amount_out"),
        ]
        for path in paths:
            cursor = quote
            for attr in path:
                cursor = getattr(cursor, attr, None)
                if cursor is None:
                    break
            if cursor is not None:
                return Decimal(str(cursor))
        return Decimal("0")

    def _extract_swap_out_amount(self, result: Any) -> Decimal:
        swap_amounts = getattr(result, "swap_amounts", None)
        if swap_amounts is not None:
            out_decimal = getattr(swap_amounts, "amount_out_decimal", None)
            if out_decimal is not None:
                return Decimal(str(out_decimal))
            out_raw = getattr(swap_amounts, "amount_out", None)
            if out_raw is not None:
                return Decimal(str(out_raw))
        return Decimal("0")

    def _format_rsi_trace(self, current_rsi: Decimal, previous_rsi: Decimal | None) -> str:
        previous_text = "n/a" if previous_rsi is None else format(previous_rsi, "f")
        return f"rsi_current={format(current_rsi, 'f')} rsi_previous={previous_text}"

    def _rsi_zone(self, value: Decimal) -> RSIZone:
        if value > self.rsi_high:
            return RSIZone.HIGH
        if value < self.rsi_low:
            return RSIZone.LOW
        return RSIZone.NEUTRAL

    def _candle_key_5m(self, ts: datetime) -> str:
        ts_utc = ts.astimezone(UTC)
        bucket = (ts_utc.minute // 5) * 5
        return ts_utc.replace(minute=bucket, second=0, microsecond=0).isoformat()
