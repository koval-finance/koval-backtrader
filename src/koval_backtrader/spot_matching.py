# SPDX-License-Identifier: GPL-3.0-or-later
"""Independent Decimal matcher for the engine's koval_spot_ohlcv_v1 policy."""

from decimal import ROUND_CEILING, ROUND_FLOOR, Context, Decimal, InvalidOperation, localcontext
from functools import wraps

import numpy as np
from koval.engine.spot_multi_order import SpotExecutionEventV1, SpotFeeV1

TERMINAL = frozenset({"filled", "canceled", "expired", "rejected"})
ZERO = Decimal(0)


def exact(function):
    @wraps(function)
    def call(*args, **kwargs):
        with localcontext(Context(prec=256)):
            return function(*args, **kwargs)

    return call


def text_number(value):
    result = format(value, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def number(value):
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, bool) or not isinstance(value, str | int | float | Decimal):
        raise ValueError("Spot number must be a decimal value")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("invalid Spot number") from exc
    if not result.is_finite() or abs(result) > Decimal("1e100"):
        raise ValueError("invalid Spot number")
    return result


def floor_step(value, step):
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def eligible(state, order):
    return (
        order.status not in TERMINAL | {"unknown"}
        and order.intent.activation_bar <= state.bar_index
    )


def status_event(state, order, status, timestamp_ms, **delta):
    """The stable simulator namespace matches the shared fixture identities."""
    sequence = state.event_sequence + 1
    return SpotExecutionEventV1(
        event_id=f"paper-e{sequence}",
        account_id=state.binding.account_id,
        session_id=state.binding.session_id,
        symbol=state.binding.symbol,
        client_order_id=order.intent.client_order_id,
        exchange_order_id=f"paper-{order.intent.client_order_id}",
        generation=order.intent.generation,
        source_sequence=sequence,
        timestamp_ms=timestamp_ms,
        side=order.intent.side,
        purpose=order.intent.purpose,
        status=status,
        cumulative_quantity=order.filled_quantity + delta.get("quantity", ZERO),
        cumulative_quote=order.filled_quote + delta.get("quote_notional", ZERO),
        **delta,
    )


@exact
def match_event(state, order, reference, *, at_open, timestamp_ms):
    """One actual delta; all orders debit the session's one volume allowance."""
    intent, rules, policy = order.intent, state.instrument, state.policy
    buy = intent.side == "buy"
    adjustment = (policy.spread_bps / 2 + policy.slippage_bps) / 10000
    price = reference * (1 + adjustment if buy else 1 - adjustment)
    rounding = ROUND_CEILING if buy else ROUND_FLOOR
    price = (price / rules.price_tick).to_integral_value(rounding=rounding) * rules.price_tick
    if intent.limit_price is not None:
        price = min(price, intent.limit_price) if buy else max(price, intent.limit_price)
    if not rules.minimum_price <= price <= rules.maximum_price or (
        rules.price_band_low is not None
        and not rules.price_band_low <= price <= rules.price_band_high
    ):
        return status_event(state, order, "rejected", timestamp_ms)
    market = intent.order_type == "market"
    step = rules.market_quantity_step if market else rules.quantity_step
    remaining = (
        (intent.quote_notional_cap - order.filled_quote) / price
        if intent.quote_notional_cap is not None
        else intent.quantity - order.filled_quantity
    )
    requested = floor_step(remaining, step)
    if (
        market
        and not order.filled_quantity
        and (
            requested < (rules.market_minimum_quantity or rules.minimum_quantity)
            or requested > (rules.market_maximum_quantity or rules.maximum_quantity)
            or rules.apply_notional_to_market
            and not rules.minimum_notional <= requested * price <= rules.maximum_notional
        )
    ):
        return status_event(state, order, "rejected", timestamp_ms)
    quantity = floor_step(min(requested, state.liquidity_allowance - state.liquidity_used), step)
    if quantity <= 0:
        return None
    notional = quantity * price
    rate = policy.taker_fee_bps if market or at_open else policy.maker_fee_bps
    fee_value = notional * rate / 10000
    asset = policy.fee_asset
    fee_quantity = fee_value
    third_asset = asset not in {state.binding.base_asset, state.binding.quote_asset}
    if asset == state.binding.base_asset:
        fee_quantity /= price
    elif third_asset:
        fee_quantity /= policy.third_asset_quote_price
    fees = (
        (
            SpotFeeV1(
                asset=asset,
                quantity=fee_quantity,
                source=policy.fee_source,
                quote_value=fee_value if third_asset else None,
            ),
        )
        if fee_quantity
        else ()
    )
    full = quantity == requested
    if intent.quote_notional_cap is not None:
        full = (
            floor_step((intent.quote_notional_cap - order.filled_quote - notional) / price, step)
            == 0
        )
    return status_event(
        state,
        order,
        "filled" if full else "partial",
        timestamp_ms,
        trade_id=f"paper-t{state.event_sequence + 1}",
        quantity=quantity,
        price=price,
        quote_notional=notional,
        fees=fees,
    )
