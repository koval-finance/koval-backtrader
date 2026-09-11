# SPDX-License-Identifier: GPL-3.0-or-later
"""Evidence for judging a strategy, aggregated from the persisted ledgers.

Win rate and profit factor over a dozen trades are noise, and a run with no
losing trades has no profit factor at all — reporting one as ``0.0`` or as
``inf`` invents a conclusion. Everything here either reconciles to the trade
and cashflow ledgers or reports itself as unavailable, by name.

Nothing in this module replaces the raw trades. An aggregate whose inputs are
gone cannot be checked, and a number nobody can check is a number nobody
should quote.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, datetime

# Below this, a win rate or a profit factor is describing the sample, not the
# strategy. Reported rather than enforced: it is the reader's judgement call.
RATIO_SAMPLE_FLOOR = 30

# MAE/MFE come from bar highs and lows. The true intrabar path is unknown, so
# the real excursion is at least this bad and possibly worse.
EXCURSION_RESOLUTION = "bar_high_low_not_intrabar_path"


def _finite(value: float | None) -> float | None:
    if value is None:
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _mean(values: Sequence[float]) -> float | None:
    return math.fsum(values) / len(values) if values else None


def time_under_water(equity: Sequence[float]) -> tuple[float, int]:
    """Share of points below the running peak, and the longest such stretch.

    Measured in equity-curve points, which are bars. It says how long the
    account spent below a level it had already reached — a cost that a single
    maximum-drawdown percentage hides entirely.
    """
    peak = -math.inf
    under = 0
    longest = 0
    current = 0
    for value in equity:
        point = float(value)
        if point >= peak:
            peak = point
            current = 0
        else:
            under += 1
            current += 1
            longest = max(longest, current)
    total = len(equity)
    return (100.0 * under / total if total else 0.0, longest)


def segment_inputs(entry_time_ms: int, market: dict | None) -> dict:
    """Per-trade keys an application can group by.

    Deliberately raw: this package reports which calendar bucket and which
    market a trade belongs to, and leaves regime labelling and walk-forward
    orchestration to the caller that owns those definitions.
    """
    moment = datetime.fromtimestamp(entry_time_ms / 1000, tz=UTC)
    iso = moment.isocalendar()
    return {
        "venue": None if market is None else market["exchange"],
        "market": None if market is None else market["market"],
        "symbol": None if market is None else market["canonical_symbol"],
        "year": moment.year,
        "month": moment.month,
        "iso_week": iso.week,
        "weekday": moment.weekday(),
        "hour_utc": moment.hour,
    }


def _expectancy(trades: list[dict]) -> dict:
    results = [float(trade.get("realized_pnl", 0.0)) for trade in trades]
    risks = [_finite(trade.get("risk_at_entry")) for trade in trades]
    usable = [
        result / risk
        for result, risk in zip(results, risks, strict=True)
        if risk is not None and risk > 0
    ]
    return {
        "per_trade": _mean(results),
        # An R multiple needs every trade's risk; a partial average would mix
        # two different denominators into one number.
        "per_trade_r": _mean(usable) if len(usable) == len(results) else None,
    }


def _excursion(trades: list[dict]) -> dict:
    mae = [_finite(trade.get("max_adverse_excursion")) for trade in trades]
    mfe = [_finite(trade.get("max_favorable_excursion")) for trade in trades]
    mae = [value for value in mae if value is not None]
    mfe = [value for value in mfe if value is not None]
    return {
        "avg_mae": _mean(mae),
        "avg_mfe": _mean(mfe),
        "worst_mae": max(mae) if mae else None,
        "best_mfe": max(mfe) if mfe else None,
        "resolution": EXCURSION_RESOLUTION,
    }


def _costs(trades: list[dict], execution_costs: dict | None) -> dict:
    commission = math.fsum(float(trade.get("commission", 0.0)) for trade in trades)
    # `gross_price_pnl` is genuinely gross. The legacy field named
    # `gross_realized_pnl` is not: it is already net of commission (see
    # docs/results.md), so it has to be grossed back up before it can be a
    # denominator for a cost ratio.
    gross = math.fsum(
        float(trade["gross_price_pnl"])
        if "gross_price_pnl" in trade
        else float(trade.get("gross_realized_pnl", 0.0)) + float(trade.get("commission", 0.0))
        for trade in trades
    )
    adjustment = (
        None if execution_costs is None else float(execution_costs["price_adjustment_cost"])
    )
    closed_adjustment = math.fsum(
        float(trade.get("execution_costs", {}).get("price_adjustment_cost", 0.0))
        for trade in trades
    )
    closed_cost = commission + closed_adjustment
    if execution_costs is not None:
        commission = float(execution_costs["commission"])
    liquidation = (
        0.0 if execution_costs is None else float(execution_costs.get("liquidation_fee", 0.0))
    )
    funding_status = (
        "unavailable"
        if execution_costs is None
        else execution_costs.get("funding_status", "unavailable")
    )
    funding = (
        None if funding_status == "unavailable" else float(execution_costs["funding_cashflow"])
    )
    total = commission + (adjustment or 0.0) + liquidation
    return {
        "commission": commission,
        "price_adjustment": adjustment,
        # Zero funding was charged because none is modelled. That is not the
        # same claim as a historical rate having been zero.
        "funding": funding,
        "funding_status": funding_status,
        "liquidation_fee": liquidation,
        "total_modelled_cost": total,
        "cost_over_gross_pct": (100.0 * closed_cost / abs(gross)) if gross else None,
    }


def build_research_metrics(
    *,
    trades: list[dict],
    equity_curve: Sequence[dict],
    total_bars: int,
    bars_in_position: int,
    max_drawdown_pct: float,
    execution_costs: dict | None,
    open_position: dict | None,
) -> dict:
    """Aggregate the persisted ledgers. Never a substitute for reading them."""
    closed = list(trades)
    wins = sum(1 for trade in closed if float(trade.get("realized_pnl", 0.0)) > 0)
    losses = sum(1 for trade in closed if float(trade.get("realized_pnl", 0.0)) < 0)
    equity = [float(point["equity"]) for point in equity_curve]
    under_water_pct, longest_under_water = time_under_water(equity)

    holding_bars = [
        value
        for value in (_finite(trade.get("holding_bars")) for trade in closed)
        if value is not None
    ]
    holding_seconds = [
        value
        for value in (_finite(trade.get("holding_seconds")) for trade in closed)
        if value is not None
    ]
    avg_bars = _mean(holding_bars)

    unavailable: dict[str, str] = {}
    if not closed:
        unavailable["expectancy"] = "no_closed_trades"
        unavailable["excursion"] = "no_closed_trades"
        unavailable["holding_time"] = "no_closed_trades"
    if execution_costs is None:
        unavailable["price_adjustment"] = "not_modelled_by_legacy_v1"

    expectancy = _expectancy(closed)
    if closed and expectancy["per_trade_r"] is None:
        unavailable["expectancy_r"] = "per_trade_risk_unavailable"

    return {
        "sample_size": {
            "closed_trades": len(closed),
            "wins": wins,
            "losses": losses,
            "sufficient_for_ratios": len(closed) >= RATIO_SAMPLE_FLOOR,
            "ratio_sample_floor": RATIO_SAMPLE_FLOOR,
        },
        "expectancy": expectancy,
        "exposure": {
            "total_bars": int(total_bars),
            "bars_in_position": int(bars_in_position),
            "exposure_pct": (100.0 * bars_in_position / total_bars) if total_bars else 0.0,
            "avg_holding_bars": avg_bars,
            # Elapsed time between the entry and exit fills, not bar count
            # times bar length: those differ by one bar and the difference
            # compounds on long holds.
            "avg_holding_seconds": _mean(holding_seconds),
        },
        "drawdown": {
            "max_drawdown_pct": float(max_drawdown_pct),
            "time_under_water_pct": under_water_pct,
            "max_time_under_water_bars": longest_under_water,
            "basis": "bar_close_equity_intrabar_pain_invisible",
        },
        "excursion": _excursion(closed),
        "costs": _costs(closed, execution_costs),
        "final_open_position": open_position,
        "unavailable": unavailable,
    }
