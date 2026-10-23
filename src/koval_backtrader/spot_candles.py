# SPDX-License-Identifier: GPL-3.0-or-later
"""Causal historical observations; no Grid/DCA decision rules live here."""

from decimal import Decimal

import numpy as np
from koval.engine.history_window import DEFAULT_HISTORY_BARS
from koval.engine.timeframe_utils import timeframe_to_minutes

from koval_backtrader.spot_matching import (
    ZERO,
    eligible,
    exact,
    number,
    status_event,
    text_number,
)


class SpotCandleExecution:
    def _price(self, reference, phase):
        self._observe(
            "price",
            {"price": text_number(reference), "bar_index": self._state.bar_index, "phase": phase},
        )
        self._drain()

    def _point(self, reference, phase, at_open=False):
        state = self._state
        stop = state.controller.stop
        if (
            stop is not None
            and state.account.net_base
            and reference <= stop
            and state.controller.phase != "stopped_holding"
        ):
            self._price(reference, phase)
        working = sorted(
            self._state.orders.values(),
            key=lambda o: (
                0 if o.intent.purpose == "flatten" else 1 if o.intent.side == "sell" else 2,
                o.accepted_sequence,
                o.intent.client_order_id,
            ),
        )
        for prior in working:
            order = self._state.orders[prior.intent.client_order_id]
            if not eligible(self._state, order) or order.intent.side != "sell":
                continue
            if order.intent.order_type == "market":
                emergency = (
                    order.intent.purpose == "flatten"
                    and self._state.controller.terminal_reason == "stop"
                    and self._state.stop_reference_bar != self._state.bar_index
                )
                if not at_open and not emergency:
                    continue
            elif reference < order.intent.limit_price:
                continue
            self._match(order.intent.client_order_id, reference, at_open)
            if order.intent.purpose == "flatten" and not at_open:
                candidate = self._state.model_copy(deep=True)
                candidate.stop_reference_bar = candidate.bar_index
                self._persist(candidate, "stop_reference_consumed")
        self._price(reference, phase)
        for prior in working:
            if self._state.controller.terminal_action or self._state.reconciliation_required:
                break
            order = self._state.orders[prior.intent.client_order_id]
            if not eligible(self._state, order) or order.intent.side != "buy":
                continue
            if (
                order.intent.order_type == "market"
                and not at_open
                or order.intent.limit_price is not None
                and reference > order.intent.limit_price
            ):
                continue
            self._match(order.intent.client_order_id, reference, at_open)
            stop = self._state.controller.stop
            if stop is not None and self._state.account.net_base and reference <= stop:
                self._point(reference, phase)
                break

    def _leg(self, start, end, phase):
        cursor = start
        descending = end < start
        while cursor != end:
            crossings = [end]
            for order in self._state.orders.values():
                price = order.intent.limit_price
                if not eligible(self._state, order) or price is None:
                    continue
                if (
                    descending
                    and order.intent.side == "buy"
                    and end <= price < cursor
                    or not descending
                    and order.intent.side == "sell"
                    and cursor < price <= end
                ):
                    crossings.append(price)
            stop = self._state.controller.stop
            if (
                descending
                and stop is not None
                and self._state.account.net_base
                and end <= stop < cursor
            ):
                crossings.append(stop)
            cursor = max(crossings) if descending else min(crossings)
            self._point(cursor, phase)

    def _validate_candle(self, candle, *, previous=...):
        if self._failed:
            raise RuntimeError("Spot journal failed; restore a fresh historical session")
        if self._state.finished:
            raise ValueError("historical Spot session has reached end of data")
        if self._state.bar_in_progress:
            raise RuntimeError("incomplete candle requires fresh recovery")
        if len(candle) != 6:
            raise ValueError("Spot candle must have six OHLCV values")
        values = [number(value) for value in candle]
        timestamp, opening, high, low, close, volume = values
        interval = timeframe_to_minutes(self._state.binding.timeframe) * 60000
        if (
            timestamp < 0
            or timestamp != int(timestamp)
            or int(timestamp) % interval
            or min(opening, high, low, close) <= 0
            or volume < 0
            or not low <= min(opening, close) <= max(opening, close) <= high
        ):
            raise ValueError("invalid Spot OHLCV candle")
        if previous is ...:
            previous = self._state.last_candle
        if previous is not None:
            if timestamp == previous[0]:
                if values != previous:
                    raise ValueError("conflicting reconnect candle")
                return None
            if timestamp != previous[0] + interval:
                raise ValueError("Spot candle gap or out-of-order input")
        binding, rules = self._state.binding, self._state.instrument
        if binding.evaluation_end_ms is not None and timestamp >= binding.evaluation_end_ms:
            raise ValueError("candle outside evaluation interval")
        if (
            rules.status == "historical"
            and not rules.effective_from_ms <= timestamp <= rules.effective_to_ms
        ):
            raise ValueError("historical instrument evidence does not cover candle")
        return values

    @exact
    def on_candle(self, candle):
        values = self._validate_candle(candle)
        if values is None:
            return
        timestamp, opening, high, low, close, volume = values
        self._timestamp = int(timestamp)
        candidate = self._state.model_copy(deep=True)
        candidate.bar_index += 1
        candidate.bar_in_progress = True
        candidate.last_candle = values
        candidate.history = (candidate.history + [values])[-DEFAULT_HISTORY_BARS:]
        candidate.liquidity_allowance = volume * candidate.policy.maximum_volume_participation
        candidate.liquidity_used = ZERO
        levels = [
            o.intent.limit_price
            for o in candidate.orders.values()
            if eligible(candidate, o)
            and o.intent.limit_price is not None
            and low <= o.intent.limit_price <= high
        ]
        if len(levels) > 1:
            candidate.trace.append(
                {
                    "kind": "ambiguity",
                    "bar_index": candidate.bar_index,
                    "candidates": [text_number(p) for p in levels],
                    "policy": "open_low_high_close",
                    "selected": "chronological_crossings",
                }
            )
        self._persist(candidate, "candle_received")
        for order in list(self._state.orders.values()):
            if (
                eligible(self._state, order)
                and order.intent.expires_after_bars
                and self._state.bar_index
                >= order.intent.activation_bar + order.intent.expires_after_bars
            ):
                self._execution(status_event(self._state, order, "expired", self._timestamp))
        self._drain()
        self._point(opening, "open", True)
        self._leg(opening, low, "descending")
        self._leg(low, high, "ascending")
        self._leg(high, close, "close")
        self._price(close, "close")
        self._closed_bar(values)

    def _closed_bar(self, values):
        timestamp, opening, high, low, close, volume = values
        binding = self._state.binding
        interval = timeframe_to_minutes(binding.timeframe) * 60000
        strategy = self._strategy
        strategy.timestamp_ms, strategy.decision_timestamp_ms = (
            int(timestamp),
            int(timestamp) + interval,
        )
        strategy.bar_index = self._state.bar_index
        strategy.open, strategy.high, strategy.low, strategy.close, strategy.volume = map(
            float, values[1:]
        )
        history = np.array([[float(v) for v in row] for row in self._state.history])
        strategy.opens, strategy.highs, strategy.lows, strategy.closes, strategy.volumes = (
            history[:, i] for i in range(1, 6)
        )
        strategy.account_value = float(
            self._state.account.quote_cash + self._state.account.net_base * close
        )
        strategy.position_size = float(self._state.account.net_base)
        strategy.position_direction = "long" if strategy.position_size else None
        strategy.spot_runtime = self.report(mark=close)
        strategy.on_bar()
        request = strategy.get_spot_recipe_request()
        self._observe(
            "closed_bar",
            {
                "bar_index": self._state.bar_index,
                "close": text_number(close),
                "start": bool(
                    request
                    and request.start
                    and (
                        binding.evaluation_start_ms is None
                        or timestamp >= binding.evaluation_start_ms
                    )
                ),
                "stop_hold": bool(request and request.stop_hold),
                "stop_flatten": bool(request and request.stop_flatten),
                "decision_timestamp_ms": int(timestamp) + interval,
            },
        )
        self._drain()
        candidate = self._state.model_copy(deep=True)
        candidate.graph_state = strategy.export_graph_state()
        report = self.report(mark=close)
        if report["net_equity"] is not None:
            equity = Decimal(report["net_equity"])
            candidate.peak_equity = max(candidate.peak_equity, equity)
            candidate.maximum_drawdown = max(
                candidate.maximum_drawdown, (candidate.peak_equity - equity) / candidate.peak_equity
            )
            candidate.equity_curve.append(
                {
                    "timestamp_ms": int(timestamp) + interval,
                    "net_equity": text_number(equity),
                    "inventory": text_number(candidate.account.net_base),
                }
            )
        candidate.bar_in_progress = False
        self._persist(candidate, "candle_processed")
