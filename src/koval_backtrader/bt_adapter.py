# SPDX-License-Identifier: GPL-3.0-or-later
# Koval Backtrader Adapter — strategy execution shim
# Strategies never import Backtrader; broker implementations live beside this bridge.
from __future__ import annotations

from dataclasses import asdict, replace
from math import isfinite

import backtrader as bt
import numpy as np
from koval.engine.engine_events import EngineEvent, EventType
from koval.engine.history_window import DEFAULT_HISTORY_BARS
from koval.engine.logger import get_logger
from koval.engine.protection import validate_protection_update
from koval.strategy.base.declarative import DeclarativeStrategy
from koval.strategy.base.trade_setup import TradeSetup
from koval.strategy.helpers.risk.position_sizer import calculate_position_size

from koval_backtrader.execution_account import ExecutionAccount, revalidated_risk
from koval_backtrader.execution_config import COSTED_VERSIONS
from koval_backtrader.execution_trace import ExecutionTrace
from koval_backtrader.market_identity import rejects_direction
from koval_backtrader.research_metrics import segment_inputs
from koval_backtrader.strategy_account import bind_strategy_account
from koval_backtrader.terminal_execution import finalize_runtime
from koval_backtrader.time_conversion import num2utc_ms, utc_ms

logger = get_logger(__name__)

# Maps internal exit-reason tokens to the human labels that appear on a closed
# trade. Consumers classify exits from these strings, so they are part of this
# package's output contract; anything unmapped falls back to "Manual".
_EXIT_REASON_LABELS: dict[str, str] = {
    "stop_loss": "Stop Loss",
    "take_profit": "Take Profit",
    "liquidation": "Liquidation",
    "end_of_data": "End of Data",
}

_DEFAULT_PARAMS: tuple = (
    ("risk_reward_ratio", 2.0),
    ("risk_per_trade", 1.0),
    ("leverage", 1.0),
    ("max_drawdown", None),
    ("history_bars", DEFAULT_HISTORY_BARS),
    ("strategy_config", {}),
    ("event_sink", None),
    ("execution_metadata", None),
    ("primary_timeframe_ms", None),
    ("htf_timeframe_ms", None),
    ("market_identity", None),
    ("runtime_boundaries", None),
    ("preparation_candles", None),
)


class BTStrategyAdapter(bt.Strategy):
    """
    Backtrader shim that delegates all business logic to a DeclarativeStrategy instance.

    Every key decision emits an EngineEvent — enabling full auditability and explainability.
    Collected in self._events; read by engine runner after cerebro.run().
    """

    params = _DEFAULT_PARAMS

    def __init__(self):
        super().__init__()
        self._strategy: DeclarativeStrategy = self._create_strategy_instance()
        self._strategy.config = dict(self.params.strategy_config or {})
        prepare = getattr(self._strategy, "prepare_backtest", None)
        if callable(prepare) and self.params.preparation_candles is not None:
            prepare(self.params.preparation_candles, history_bars=int(self.params.history_bars))

        self._entry_order: bt.Order | None = None
        self._stop_order: bt.Order | None = None
        self._tp_order: bt.Order | None = None
        self._pending_setup: TradeSetup | None = None
        self._trade_map: dict = {}
        self._trade_info: dict[int, dict] = {}
        self._pending_entry_size: float = 0.0
        self._pending_exit_price: float | None = None
        # Per-trade research inputs, gathered while the position is open.
        self._open_trade: dict | None = None
        self._bars_in_position: int = 0
        self._notified_fills = {}
        self._next_trade_id: int = 1
        boundaries = self.params.runtime_boundaries
        self._account = ExecutionAccount(
            self.broker.startingcash,
            commission_bps=self._commission_bps(),
            adjustment_fraction=self._adjustment_fraction(),
            ledger=getattr(self.broker, "account_ledger", None),
            funding_status=getattr(self.broker, "funding_status", "unavailable"),
            daily_baseline_equity=None if boundaries is None else boundaries.daily_baseline_equity,
            peak_equity=None if boundaries is None else boundaries.peak_equity,
        )
        if hasattr(self.broker, "bind_data"):
            self.broker.bind_data(self.data)
        bind_strategy_account(self._strategy, self._account)
        self._dd_limit_hit: bool = False
        self._entry_exec_bar: int = -1
        self._last_exit_reason: str = "unknown"
        # A cancel is only notified on the next bar, by which time _entry_order
        # has been cleared; keep the ref so the notification is still ours.
        self._cancelled_entry_ref: int | None = None

        self._events: list[EngineEvent] = []
        self._trace = ExecutionTrace()
        start_payload = {"strategy": type(self._strategy).__name__}
        if self.params.execution_metadata is not None:
            start_payload["execution_model"] = self.params.execution_metadata
        self._emit(EventType.SESSION_START, start_payload)

    def _create_strategy_instance(self) -> DeclarativeStrategy:
        raise NotImplementedError

    def _commission_bps(self) -> float:
        """The rate risk arithmetic should charge, or zero when none is modelled."""
        metadata = self.params.execution_metadata
        if metadata is None:
            return 0.0
        return float(metadata["resolved_config"]["execution_model"].get("commission_bps", 0.0))

    def _adjustment_fraction(self) -> float:
        """Half spread plus slippage as a fraction. Zero outside the fixed model."""
        metadata = self.params.execution_metadata
        if metadata is None or metadata["version"] not in COSTED_VERSIONS:
            return 0.0
        model = metadata["resolved_config"]["execution_model"]
        return (float(model["spread_bps"]) / 2 + float(model["slippage_bps"])) / 10_000

    def _timestamp_ms(self) -> int:
        timestamp = self.data.datetime.datetime(0)
        metadata = self.params.execution_metadata
        if metadata is not None and metadata["version"] in COSTED_VERSIONS:
            return utc_ms(timestamp)
        # Legacy keeps naive-datetime epoch conversion for reproduction only.
        return int(timestamp.timestamp() * 1000)

    def _safe_timestamp_ms(self) -> int:
        if not len(self.data):
            return 0
        try:
            return self._timestamp_ms()
        except Exception:
            return 0

    def _emit(self, event_type: EventType, payload: dict | None = None) -> None:
        bar = len(self.data) if self.data is not None else 0
        ts = self._safe_timestamp_ms()
        payload = dict(payload or {})
        payload["event_id"] = f"event-{len(self._events) + 1}"
        payload.setdefault("decision_id", self._trace.decision_id)
        if event_type == EventType.ORDER_REJECTED:
            self._trace.reject(payload)
        event = EngineEvent(
            event_type=event_type,
            bar_index=bar,
            timestamp_ms=ts,
            payload=payload,
        )
        self._events.append(event)
        sink = self.params.event_sink
        if sink is not None:
            sink(event)

    def _inject_state(self) -> None:
        s = self._strategy
        # Only [0] (current bar) — never positive indices (look-ahead)
        s.close = float(self.data.close[0])
        s.high = float(self.data.high[0])
        s.low = float(self.data.low[0])
        s.open = float(self.data.open[0])
        try:
            s.volume = float(self.data.volume[0])
        except Exception:
            s.volume = 0.0
        s.bar_index = len(self.data)
        s.timestamp_ms = self._safe_timestamp_ms()
        s.decision_timestamp_ms = (
            None
            if self.params.primary_timeframe_ms is None
            else s.timestamp_ms + self.params.primary_timeframe_ms
        )
        s.account_value = self.broker.getvalue()
        s.account = self._account.snapshot()
        s.position_size = abs(float(self.position.size)) if self.position else 0.0
        s.position_direction = (
            "long" if self.position.size > 0 else "short" if self.position.size < 0 else None
        )

        # Candle history arrays — chronological, [-1] == current bar.
        # data.line.get(ago=0, size=n) returns the last n bars in chronological order.
        n = min(int(self.params.history_bars), len(self.data))
        if n > 0:
            s.closes = np.array(self.data.close.get(ago=0, size=n), dtype=float)
            s.highs = np.array(self.data.high.get(ago=0, size=n), dtype=float)
            s.lows = np.array(self.data.low.get(ago=0, size=n), dtype=float)
            s.opens = np.array(self.data.open.get(ago=0, size=n), dtype=float)
            try:
                s.volumes = np.array(self.data.volume.get(ago=0, size=n), dtype=float)
            except (AttributeError, IndexError):
                s.volumes = np.zeros(n, dtype=float)

        # Higher-timeframe history — injected only when a 2nd feed is present,
        # and only for bars that CLOSED before this primary bar's decision. A
        # forming HTF bar reveals its own future, so it is never shown.
        if len(self.datas) > 1:
            s.htf_closes = s.htf_highs = s.htf_lows = s.htf_opens = s.htf_volumes = None
            htf = self.datas[1]
            available = len(htf)
            if self.params.primary_timeframe_ms is None or self.params.htf_timeframe_ms is None:
                raise ValueError(
                    "a higher-timeframe feed requires primary_timeframe_ms and "
                    "htf_timeframe_ms so only closed HTF bars are injected"
                )
            if available > 0:
                decision_ms = utc_ms(self.data.datetime.datetime(0)) + int(
                    self.params.primary_timeframe_ms
                )
                htf_ms = int(self.params.htf_timeframe_ms)
                # Only trailing bars can still be forming, so stop at the first
                # closed one. Rescanning the whole history on every primary bar
                # would make injection O(primary bars x HTF bars).
                skip = 0
                while skip < available and num2utc_ms(htf.datetime[-skip]) + htf_ms > decision_ms:
                    skip += 1
                m = min(int(self.params.history_bars), available - skip)
                if self.params.runtime_boundaries is not None:
                    # Paper aggregates only complete buckets inside its rolling
                    # primary window. Archived HTF rows outside it cannot become
                    # extra indicator history in the comparable runtime path.
                    window_start = num2utc_ms(self.data.datetime[-(n - 1)])
                    while m > 0 and num2utc_ms(htf.datetime[-(skip + m - 1)]) < window_start:
                        m -= 1
                # No closed HTF bar is the same "unavailable" state as no HTF
                # feed: leave the arrays None rather than inventing an empty one.
                if m > 0:

                    def _line(line, m=m, skip=skip):
                        return np.array(line.get(ago=-skip, size=m), dtype=float)

                    s.htf_closes = _line(htf.close)
                    s.htf_highs = _line(htf.high)
                    s.htf_lows = _line(htf.low)
                    s.htf_opens = _line(htf.open)
                    try:
                        s.htf_volumes = _line(htf.volume)
                    except (AttributeError, IndexError):
                        s.htf_volumes = np.zeros(m, dtype=float)

    def next(self) -> None:
        if not len(self.data):
            return
        if getattr(self, "_last_decision_bar", None) == len(self.data):
            return
        self._last_decision_bar = len(self.data)
        if not self.in_evaluation():
            return
        if self._dd_limit_hit:
            return
        # Same order as the engine's live runner: fills have already been
        # notified, the account absorbs this bar's equity, then the strategy
        # sees it. Reversing these two would show a stale drawdown.
        self._account.on_bar(equity=self.broker.getvalue(), timestamp_ms=self._safe_timestamp_ms())
        self._track_excursion()
        self._inject_state()
        self._trace.begin_decision(self)
        # Every bar, open position or not: the engine's live runner calls this
        # after syncing state, and a strategy that behaves differently in a
        # backtest than it does in paper is worse than no backtest.
        self._strategy.on_bar()
        if not self.position:
            if self._entry_order is not None and self._entry_order.alive():
                if self._strategy.should_cancel_entry():
                    self._cancelled_entry_ref = self._entry_order.ref
                    self.cancel(self._entry_order)
                    self._entry_order = None
                    return
                # entry already pending — don't place a second order
                return
            self._try_enter()
        else:
            self._update_exits()

    def prenext(self) -> None:
        # A later secondary feed must not suppress primary-clock decisions.
        self.next()

    def in_evaluation(self) -> bool:
        boundaries = self.params.runtime_boundaries
        return boundaries is None or self._safe_timestamp_ms() >= boundaries.evaluation_start_ms

    def _try_enter(self) -> None:
        if (
            self.params.runtime_boundaries is not None
            and len(self.datas) > 1
            and self._strategy.htf_closes is None
        ):
            self._trace.decisions[-1]["entry_gate"] = "higher_timeframe_unavailable"
            return
        direction: str | None = None
        if self._strategy.should_long():
            direction = "long"
        elif self._strategy.should_short():
            direction = "short"
        if direction is None:
            return

        if not self._strategy._execute_filters():
            self._emit(EventType.FILTER_REJECTED, {"direction": direction})
            return

        self._emit(EventType.FILTER_PASSED, {"direction": direction})
        setup = self._strategy.go_long() if direction == "long" else self._strategy.go_short()
        self._emit(
            EventType.SIGNAL_DETECTED,
            {
                "direction": setup.direction,
                "entry_price": setup.entry_price,
                "stop_loss": setup.stop_loss,
                "take_profit": setup.take_profit,
                "why_entry": list(setup.why_entry),
                "indicators_at_entry": dict(setup.indicators_at_entry),
            },
        )
        self._submit_entry(setup)

    def _submit_entry(self, setup: TradeSetup) -> None:
        self._trace.begin_intent(setup)
        # A spot product has no borrow, so a spot-labelled run must not open a
        # position the venue would refuse. Checked here, beside the
        # affordability rule, so the refusal reports the same fields any other
        # refusal does. It is an event, not a halt: a long/short strategy keeps
        # running its longs.
        unsupported = rejects_direction(self.params.market_identity, setup.direction)
        if unsupported is not None:
            self._emit(
                EventType.ORDER_REJECTED,
                {
                    "direction": setup.direction,
                    "entry_type": setup.entry_type,
                    "size": setup.size,
                    "price": setup.entry_price,
                    "reason": unsupported,
                },
            )
            return

        size = setup.size
        if size is None:
            size = calculate_position_size(
                account_value=self.broker.getvalue(),
                risk_per_trade_pct=self.params.risk_per_trade,
                entry_price=setup.entry_price,
                stop_loss=setup.stop_loss,
                direction=setup.direction,
                leverage=self.params.leverage,
            )
        validator = getattr(self.broker, "validate_entry", None)
        if validator is not None:
            if setup.take_profit is None:
                distance = abs(setup.entry_price - setup.stop_loss) * self.params.risk_reward_ratio
                setup = replace(
                    setup,
                    take_profit=setup.entry_price
                    + (distance if setup.direction == "long" else -distance),
                )
            validator(setup, size)
        if size <= 0:
            return

        metadata = self.params.execution_metadata
        if metadata is not None and metadata["version"] in COSTED_VERSIONS:
            model = metadata["resolved_config"]["execution_model"]
            reference = float(setup.entry_price)
            required = size * reference / float(model["leverage"]) + (
                size * reference * float(model["commission_bps"]) / 10_000
            )
            submission = getattr(self.broker, "submission_requirement", None)
            if submission is not None:
                required = submission(setup, size, self._safe_timestamp_ms())
            available = float(self.broker.getvalue()) - self._margin_in_use()
            # A non-finite order is a defect, not an affordability outcome: it
            # must fail loudly rather than be reported as a margin rejection.
            if not isfinite(size) or not isfinite(reference) or not isfinite(required):
                raise ValueError(
                    "execution price, size and notional must be finite, with positive price"
                )
            if required > available:
                self._emit(
                    EventType.ORDER_REJECTED,
                    {
                        "direction": setup.direction,
                        "entry_type": setup.entry_type,
                        "size": size,
                        "price": reference,
                        "reason": "insufficient_margin",
                        "required": required,
                        "available": available,
                    },
                )
                return

        self._pending_setup = setup
        is_long = setup.direction == "long"
        order_fn = self.buy if is_long else self.sell

        entry_options = {"_checksubmit": False} if validator is not None else {}
        if self.params.runtime_boundaries is not None:
            entry_options["koval_decision_timestamp_ms"] = self._strategy.decision_timestamp_ms
        if setup.entry_type == "market":
            self._entry_order = order_fn(size=size, koval_setup=setup, **entry_options)
        elif setup.entry_type == "limit":
            self._entry_order = order_fn(
                price=setup.entry_price,
                exectype=bt.Order.Limit,
                size=size,
                koval_setup=setup,
                **entry_options,
            )
        else:
            self._entry_order = order_fn(
                price=setup.entry_price,
                exectype=bt.Order.Stop,
                size=size,
                koval_setup=setup,
                **entry_options,
            )

        self._emit(
            EventType.ORDER_PLACED,
            {
                "order_id": self.broker.order_id(self._entry_order)
                if hasattr(self.broker, "order_id")
                else None,
                "intent_id": self._trace.intent["intent_id"],
                "direction": setup.direction,
                "entry_type": setup.entry_type,
                "size": size,
                "price": setup.entry_price,
            },
        )

    # Exit legs only ever close exposure, so the submit-time cash pseudo-execution
    # is meaningless for them and, for a short whose notional is near the cash
    # balance, wrongly margin-rejects the second OCO leg (both legs are pseudo-
    # executed against one running cash figure). Backtrader submits its own
    # bracket children with the same flag. Actual execution still refuses to
    # OPEN exposure without cash; a closing fill never needs cash.
    def _margin_in_use(self) -> float:
        # The adapter only enters when flat, so this is zero at the check today;
        # it keeps the rule correct if pyramiding is ever allowed.
        if not self.position:
            return 0.0
        model = self.params.execution_metadata["resolved_config"]["execution_model"]
        return (
            abs(float(self.position.size)) * float(self.position.price) / float(model["leverage"])
        )

    def _track_excursion(self) -> None:
        """Widen the open trade's high/low with this bar. Diagnostic only.

        Never injected into the strategy: it describes bars the strategy has
        already acted on, and feeding it back would be look-ahead.
        """
        open_trade = self._open_trade
        if open_trade is None:
            return
        open_trade["high"] = max(open_trade["high"], float(self.data.high[0]))
        open_trade["low"] = min(open_trade["low"], float(self.data.low[0]))

    def _entry_margin(self, fill_price: float, size: float) -> float:
        """Margin an actual fill ties up. Zero when no margin model applies."""
        metadata = self.params.execution_metadata
        if metadata is None or metadata["version"] not in COSTED_VERSIONS:
            return 0.0
        leverage = float(metadata["resolved_config"]["execution_model"]["leverage"])
        return abs(size) * abs(fill_price) / leverage

    def _place_bracket(
        self, exec_price: float, size: float, setup: TradeSetup, origin_order=None
    ) -> None:
        trace_options = (
            {}
            if origin_order is None
            else {
                "koval_decision_id": origin_order.info.get("koval_decision_id"),
                "koval_intent_id": origin_order.info.get("koval_intent_id"),
            }
        )
        sl = setup.stop_loss
        tp = setup.take_profit
        if tp is None:
            dist = abs(exec_price - sl)
            tp = (
                exec_price + dist * self.params.risk_reward_ratio
                if setup.direction == "long"
                else exec_price - dist * self.params.risk_reward_ratio
            )
        if setup.direction == "long":
            self._stop_order = self.sell(
                price=sl,
                exectype=bt.Order.Stop,
                size=size,
                _checksubmit=False,
                koval_role="stop_loss",
                **trace_options,
            )
            self._tp_order = self.sell(
                price=tp,
                exectype=bt.Order.Limit,
                size=size,
                oco=self._stop_order,
                _checksubmit=False,
                koval_role="take_profit",
                **trace_options,
            )
        else:
            self._stop_order = self.buy(
                price=sl,
                exectype=bt.Order.Stop,
                size=size,
                _checksubmit=False,
                koval_role="stop_loss",
                **trace_options,
            )
            self._tp_order = self.buy(
                price=tp,
                exectype=bt.Order.Limit,
                size=size,
                oco=self._stop_order,
                _checksubmit=False,
                koval_role="take_profit",
                **trace_options,
            )

    def _update_exits(self) -> None:
        if self._stop_order is None or not self._stop_order.alive():
            return
        # The entry-fill bar is included. Protection still cannot *fill* on it,
        # but the MIT paper runtime accepts a replacement there, and dropping
        # the strategy's instruction outright made a trailing stop behave
        # differently in a backtest than in paper. Pinned by
        # tests/test_paper_parity.py::dynamic_stop_requested_on_entry_fill_bar.
        if self._entry_exec_bar < 0:
            return
        size = abs(self.position.size)
        is_long = self.position.size > 0
        order_fn = self.sell if is_long else self.buy

        trade_id = self._next_trade_id
        new_sl = self._strategy.on_sl_update(trade_id)
        new_tp = self._strategy.on_tp_update(trade_id)

        current_sl = self._stop_order.price
        current_tp = self._tp_order.price if (self._tp_order and self._tp_order.alive()) else None

        sl_changed = new_sl is not None and new_sl != current_sl
        tp_changed = new_tp is not None and new_tp != current_tp
        if not sl_changed and not tp_changed:
            return

        # The stop is the OCO group leader, so cancelling EITHER leg cascades and
        # cancels its sibling (see oco_patch._patched_ococheck). Reading
        # `_tp_order.alive()` after the cancel therefore always returns False and
        # would silently drop the take-profit. Rebuild the whole bracket
        # atomically instead: snapshot both target prices first, cancel once,
        # then re-place both legs as a fresh OCO pair.
        target_sl = new_sl if sl_changed else current_sl
        target_tp = new_tp if tp_changed else current_tp

        normalize = getattr(self.broker, "normalize_protection", None)
        if normalize is not None:
            target_sl, target_tp = normalize(self, target_sl, target_tp)
        validate_protection_update(
            side="buy" if is_long else "sell",
            current_stop=current_sl,
            stop_price=target_sl,
            target_price=target_tp,
        )
        self._account.on_stop_moved(target_sl)
        updated = getattr(self.broker, "protection_updated", None)
        if updated is not None:
            updated(self, target_sl, target_tp)
        self.cancel(self._stop_order)
        self._stop_order = order_fn(
            price=target_sl,
            exectype=bt.Order.Stop,
            size=size,
            _checksubmit=False,
            koval_role="stop_loss",
        )
        if target_tp is not None:
            self._tp_order = order_fn(
                price=target_tp,
                exectype=bt.Order.Limit,
                size=size,
                oco=self._stop_order,
                _checksubmit=False,
                koval_role="take_profit",
            )
        else:
            self._tp_order = None

    def notify_order(self, order: bt.Order) -> None:
        self._trace.record_order(self, order)
        if order.status in (order.Submitted, order.Accepted):
            return

        if order.status in (order.Completed, order.Partial):
            prior_size, prior_value, prior_fee, prior_pnl = self._notified_fills.get(
                order.ref, (0.0, 0.0, 0.0, 0.0)
            )
            cumulative_size = abs(float(order.executed.size))
            cumulative_value = cumulative_size * float(order.executed.price)
            delta_size = cumulative_size - prior_size
            delta_fee = float(order.executed.comm) - prior_fee
            delta_pnl = float(order.executed.pnl) - prior_pnl
            self._notified_fills[order.ref] = (
                cumulative_size,
                cumulative_value,
                float(order.executed.comm),
                float(order.executed.pnl),
            )
            if order == self._entry_order or order.info.get("koval_setup") is not None:
                if order.status == order.Completed:
                    self._entry_order = None
                size = cumulative_size
                setup = self._pending_setup or order.info.get("koval_setup")
                if setup is None or delta_size <= 0:
                    return
                self._pending_entry_size = size
                if not order.info.get("koval_protection_placed", False):
                    self._place_bracket(order.executed.price, size, setup, origin_order=order)
                self._entry_exec_bar = len(self.data)
                fill_price = float(order.executed.price)
                commission = float(order.executed.comm)
                risk = revalidated_risk(
                    direction=setup.direction,
                    requested_price=float(setup.entry_price),
                    requested_size=float(setup.size if setup.size is not None else size),
                    fill_price=fill_price,
                    filled_size=size,
                    stop_price=float(setup.stop_loss),
                    commission_bps=self._commission_bps(),
                    adjustment_fraction=self._adjustment_fraction(),
                )
                if prior_size:
                    self._open_trade.update(
                        entry_price=fill_price, size=size, risk_at_entry=risk["actual_risk"]
                    )
                    for trade in self._trade_map.values():
                        trade["size"] = size
                else:
                    self._open_trade = {
                        "direction": setup.direction,
                        "entry_price": fill_price,
                        "size": size,
                        "bar": len(self.data),
                        "timestamp_ms": self._safe_timestamp_ms(),
                        "risk_at_entry": risk["actual_risk"],
                        # The fill bar counts: the position was held for the rest
                        # of it, and its range is part of the excursion.
                        "high": float(self.data.high[0]),
                        "low": float(self.data.low[0]),
                    }
                if prior_size:
                    delta_price = (cumulative_value - prior_value) / delta_size
                    self._account.on_entry_fill(
                        fill_price=delta_price,
                        quantity=delta_size,
                        commission=delta_fee,
                        margin=self._entry_margin(delta_price, delta_size),
                    )
                else:
                    self._pending_setup = setup
                    self._account.on_open(
                        direction=setup.direction,
                        fill_price=fill_price,
                        quantity=size,
                        stop_price=float(setup.stop_loss),
                        commission=commission,
                        margin=self._entry_margin(fill_price, size),
                    )
                # The gap and the modelled costs are only knowable now. A risk
                # gate that never revalidates here is gating on a price the
                # account did not pay.
                self._emit(
                    EventType.ORDER_FILLED,
                    {
                        "order_id": self.broker.order_id(order)
                        if hasattr(self.broker, "order_id")
                        else None,
                        "fill_ids": [
                            fill["fill_id"]
                            for fill in getattr(self.broker, "order_fills", {}).get(order.ref, ())
                        ],
                        "decision_id": order.info.get("koval_decision_id"),
                        "direction": setup.direction,
                        "fill_price": order.executed.price,
                        "size": size,
                        "commission": delta_fee,
                        "fill_quantity": delta_size,
                        "cumulative_quantity": size,
                        "status": "partial" if order.status == order.Partial else "filled",
                        **risk,
                    },
                )
                return

            is_stop = self._stop_order is not None and order.ref == self._stop_order.ref
            is_tp = self._tp_order is not None and order.ref == self._tp_order.ref

            if order.info.get("koval_role") in {"liquidation", "end_of_data"}:
                self._pending_exit_price = float(order.executed.price)
                self._last_exit_reason = order.info["koval_role"]
                self._stop_order = self._tp_order = None
            if is_stop or is_tp:
                self._pending_exit_price = float(order.executed.price)

            if order.status == order.Partial:
                self._account.on_partial_close(
                    quantity=delta_size, realized_pnl=delta_pnl, commission=delta_fee
                )
                self._last_exit_reason = "stop_loss" if is_stop else "take_profit"
                return

            if is_stop:
                if self._tp_order:
                    self.cancel(self._tp_order)
                    self._tp_order = None
                self._stop_order = None
                self._last_exit_reason = "stop_loss"

            elif is_tp:
                if self._stop_order:
                    self.cancel(self._stop_order)
                    self._stop_order = None
                self._tp_order = None
                self._last_exit_reason = "take_profit"

        elif order.status in (order.Canceled, order.Margin, order.Rejected):
            if order == self._entry_order or order.ref == self._cancelled_entry_ref:
                self._entry_order = None
                self._cancelled_entry_ref = None
                setup = self._pending_setup
                if not order.executed.size:
                    self._pending_setup = None
                reason = {
                    order.Margin: "insufficient_margin",
                    order.Canceled: "canceled",
                    order.Rejected: "rejected",
                }[order.status]
                reason = order.info.get("koval_rejection", reason)
                self._emit(
                    EventType.ORDER_REJECTED,
                    {
                        "direction": getattr(setup, "direction", None),
                        "entry_type": getattr(setup, "entry_type", None),
                        "size": abs(float(order.created.size)),
                        "price": float(order.created.price or 0.0),
                        "reason": reason,
                        "order_id": self.broker.order_id(order)
                        if hasattr(self.broker, "order_id")
                        else None,
                    },
                )

    def _close_research_record(self) -> dict:
        """Excursion, holding time, risk and segment keys for the closing trade.

        MAE/MFE are measured from bar highs and lows over the holding period,
        including the fill bar and the exit bar. The true intrabar path is
        unknown; these diagnostics may include extrema before entry or after exit.
        """
        open_trade = self._open_trade
        self._open_trade = None
        if open_trade is None:
            return {}
        high = max(open_trade["high"], float(self.data.high[0]))
        low = min(open_trade["low"], float(self.data.low[0]))
        entry = open_trade["entry_price"]
        size = open_trade["size"]
        if open_trade["direction"] == "long":
            favorable, adverse = high - entry, entry - low
        else:
            favorable, adverse = entry - low, high - entry
        bars = len(self.data) - open_trade["bar"] + 1
        self._bars_in_position += bars
        market = self.params.market_identity
        return {
            "max_favorable_excursion": max(0.0, favorable) * size,
            "max_adverse_excursion": max(0.0, adverse) * size,
            "holding_bars": bars,
            "holding_seconds": max(
                0.0, (self._safe_timestamp_ms() - open_trade["timestamp_ms"]) / 1000.0
            ),
            "risk_at_entry": open_trade["risk_at_entry"],
            "segment": segment_inputs(
                open_trade["timestamp_ms"], None if market is None else market.as_dict()
            ),
        }

    def _open_trade_bars(self) -> int:
        """Bars held by a position still open at the end of the data, if any."""
        if self._open_trade is None:
            return 0
        return len(self.data) - self._open_trade["bar"] + 1

    def exposure_bars(self) -> int:
        """Bars on which a position was held, closed trades and any open one."""
        return self._bars_in_position + self._open_trade_bars()

    def notify_trade(self, trade: bt.Trade) -> None:
        if trade.justopened:
            if self._pending_setup:
                self._trade_map[trade.ref] = {
                    "setup": self._pending_setup,
                    "size": self._pending_entry_size,
                }
                self._strategy.on_open_position(
                    self._next_trade_id,
                    replace(
                        self._pending_setup,
                        entry_price=float(trade.price),
                        size=self._pending_entry_size,
                    ),
                )
                self._emit(
                    EventType.TRADE_OPENED,
                    {
                        "trade_id": self._next_trade_id,
                        "direction": self._pending_setup.direction,
                        "entry_price": float(trade.price),
                        "stop_loss": self._pending_setup.stop_loss,
                        "take_profit": self._pending_setup.take_profit,
                        "why_entry": list(self._pending_setup.why_entry),
                    },
                )
                self._pending_setup = None

        elif trade.isclosed:
            stored = self._trade_map.get(trade.ref, {})
            setup = stored.get("setup")
            # Snapshot both before the reset below: the closed-trade event is
            # emitted afterwards and has to report what actually happened, not
            # the cleared state.
            exit_reason = self._last_exit_reason
            exit_price = self._pending_exit_price
            fills = getattr(self.broker, "trade_fills", {}).get(self._next_trade_id, [])
            liquidation_fee = sum(fill.get("liquidation_fee", 0.0) for fill in fills)
            exit_fills = [fill for fill in fills if fill["role"] == "exit"]
            if exit_fills:
                exit_price = sum(fill["size"] * fill["fill_price"] for fill in exit_fills) / sum(
                    fill["size"] for fill in exit_fills
                )
            result = {
                "pnl": trade.pnl,
                "pnl_comm": trade.pnlcomm - liquidation_fee,
                "exit_reason": exit_reason,
                "setup": setup,
            }
            research = self._close_research_record()
            self._trade_info[trade.ref] = {
                **research,
                "size": stored.get("size", self._pending_entry_size),
                "exit_reason": _EXIT_REASON_LABELS.get(exit_reason, "Manual"),
                "exit_price": exit_price,
                "stop_loss": getattr(setup, "stop_loss", 0.0) if setup else 0.0,
                "take_profit": getattr(setup, "take_profit", 0.0) if setup else 0.0,
                "sl_calculation": getattr(setup, "sl_calc_expr", "") if setup else "",
                "tp_calculation": getattr(setup, "tp_calc_expr", "") if setup else "",
                "why_entry": list(getattr(setup, "why_entry", []) or []) if setup else [],
                "indicators_at_entry": dict(getattr(setup, "indicators_at_entry", {}) or {})
                if setup
                else {},
            }
            # Reset per-trade exit state so the next trade can't inherit a stale
            # reason/price if it ever closes via a non-bracket path.
            self._pending_exit_price = None
            self._last_exit_reason = "unknown"
            # Costed runs already booked gross PnL and every fill fee in the
            # shared broker ledger. Legacy local accounts book them separately;
            # the previously paid entry fee must not be charged again.
            entry_commission = self._account.entry_commission
            total_commission = float(trade.pnl) - float(trade.pnlcomm)
            self._account.on_close(
                realized_pnl=float(trade.pnl),
                commission=total_commission - entry_commission,
            )
            self._strategy.on_close_position(self._next_trade_id, result)
            self._emit(
                EventType.TRADE_CLOSED,
                {
                    "trade_id": self._next_trade_id,
                    "pnl": trade.pnl,
                    "fill_ids": [fill["fill_id"] for fill in fills],
                    "order_ids": list(dict.fromkeys(fill["order_id"] for fill in fills)),
                    "pnl_comm": trade.pnlcomm - liquidation_fee,
                    "liquidation_fee": liquidation_fee,
                    "exit_reason": exit_reason,
                    "entry_price": float(trade.price),
                    # Every close reaching here came through a bracket leg, and
                    # a leg knows its fill price. The fallback is insurance for
                    # a future non-bracket exit path, not a case that fires.
                    "exit_price": float(
                        exit_price if exit_price is not None else self.data.close[0]
                    ),
                },
            )
            self._next_trade_id += 1
            self._check_drawdown()

    def get_trade_info(self, trade_ref: int) -> dict:
        """Per-trade enrichment consumed by ``TradeListAnalyzer``.

        Backtrader reports ``trade.size == 0`` on a closed trade, so the
        analyzer cannot recover the filled size or exit reason on its own. We
        captured both during order/trade notifications; expose them here so the
        persisted ``TradeRecord`` carries a real size, exit price (derived from
        gross PnL ÷ size) and a tp/sl reason.
        """
        return self._trade_info.get(trade_ref, {})

    def buy(self, *args, **kwargs):
        order = super().buy(*args, **kwargs)
        self._trace.record_order(self, order)
        return order

    def sell(self, *args, **kwargs):
        order = super().sell(*args, **kwargs)
        self._trace.record_order(self, order)
        return order

    def _check_drawdown(self) -> None:
        max_dd = self.params.max_drawdown
        if not max_dd or max_dd <= 0:
            return
        # The peak comes from the same account state the paper runtime reports,
        # rather than a second counter that could drift from it. The comparison
        # still uses the live broker value: this runs during close notification,
        # before the account has absorbed this bar's equity, and reading the
        # account's equity here would delay every trip by one bar.
        peak = self._account.peak_equity
        if peak <= 0:
            return
        dd_pct = 100.0 * (peak - self.broker.getvalue()) / peak
        if dd_pct > max_dd and not self._dd_limit_hit:
            self._dd_limit_hit = True
            self._emit(EventType.DRAWDOWN_LIMIT_HIT, {"drawdown_pct": dd_pct, "limit": max_dd})
            try:
                self.cerebro.runstop()
            except Exception:
                pass

    def stop(self) -> None:
        finalize_runtime(self)
        self._emit(
            EventType.SESSION_END,
            {
                "total_trades": self._next_trade_id - 1,
                "final_value": self.broker.getvalue(),
                "account": asdict(self._account.snapshot()),
                "account_ledger": self._account.ledger_entries(),
            },
        )


def make_bt_strategy_class(
    declarative_cls: type[DeclarativeStrategy],
    **default_params,
) -> type[BTStrategyAdapter]:
    """
    Factory: wrap any DeclarativeStrategy into a BTStrategyAdapter subclass.

    The engine is strategy-agnostic — it calls this with any DeclarativeStrategy
    and gets back a BT-compatible class. The engine never knows the concrete strategy.
    """
    default_param_keys = {k for k, _ in _DEFAULT_PARAMS}
    base_params = tuple(
        (k, default_params[k]) if k in default_params else (k, v) for k, v in _DEFAULT_PARAMS
    )
    extra_params = tuple((k, v) for k, v in default_params.items() if k not in default_param_keys)
    final_params = base_params + extra_params

    class _Adapter(BTStrategyAdapter):
        params = final_params

        def _create_strategy_instance(self) -> DeclarativeStrategy:
            return declarative_cls()

    _Adapter.__name__ = f"{declarative_cls.__name__}Adapter"
    _Adapter.__qualname__ = f"{declarative_cls.__name__}Adapter"
    return _Adapter
