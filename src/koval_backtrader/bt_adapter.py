# SPDX-License-Identifier: GPL-3.0-or-later
# Koval Backtrader Adapter — strategy execution shim
# All backtrader-specific code lives here. Strategies never import backtrader.
from __future__ import annotations

import backtrader as bt
import numpy as np
from koval.engine.engine_events import EngineEvent, EventType
from koval.engine.logger import get_logger
from koval.strategy.base.declarative import DeclarativeStrategy
from koval.strategy.base.trade_setup import TradeSetup
from koval.strategy.helpers.risk.position_sizer import calculate_position_size

logger = get_logger(__name__)

# Maps internal exit-reason tokens to the human labels that appear on a closed
# trade. Consumers classify exits from these strings, so they are part of this
# package's output contract; anything unmapped falls back to "Manual".
_EXIT_REASON_LABELS: dict[str, str] = {
    "stop_loss": "Stop Loss",
    "take_profit": "Take Profit",
}

_DEFAULT_PARAMS: tuple = (
    ("risk_reward_ratio", 2.0),
    ("risk_per_trade", 1.0),
    ("leverage", 1.0),
    ("max_drawdown", None),
    ("history_bars", 300),
    ("strategy_config", {}),
    ("event_sink", None),
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

        self._entry_order: bt.Order | None = None
        self._stop_order: bt.Order | None = None
        self._tp_order: bt.Order | None = None
        self._pending_setup: TradeSetup | None = None
        self._trade_map: dict = {}
        self._trade_info: dict[int, dict] = {}
        self._pending_entry_size: float = 0.0
        self._pending_exit_price: float | None = None
        self._next_trade_id: int = 1
        self._equity_peak: float = self.broker.startingcash
        self._dd_limit_hit: bool = False
        self._entry_exec_bar: int = -1
        self._last_exit_reason: str = "unknown"

        self._events: list[EngineEvent] = []
        self._emit(EventType.SESSION_START, {"strategy": type(self._strategy).__name__})

    def _create_strategy_instance(self) -> DeclarativeStrategy:
        raise NotImplementedError

    def _emit(self, event_type: EventType, payload: dict | None = None) -> None:
        bar = len(self.data) if self.data is not None else 0
        try:
            ts = int(self.data.datetime.datetime(0).timestamp() * 1000)
        except Exception:
            ts = 0
        event = EngineEvent(
            event_type=event_type,
            bar_index=bar,
            timestamp_ms=ts,
            payload=payload or {},
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
        try:
            s.timestamp_ms = int(self.data.datetime.datetime(0).timestamp() * 1000)
        except Exception:
            s.timestamp_ms = 0
        s.account_value = self.broker.getvalue()
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

        # Higher-timeframe history — injected only when a 2nd feed is present.
        if len(self.datas) > 1:
            htf = self.datas[1]
            m = min(int(self.params.history_bars), len(htf))
            if m > 0:
                s.htf_closes = np.array(htf.close.get(ago=0, size=m), dtype=float)
                s.htf_highs = np.array(htf.high.get(ago=0, size=m), dtype=float)
                s.htf_lows = np.array(htf.low.get(ago=0, size=m), dtype=float)
                s.htf_opens = np.array(htf.open.get(ago=0, size=m), dtype=float)
                try:
                    s.htf_volumes = np.array(htf.volume.get(ago=0, size=m), dtype=float)
                except (AttributeError, IndexError):
                    s.htf_volumes = np.zeros(m, dtype=float)

    def next(self) -> None:
        if self._dd_limit_hit:
            return
        self._inject_state()
        # Every bar, open position or not: the engine's live runner calls this
        # after syncing state, and a strategy that behaves differently in a
        # backtest than it does in paper is worse than no backtest.
        self._strategy.on_bar()
        self._equity_peak = max(self._equity_peak, self.broker.getvalue())
        if not self.position:
            if self._entry_order is not None and self._entry_order.alive():
                if self._strategy.should_cancel_entry():
                    self.cancel(self._entry_order)
                    self._entry_order = None
                    return
                # entry already pending — don't place a second order
                return
            self._try_enter()
        else:
            self._update_exits()

    def _try_enter(self) -> None:
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
        if size <= 0:
            return

        self._pending_setup = setup
        is_long = setup.direction == "long"
        order_fn = self.buy if is_long else self.sell

        if setup.entry_type == "market":
            self._entry_order = order_fn(size=size)
        elif setup.entry_type == "limit":
            self._entry_order = order_fn(
                price=setup.entry_price, exectype=bt.Order.Limit, size=size
            )
        else:
            self._entry_order = order_fn(price=setup.entry_price, exectype=bt.Order.Stop, size=size)

        self._emit(
            EventType.ORDER_PLACED,
            {
                "direction": setup.direction,
                "entry_type": setup.entry_type,
                "size": size,
                "price": setup.entry_price,
            },
        )

    def _place_bracket(self, exec_price: float, size: float, setup: TradeSetup) -> None:
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
            self._stop_order = self.sell(price=sl, exectype=bt.Order.Stop, size=size)
            self._tp_order = self.sell(
                price=tp, exectype=bt.Order.Limit, size=size, oco=self._stop_order
            )
        else:
            self._stop_order = self.buy(price=sl, exectype=bt.Order.Stop, size=size)
            self._tp_order = self.buy(
                price=tp, exectype=bt.Order.Limit, size=size, oco=self._stop_order
            )

    def _update_exits(self) -> None:
        if self._stop_order is None or not self._stop_order.alive():
            return
        if self._entry_exec_bar < 0 or len(self.data) == self._entry_exec_bar:
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

        self.cancel(self._stop_order)
        self._stop_order = order_fn(price=target_sl, exectype=bt.Order.Stop, size=size)
        if target_tp is not None:
            self._tp_order = order_fn(
                price=target_tp, exectype=bt.Order.Limit, size=size, oco=self._stop_order
            )
        else:
            self._tp_order = None

    def notify_order(self, order: bt.Order) -> None:
        if order.status in (order.Submitted, order.Accepted):
            return

        if order.status == order.Completed:
            if order == self._entry_order:
                self._entry_order = None
                size = abs(order.executed.size)
                if self._pending_setup is None:
                    return
                self._pending_entry_size = size
                self._place_bracket(order.executed.price, size, self._pending_setup)
                self._entry_exec_bar = len(self.data)
                self._emit(
                    EventType.ORDER_FILLED,
                    {
                        "direction": self._pending_setup.direction,
                        "fill_price": order.executed.price,
                        "size": size,
                    },
                )
                return

            is_stop = self._stop_order is not None and order.ref == self._stop_order.ref
            is_tp = self._tp_order is not None and order.ref == self._tp_order.ref

            if is_stop or is_tp:
                self._pending_exit_price = float(order.executed.price)

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
            if order == self._entry_order:
                self._entry_order = None

    def notify_trade(self, trade: bt.Trade) -> None:
        if trade.justopened:
            if self._pending_setup:
                self._trade_map[trade.ref] = {
                    "setup": self._pending_setup,
                    "size": self._pending_entry_size,
                }
                self._strategy.on_open_position(self._next_trade_id, self._pending_setup)
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
            result = {
                "pnl": trade.pnl,
                "pnl_comm": trade.pnlcomm,
                "exit_reason": exit_reason,
                "setup": setup,
            }
            self._trade_info[trade.ref] = {
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
            self._strategy.on_close_position(self._next_trade_id, result)
            self._emit(
                EventType.TRADE_CLOSED,
                {
                    "trade_id": self._next_trade_id,
                    "pnl": trade.pnl,
                    "pnl_comm": trade.pnlcomm,
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

    def _check_drawdown(self) -> None:
        max_dd = self.params.max_drawdown
        if not max_dd or max_dd <= 0 or self._equity_peak <= 0:
            return
        dd_pct = 100.0 * (self._equity_peak - self.broker.getvalue()) / self._equity_peak
        if dd_pct > max_dd and not self._dd_limit_hit:
            self._dd_limit_hit = True
            self._emit(EventType.DRAWDOWN_LIMIT_HIT, {"drawdown_pct": dd_pct, "limit": max_dd})
            try:
                self.cerebro.runstop()
            except Exception:
                pass

    def stop(self) -> None:
        self._emit(
            EventType.SESSION_END,
            {
                "total_trades": self._next_trade_id - 1,
                "final_value": self.broker.getvalue(),
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
