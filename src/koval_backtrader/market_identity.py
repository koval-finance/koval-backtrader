# SPDX-License-Identifier: GPL-3.0-or-later
"""Validate configured identity using the MIT engine's market contract.

The earlier six-field block is accepted as an input alias. Results and replay
configs always use the engine's four canonical fields. No venue I/O occurs.
"""

from collections.abc import Mapping

from koval.engine.market_identity import MarketIdentity, canonical_symbol
from koval.engine.market_identity import resolve_market_identity as engine_market_identity

_LEGACY_FIELDS = {"venue", "market", "symbol", "contract_type", "base_currency", "quote_currency"}
_FIELDS = {"exchange", "market", "canonical_symbol", "contract_type"}


def resolve_market_identity(market: Mapping | None) -> MarketIdentity | None:
    if market is None:
        return None
    if not isinstance(market, Mapping):
        raise ValueError("market must be a mapping")
    legacy = bool(set(market) & {"venue", "symbol", "base_currency", "quote_currency"})
    required = _LEGACY_FIELDS if legacy else _FIELDS
    missing = sorted(required - set(market))
    if missing:
        raise ValueError(f"market requires {', '.join(missing)}")
    if set(market) - required:
        raise ValueError("market contains unknown fields")
    for name in required:
        if not isinstance(market[name], str) or not market[name].strip():
            raise ValueError(f"market.{name} must be a non-empty string")
    if legacy:
        base, quote = (market[name].strip().upper() for name in ("base_currency", "quote_currency"))
        if base == quote:
            raise ValueError("market.quote_currency must differ from base_currency")
        symbol = canonical_symbol(market["symbol"])
        if not symbol.startswith(base) or not symbol.endswith(quote):
            raise ValueError("market.symbol does not match its base and quote currencies")
    return engine_market_identity(
        exchange=market["venue" if legacy else "exchange"],
        market=market["market"],
        symbol=market["symbol" if legacy else "canonical_symbol"],
        contract_type=market["contract_type"].strip().lower(),
    )


def market_constraints(identity: MarketIdentity | None) -> str:
    if identity is None:
        return "unconstrained_label_only"
    return (
        "spot_no_shorts_no_leverage"
        if identity.market == "spot"
        else "linear_derivative_shorts_allowed"
    )


def validate_against_labels(
    identity: MarketIdentity | None, *, exchange: str, exchange_type: str
) -> None:
    if identity is None:
        return
    expected = engine_market_identity(
        exchange=exchange, market=exchange_type, symbol=identity.canonical_symbol
    )
    if identity.exchange != expected.exchange:
        raise ValueError("market.exchange does not match execution_config.exchange")
    if identity.market != expected.market:
        raise ValueError("market.market does not match execution_config.exchange_type")


def validate_against_model(identity: MarketIdentity | None, *, leverage: float) -> None:
    if identity is not None and identity.market == "spot" and leverage != 1.0:
        raise ValueError("spot leverage must be 1.0")


def rejects_direction(identity: MarketIdentity | None, direction: str) -> str | None:
    if identity is not None and identity.market == "spot" and direction == "short":
        return "spot_short_unsupported"
    return None
