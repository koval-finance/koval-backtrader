# SPDX-License-Identifier: GPL-3.0-or-later
"""Plain execution disclosure and reconciliation. Never debit costs here."""

from __future__ import annotations

import math
import platform
from hashlib import sha256
from importlib.metadata import version
from importlib.resources import files

from koval.engine.history_window import DEFAULT_HISTORY_BARS
from koval.engine.paper_profile import EQUAL_TIMESTAMP_ORDER

from koval_backtrader.execution_config import COSTED_VERSIONS, REALISTIC_VERSION, ExecutionModel


def _implementation_digest() -> str:
    """Identify editable/unreleased code as well as published package versions."""
    digest = sha256()
    for source in sorted(files("koval_backtrader").iterdir(), key=lambda item: item.name):
        if source.is_file() and source.name.endswith(".py"):
            digest.update(source.name.encode() + b"\0" + source.read_bytes() + b"\0")
    return digest.hexdigest()


def execution_metadata(model: ExecutionModel) -> dict:
    fixed = model.version in COSTED_VERSIONS
    metadata = {
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
            # What the declared market prohibits. Without a market block the
            # label is provenance only, exactly as it was in 0.10.0.
            "market_constraints": model.market_constraints,
            "commission_currency": "quote",
            "maker_taker_classification": "unavailable",
            "funding": "unavailable",
            "liquidity": "unlimited_full_fills",
            "additional_latency_bars": 0,
            "market_timing": "next_bar_open",
            "protection_timing": "same_entry_bar"
            if model.version == REALISTIC_VERSION
            else "bar_after_entry_fill",
            "equal_timestamp_order": list(EQUAL_TIMESTAMP_ORDER)
            if model.version == REALISTIC_VERSION
            else None,
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

    if model.version == REALISTIC_VERSION:
        evidence = model.execution_evidence
        assumptions = metadata["assumptions"]
        assumptions.update(
            commission_policy="evidence_schedule"
            if evidence.fee_schedule
            else "uniform_approximation",
            maker_taker_classification="resting_entry_limit_assumed_maker_other_fills_taker",
            intrabar_ordering="conservative_stop_first",
            funding="archived_settlement_cashflows" if evidence.funding else "unavailable",
            liquidity="shared_primary_bar_volume_budget"
            if evidence.execution_proxy
            else "unlimited_full_fills",
            instrument_constraints="time_valid_evidence"
            if evidence.instrument_specs
            else "unavailable",
            liquidation="mark_price_cross_margin_single_position"
            if evidence.mark_prices
            else "unavailable",
        )
        metadata["evidence_manifest"] = evidence.manifest()
        effects = metadata["unmodelled_effects"]
        modelled = []
        if evidence.funding:
            modelled += ["funding"]
        if evidence.fee_schedule:
            modelled += ["maker_taker_fees"]
        if evidence.instrument_specs:
            modelled += ["exchange_filters"]
        if evidence.mark_prices:
            modelled += ["maintenance_margin", "liquidation"]
        if evidence.execution_proxy:
            modelled += ["volume_participation", "partial_fills", "additional_latency"]
            assumptions["latency"] = model.as_config()["execution_proxy"]["latency"]
            if evidence.execution_proxy.calibration:
                modelled += ["volatility_slippage", "size_dependent_impact"]
        metadata["unmodelled_effects"] = [effect for effect in effects if effect not in modelled]
        metadata["unmodelled_effects"] += [
            "cancellation_latency",
            "replacement_latency",
            "portfolio_margin",
        ]
    return metadata


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
    entries = [fill for fill in fills if fill["role"] == "entry"]
    exits = [fill for fill in fills if fill["role"] == "exit"]
    quantity = math.fsum(fill["size"] for fill in entries)
    entry_price = math.fsum(fill["size"] * fill["fill_price"] for fill in entries) / quantity
    exit_price = math.fsum(fill["size"] * fill["fill_price"] for fill in exits) / quantity
    costs = _cost_totals(fills)
    costs["reference_pnl"] = _reference_cashflow(fills)
    sign = 1 if entries[0]["side"] == "buy" else -1
    gross = sign * quantity * (exit_price - entry_price)
    record.update(
        fill_ids=[fill["fill_id"] for fill in fills],
        order_ids=list(dict.fromkeys(fill["order_id"] for fill in fills)),
        decision_id=entries[0]["decision_id"],
        entry_price=entry_price,
        exit_price=exit_price,
        size=quantity,
        gross_price_pnl=gross,
        commission=costs["commission"],
        net_pnl_before_funding=gross - costs["commission"],
        execution_costs=costs,
    )
    liquidation_fee = math.fsum(fill.get("liquidation_fee", 0.0) for fill in fills)
    if liquidation_fee:
        record["liquidation_fee"] = liquidation_fee
        record["realized_pnl"] -= liquidation_fee


def build_execution_audit(
    *,
    fills,
    trades,
    initial_capital,
    final_capital,
    position_size,
    position_price,
    last_close,
    funding_entries=(),
    funding_status="unavailable",
    liquidation_fee=0.0,
):
    costs = _cost_totals(fills)
    funding = math.fsum(entry["amount"] for entry in funding_entries)
    costs.update(
        funding_cashflow=funding,
        funding_status=funding_status,
        funding_entries=list(funding_entries),
        liquidation_fee=liquidation_fee,
    )
    closed_net = math.fsum(trade["realized_pnl"] for trade in trades)
    open_commission = math.fsum(
        fill["commission"] for fill in fills if fill["trade_id"] > len(trades)
    )
    open_pnl = position_size * (last_close - position_price) if position_size else 0.0
    open_realized = (
        math.fsum(
            (1 if fill["side"] == "sell" else -1)
            * fill["size"]
            * (fill["fill_price"] - position_price)
            for fill in fills
            if fill["trade_id"] > len(trades) and fill["role"] == "exit"
        )
        if position_size
        else 0.0
    )
    reference_pnl = _reference_cashflow(fills) + position_size * last_close
    expected = (
        initial_capital
        + reference_pnl
        - costs["price_adjustment_cost"]
        - costs["commission"]
        + funding
        - liquidation_fee
    )
    error = final_capital - expected
    trade_expected = (
        initial_capital + closed_net + open_realized + open_pnl - open_commission + funding
    )
    if not math.isclose(final_capital, expected, rel_tol=1e-12, abs_tol=1e-8) or not math.isclose(
        final_capital, trade_expected, rel_tol=1e-12, abs_tol=1e-8
    ):
        raise RuntimeError("execution costs do not reconcile with final broker value")
    costs.update(
        reference_pnl=reference_pnl,
        closed_net_pnl=closed_net,
        open_unrealized_pnl=open_pnl,
        open_realized_pnl=open_realized,
        open_commission=open_commission,
        reconciliation_error=error,
    )
    return costs
