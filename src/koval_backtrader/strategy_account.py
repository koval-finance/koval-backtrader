# SPDX-License-Identifier: GPL-3.0-or-later
"""Bind the 0.11 graph bridge to the account maintained from broker fills.

GraphStrategy has no public account-injection hook in engine 0.11. Its private
account service otherwise books requested setup values a second time. This
narrow compatibility adapter supplies a read-only view at that seam; the real
ExecutionAccount is updated exclusively by execution notifications. Remove the
shim when the engine exposes a public binding hook, retaining the graph test.
"""

from koval_backtrader.execution_account import ExecutionAccount


class _StrategyAccountView:
    def __init__(self, account: ExecutionAccount) -> None:
        self._account = account

    def snapshot(self):
        return self._account.snapshot()

    def on_bar(self, **kwargs) -> None:
        """Broker equity was already applied before the strategy callback."""

    def on_open(self, **kwargs) -> None:
        """Requested setup callbacks cannot replace an actual broker fill."""

    def on_close(self, **kwargs) -> None:
        """Execution notifications already booked the closing cashflows."""


def bind_strategy_account(strategy, account: ExecutionAccount) -> None:
    if any(cls.__module__ == "koval.strategy.graph.strategy" for cls in type(strategy).__mro__):
        strategy._account = _StrategyAccountView(account)
        strategy._account_seeded = True
