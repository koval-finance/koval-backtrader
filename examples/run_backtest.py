# SPDX-License-Identifier: GPL-3.0-or-later
"""Run a backtest through this adapter, using koval-engine's bundled example.

Note what this file does *not* contain: an import of ``koval_backtrader``. The
engine finds the adapter through its entry point, so consumer code depends on
the MIT engine API alone and swapping in a different backtest engine changes
nothing here.

    python examples/run_backtest.py
"""

from __future__ import annotations

import json

from koval.cli.data import load_csv_candles
from koval.engine.backtest_engine import EngineRunSpec, load_backtest_engine
from koval.examples import example_path


def main() -> None:
    graph = json.loads(example_path("graphs", "ema_cross_trend.json").read_text(encoding="utf-8"))
    candles = load_csv_candles(example_path("data", "sample-1h.csv"))

    spec = EngineRunSpec(
        graph=graph["graph"],
        feeds={"1h": candles},
        initial_capital=10_000.0,
    )
    result = load_backtest_engine().run(spec)

    for key in sorted(result.metrics):
        print(f"{key:<24} {result.metrics[key]}")
    print(f"{'closed_trades':<24} {len(result.trades)}")


if __name__ == "__main__":
    main()
