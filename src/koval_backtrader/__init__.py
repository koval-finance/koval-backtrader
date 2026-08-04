# SPDX-License-Identifier: GPL-3.0-or-later
"""Backtrader backtest engine for the Koval strategy engine.

This package is GPL-3.0 because it imports Backtrader. It consumes the
MIT-licensed ``koval`` core; the core never imports this package. That
direction is the whole point of the split and is enforced by tests in both
repositories.

``koval.engine.backtest_engine.load_backtest_engine()`` finds
:func:`create_engine` through the ``koval.backtest_engines`` entry-point
group, so importing this module by hand is not normally necessary.
"""

from __future__ import annotations

from importlib import metadata

from koval_backtrader.backtest_runner import BacktraderBacktestEngine, create_engine

__version__ = metadata.version("koval-backtrader")

__all__ = ["BacktraderBacktestEngine", "__version__", "create_engine"]
