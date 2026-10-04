# SPDX-License-Identifier: GPL-3.0-or-later
"""Historical feature negotiation without eager unpublished MIT imports."""

from decimal import Decimal, InvalidOperation

from koval.engine.backtest_engine import ProtocolVersionError
from koval.strategy.base.declarative import DeclarativeStrategy
from koval.strategy.base.trade_setup import TradeSetup

from koval_backtrader.execution_config import REALISTIC_VERSION

POSITION_FEATURES = frozenset({"position_exit_v1", "optional_take_profit_v1"})


def graph_requirements(graph):
    required = set()
    for block in graph.get("blocks") or []:
        if block.get("type") == "exec.position_exit":
            required.add("position_exit_v1")
        if (
            block.get("type") == "exec.order_constructor"
            and (block.get("params") or {}).get("take_profit_mode") == "disabled"
        ):
            required.add("optional_take_profit_v1")
    return required


def engine_contract_available():
    return "take_profit_mode" in TradeSetup.__dataclass_fields__ and hasattr(
        DeclarativeStrategy, "get_position_exit_request"
    )


def supports_position_features(model):
    market = model.market
    return (
        model.version == REALISTIC_VERSION
        and market is not None
        and market.exchange == "binance"
        and market.market == "spot"
        and model.leverage == 1
    )


def require_features(required, offered):
    missing = set(required) - set(offered)
    if missing:
        raise ProtocolVersionError(
            "engine is missing required execution capabilities: " + ", ".join(sorted(missing))
        )


def validate_stop_only_update(*, side, current_stop, stop_price):
    """The MIT bracket validator requires a target; an absent target has no bound."""
    try:
        old, stop = (Decimal(str(value)) for value in (current_stop, stop_price))
    except InvalidOperation as exc:
        raise ValueError("protection prices must be positive and finite") from exc
    if any(not value.is_finite() or value <= 0 for value in (old, stop)):
        raise ValueError("protection prices must be positive and finite")
    if side not in {"buy", "sell"}:
        raise ValueError("protection side must be buy or sell")
    if (side == "buy" and stop < old) or (side == "sell" and stop > old):
        raise ValueError("protection update must not widen stop risk")
