# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit end-of-data execution without inventing an extra candle."""


def finalize_runtime(strategy):
    boundaries = strategy.params.runtime_boundaries
    if boundaries is None:
        return
    broker = strategy.broker
    retain = boundaries.end_of_data_policy == "mark_at_last_close"
    for order in (strategy._entry_order, strategy._stop_order, strategy._tp_order):
        if order is not None and order.alive() and (not retain or order == strategy._entry_order):
            strategy.cancel(order)
    if strategy.position and not retain:
        submit = strategy.sell if strategy.position.size > 0 else strategy.buy
        order = submit(
            size=abs(strategy.position.size),
            _checksubmit=False,
            koval_role="end_of_data",
            koval_terminal=True,
        )
        broker.pending.remove(order)
        broker._execute(order, ago=0, price=float(strategy.data.close[0]))
    # Cerebro will not advance again. Deliver actual notifications to the usual
    # strategy and trade analyzers, then refresh valuation without matching a bar.
    while (notification := broker.get_notification()) is not None:
        strategy._addnotification(notification)
    strategy._notify()
    strategy.clear()
    broker._get_value()
    strategy._account.on_bar(equity=broker.getvalue(), timestamp_ms=strategy._safe_timestamp_ms())
