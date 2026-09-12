# SPDX-License-Identifier: GPL-3.0-or-later
"""Apply the optional MIT runtime boundary contract before constructing Cerebro."""

from dataclasses import replace

import numpy as np
from koval.engine.backtest_engine import ProtocolVersionError
from koval.engine.timeframe_utils import ordered_timeframes, timeframe_to_minutes


def prepare_runtime(spec):
    """Return consumed inputs and validated boundaries; old specs keep their defaults."""
    value = getattr(spec, "runtime_contract", None)
    if value is None:
        return spec, None
    try:
        from koval.engine.run_boundaries import resolve_runtime_boundaries
    except ImportError as exc:
        raise ProtocolVersionError("runtime boundaries require koval-engine 0.12 or later") from exc
    boundaries = resolve_runtime_boundaries(value)
    timeframe = ordered_timeframes(list(spec.feeds))[0]
    boundaries.validate_inputs(timeframe=timeframe, initial_capital=spec.initial_capital)
    step = timeframe_to_minutes(timeframe) * 60_000
    rows = spec.feeds[timeframe]
    consumed = rows[rows[:, 0] < boundaries.evaluation_end_ms]
    expected_count = (boundaries.evaluation_end_ms - boundaries.warmup_start_ms) // step
    if (
        len(consumed) != expected_count
        or not len(consumed)
        or consumed[0, 0] != boundaries.warmup_start_ms
        or np.any(np.diff(consumed[:, 0]) != step)
    ):
        raise ValueError("runtime warmup/evaluation coverage is incomplete")
    feeds = {tf: bars[bars[:, 0] < boundaries.evaluation_end_ms] for tf, bars in spec.feeds.items()}
    return replace(spec, feeds=feeds), boundaries
