# SPDX-License-Identifier: GPL-3.0-or-later
"""Independent linear Futures accounting used for adapter parity vectors."""

from __future__ import annotations

from decimal import Decimal


def _decimal(value: Decimal | str, *, name: str) -> Decimal:
    parsed = Decimal(str(value))
    if not parsed.is_finite():
        raise ValueError(f"{name} must be finite")
    return parsed


def calculate_futures_snapshot(
    *,
    initial_wallet_balance: Decimal,
    realized_pnl: Decimal,
    trading_fees: Decimal,
    funding: Decimal,
    liquidation_fees: Decimal,
    side: str,
    quantity: Decimal,
    entry_price: Decimal,
    mark_price: Decimal,
    leverage: Decimal,
    contract_size: Decimal,
    maintenance_rate: Decimal,
    maintenance_amount: Decimal,
    margin_mode: str = "cross",
    isolated_margin: Decimal | None = None,
) -> dict[str, Decimal | bool | str]:
    """Calculate the plugin vector without calling the MIT accounting function."""
    if side not in {"buy", "sell"}:
        raise ValueError("position side must be buy or sell")
    initial = _decimal(initial_wallet_balance, name="initial wallet balance")
    realized = _decimal(realized_pnl, name="realized PnL")
    fees = _decimal(trading_fees, name="trading fees")
    funding_cashflow = _decimal(funding, name="funding")
    liquidation_cost = _decimal(liquidation_fees, name="liquidation fees")
    qty = _decimal(quantity, name="quantity")
    entry = _decimal(entry_price, name="entry price")
    mark = _decimal(mark_price, name="mark price")
    selected_leverage = _decimal(leverage, name="leverage")
    multiplier = _decimal(contract_size, name="contract size")
    maintenance = _decimal(maintenance_rate, name="maintenance rate")
    maintenance_deduction = _decimal(maintenance_amount, name="maintenance amount")
    if min(initial, qty, entry, mark, selected_leverage, multiplier) <= 0:
        raise ValueError("wallet, position, price, leverage, and contract size must be positive")
    if fees < 0 or liquidation_cost < 0 or not Decimal("0") <= maintenance < Decimal("1"):
        raise ValueError("fee costs and maintenance terms are invalid")
    if margin_mode not in {"cross", "isolated"}:
        raise ValueError("margin mode must be cross or isolated")

    wallet = initial + realized + funding_cashflow - fees - liquidation_cost
    sign = Decimal("1") if side == "buy" else Decimal("-1")
    unrealized = (mark - entry) * qty * multiplier * sign
    equity = wallet + unrealized
    notional = mark * qty * multiplier
    initial_margin = entry * qty * multiplier / selected_leverage
    if margin_mode == "isolated":
        if isolated_margin is None:
            raise ValueError("isolated margin is required")
        risk_cash = _decimal(isolated_margin, name="isolated margin")
        if risk_cash < initial_margin:
            raise ValueError("isolated margin must cover initial margin")
        if risk_cash > wallet:
            raise ValueError("isolated margin exceeds wallet balance")
        margin_used = risk_cash
        available_balance = wallet - risk_cash
    else:
        if isolated_margin is not None:
            raise ValueError("cross mode cannot carry isolated margin")
        risk_cash = wallet
        margin_used = notional / selected_leverage
        available_balance = equity - margin_used
    position_margin_equity = risk_cash + unrealized
    maintenance_margin = max(Decimal("0"), notional * maintenance - maintenance_deduction)
    entry_notional = entry * qty * multiplier
    exposure = qty * multiplier
    if side == "buy":
        liquidation_price = (entry_notional - risk_cash - maintenance_deduction) / (
            exposure * (Decimal("1") - maintenance)
        )
    else:
        liquidation_price = (risk_cash + entry_notional + maintenance_deduction) / (
            exposure * (Decimal("1") + maintenance)
        )
    return {
        "margin_mode": margin_mode,
        "wallet_balance": wallet,
        "unrealized_pnl": unrealized,
        "equity": equity,
        "margin_used": margin_used,
        "available_balance": available_balance,
        "position_margin_equity": position_margin_equity,
        "maintenance_margin": maintenance_margin,
        "estimated_liquidation_price": max(Decimal("0"), liquidation_price),
        "liquidated": position_margin_equity <= maintenance_margin,
    }


__all__ = ["calculate_futures_snapshot"]
