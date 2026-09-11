# SPDX-License-Identifier: GPL-3.0-or-later
"""Prefer public account binding, retaining the engine 0.11.0 graph fallback."""

from koval.strategy.base.declarative import DeclarativeStrategy

from koval_backtrader.execution_account import ExecutionAccount


class _StrategyAccountView:
    """Read-only bridge for engine 0.11.0, which has no public binding hook."""

    def __init__(self, account: ExecutionAccount) -> None:
        self._account = account

    def snapshot(self):
        return self._account.snapshot()

    def on_bar(self, **kwargs) -> None:
        """Broker equity was already applied before the strategy callback."""

    def on_open(self, **kwargs) -> None:
        """Requested setups cannot replace actual broker fills."""

    def on_close(self, **kwargs) -> None:
        """Execution notifications already booked the closing cashflows."""


def bind_strategy_account(strategy: DeclarativeStrategy, account: ExecutionAccount) -> None:
    bind_account = getattr(strategy, "bind_account", None)
    if callable(bind_account):
        bind_account(account.snapshot)
        return
    if any(cls.__module__ == "koval.strategy.graph.strategy" for cls in type(strategy).__mro__):
        strategy._account = _StrategyAccountView(account)
        strategy._account_seeded = True
