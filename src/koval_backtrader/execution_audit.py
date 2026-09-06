# SPDX-License-Identifier: GPL-3.0-or-later
"""Plain execution disclosure and reconciliation. Never debit costs here."""

from __future__ import annotations

import math
import platform
from hashlib import sha256
from importlib.metadata import version
from importlib.resources import files

from koval.engine.history_window import DEFAULT_HISTORY_BARS

from koval_backtrader.execution_config import FIXED_VERSION, ExecutionModel


def _implementation_digest() -> str:
    """Identify editable/unreleased code as well as published package versions."""
    digest = sha256()
    for source in sorted(files("koval_backtrader").iterdir(), key=lambda item: item.name):
        if source.is_file() and source.name.endswith(".py"):
            digest.update(source.name.encode() + b"\0" + source.read_bytes() + b"\0")
    return digest.hexdigest()


def execution_metadata(model: ExecutionModel) -> dict:
    fixed = model.version == FIXED_VERSION
    return {
        "version": model.version,
        "resolved_config": model.as_config(),
        "implementation_sha256": _implementation_digest(),
        "observed_inputs": ["supplied OHLCV candles; price per base unit"],
        "assumptions": {
            "price_adjustment": "adverse_half_spread_plus_slippage" if fixed else "none",
            "spread_bps": model.spread_bps,
            "slippage_bps": model.slippage_bps,
            "range_cap": "none_synthetic_cost_prices" if fixed else "unadjusted_matching",
            "limits": "entry_limits_never_worse_than_limit",
            "take_profit": (
                "market_on_touch_full_adverse_adjustment" if fixed else "limit_at_target"
            ),
            "commission_policy": "uniform",
            "commission_bps": model.commission_bps,
            "commission_currency": "quote",
            "maker_taker_classification": "unavailable",
            "funding": "unavailable",
            "liquidity": "unlimited_full_fills",
            "additional_latency_bars": 0,
            "market_timing": "next_bar_open",
            "protection_timing": "bar_after_entry_fill",
            "stop_gap": "open_before_price_adjustment",
            "intrabar_ordering": "queue_order_stop_submitted_first",
            "accounting": (
                "linear_cash_leverage_margin" if fixed else "backtrader_stocklike_linear_cash"
            ),
            "leverage": model.leverage,
            "affordability": (
                "notional_over_leverage_plus_commission_le_equity_minus_margin"
                if fixed
                else "backtrader_cash_check_longs_only"
            ),
            "end_of_data": "mark_open_position_at_last_close_no_forced_exit",
            "htf_availability": "closed_before_primary_decision",
            "htf_availability_legacy_defect_fixed": True,
            "history_bars": DEFAULT_HISTORY_BARS,
        },
        "unmodelled_effects": [
            "historical_bid_ask",
            "order_book_depth",
            "queue_priority",
            "maker_taker_fees",
            "fee_discounts_and_fee_currency_conversion",
            "funding",
            "borrow_interest",
            "volume_participation",
            "partial_fills",
            "volatility_slippage",
            "size_dependent_impact",
            "additional_latency",
            "exchange_filters",
            "venue_rejections",
            "downtime",
            "maintenance_margin",
            "liquidation",
            "intrabar_price_path",
            "intrabar_drawdown",
        ],
        "data_quality": "caller_supplied_not_independently_verified",
        "software": {
            **{
                name: version(name)
                for name in ("koval-backtrader", "koval-engine", "backtrader", "numpy", "pandas")
            },
            "python": platform.python_version(),
        },
    }


def _cost_totals(fills: list[dict]) -> dict:
    return {
        **{
            key: math.fsum(fill[key] for fill in fills)
            for key in ("spread_cost", "slippage_cost", "price_adjustment_cost", "commission")
        },
        "funding_cashflow": 0.0,
        "funding_status": "unavailable",
        "fills": fills,
    }


def _reference_cashflow(fills: list[dict]) -> float:
    return math.fsum(
        (1 if fill["side"] == "sell" else -1) * fill["size"] * fill["reference_price"]
        for fill in fills
    )


def enrich_closed_trade(record: dict, fills: list[dict]) -> None:
    """Attribute existing broker PnL to matched references and actual fills."""
    if record.get("funding_adjustment", 0.0) != 0:
        raise ValueError("funding is unavailable; analyzer-only adjustments are unsupported")
    entry, exit_fill = fills  # v1 supports one complete entry and one complete exit.
    costs = _cost_totals(fills)
    costs["reference_pnl"] = _reference_cashflow(fills)
    sign = 1 if entry["side"] == "buy" else -1
    gross = sign * entry["size"] * (exit_fill["fill_price"] - entry["fill_price"])
    record.update(
        entry_price=entry["fill_price"],
        exit_price=exit_fill["fill_price"],
        gross_price_pnl=gross,
        commission=costs["commission"],
        net_pnl_before_funding=gross - costs["commission"],
        execution_costs=costs,
    )


def build_execution_audit(
    *, fills, trades, initial_capital, final_capital, position_size, position_price, last_close
):
    costs = _cost_totals(fills)
    closed_net = math.fsum(trade["realized_pnl"] for trade in trades)
    open_commission = math.fsum(
        fill["commission"] for fill in fills if fill["trade_id"] > len(trades)
    )
    open_pnl = position_size * (last_close - position_price) if position_size else 0.0
    reference_pnl = _reference_cashflow(fills) + position_size * last_close
    expected = (
        initial_capital + reference_pnl - costs["price_adjustment_cost"] - costs["commission"]
    )
    error = final_capital - expected
    trade_expected = initial_capital + closed_net + open_pnl - open_commission
    if not math.isclose(final_capital, expected, rel_tol=1e-12, abs_tol=1e-8) or not math.isclose(
        final_capital, trade_expected, rel_tol=1e-12, abs_tol=1e-8
    ):
        raise RuntimeError("execution costs do not reconcile with final broker value")
    costs.update(
        reference_pnl=reference_pnl,
        closed_net_pnl=closed_net,
        open_unrealized_pnl=open_pnl,
        open_commission=open_commission,
        reconciliation_error=error,
    )
    return costs
