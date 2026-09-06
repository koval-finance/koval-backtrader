# SPDX-License-Identifier: GPL-3.0-or-later
"""Apply synthetic price costs before Backtrader accounts for a real fill."""

from __future__ import annotations

from math import isfinite

import backtrader as bt

from koval_backtrader.time_conversion import num2utc_ms


class ExecutionCostBroker(bt.brokers.BackBroker):
    """Full-fill, linear cash broker; matching and OCO remain upstream-owned.

    Built-in slippage and fillers remain disabled. This broker is installed only
    by the runner for ohlcv_fixed_v1, with an already validated model.
    """

    params = (("execution_model", None),)

    def __init__(self):
        super().__init__()
        self.execution_fills: list[dict] = []
        self.trade_fills: dict[int, list[dict]] = {}
        self._execution_trade_id = 1

    def _adjust_price(self, order, reference):
        model = self.p.execution_model
        fraction = (model.spread_bps / 2 + model.slippage_bps) / 10_000
        sign = 1 if order.isbuy() else -1
        adjusted = reference * (1 + sign * fraction)
        # A take-profit is a market-on-touch order (venue TAKE_PROFIT_MARKET): it
        # pays the full adverse adjustment. Only entry limits keep the limit cap.
        if order.exectype == bt.Order.Limit and order.info.get("koval_role") != "take_profit":
            limit = order.created.price
            adjusted = min(adjusted, limit) if order.isbuy() else max(adjusted, limit)
        return adjusted

    def _execute(self, order, ago=None, price=None, cash=None, position=None, dtcoc=None):
        reference = float(order.created.price if price is None else price)
        size = float(order.executed.remsize)
        if (
            not isfinite(reference)
            or reference <= 0
            or not isfinite(size)
            or not isfinite(size * reference)
        ):
            raise ValueError(
                "execution price, size and notional must be finite, with positive price"
            )
        # Submission checks are hypothetical. They must not accrue costs or
        # expose fills; Backtrader still rechecks cash at the actual fill price.
        if ago is None or price is None:
            return super()._execute(order, ago, price, cash, position, dtcoc)
        adjusted = self._adjust_price(order, reference)
        if not isfinite(adjusted) or adjusted <= 0 or not isfinite(adjusted * size):
            raise ValueError("execution adjusted price and notional must be positive and finite")
        prior_size = order.executed.size
        prior_commission = order.executed.comm
        role = "exit" if self.positions[order.data].size else "entry"
        result = super()._execute(order, ago, adjusted, cash, position, dtcoc)
        executed_size = order.executed.size - prior_size
        if executed_size:
            self._record_fill(
                order,
                reference,
                adjusted,
                executed_size,
                order.executed.comm - prior_commission,
                role,
            )
        return result

    def _record_fill(self, order, reference, adjusted, signed_size, commission, role):
        model = self.p.execution_model
        cost = abs(signed_size) * abs(adjusted - reference)
        total_bps = model.spread_bps / 2 + model.slippage_bps
        spread_cost = cost * (model.spread_bps / 2 / total_bps) if total_bps else 0.0
        record = {
            "fill_id": len(self.execution_fills) + 1,
            "trade_id": self._execution_trade_id,
            "bar_index": len(order.data),
            "timestamp_ms": num2utc_ms(order.executed.dt),
            "role": role,
            "side": "buy" if order.isbuy() else "sell",
            "order_type": order.getordername().lower(),
            "koval_role": order.info.get("koval_role", "entry"),
            "size": abs(float(signed_size)),
            "reference_price": reference,
            "fill_price": adjusted,
            "spread_cost": spread_cost,
            "slippage_cost": cost - spread_cost,
            "price_adjustment_cost": cost,
            "commission": float(commission),
            "commission_policy": "uniform",
            "liquidity_role": "unavailable",
        }
        self.execution_fills.append(record)
        self.trade_fills.setdefault(self._execution_trade_id, []).append(record)
        if not self.positions[order.data].size:
            self._execution_trade_id += 1
