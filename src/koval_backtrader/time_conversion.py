# SPDX-License-Identifier: GPL-3.0-or-later
"""One UTC millisecond conversion for v1 events, ledgers and injected state."""

from __future__ import annotations

from datetime import UTC, datetime

import backtrader as bt


def utc_ms(moment: datetime) -> int:
    """Milliseconds since the epoch, rounded, treating a naive value as UTC."""
    aware = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    return round(aware.timestamp() * 1000)


def num2utc_ms(num: float) -> int:
    """Milliseconds since the epoch for a Backtrader datetime ordinal."""
    return utc_ms(bt.num2date(num))
