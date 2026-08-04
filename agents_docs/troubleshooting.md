# Troubleshooting

Dated failure memory. Newest first. Add an entry when a failure costs more
than a few minutes to understand — the next reader is usually you.

## 2026-08-03 — `NoBacktestEngineError` even though the package is installed

**Symptom:** `load_backtest_engine()` raises `NoBacktestEngineError` listing
`available: none`, while `pip list` clearly shows `koval-backtrader`.
**Cause:** entry points are read from installed distribution metadata, not
from `pyproject.toml`. An editable install performed before the entry point
was declared leaves stale metadata behind.
**Fix:** reinstall — `pip install -e ".[dev]"`. `tests/test_entry_point.py`
catches this by comparing the declared entry point against the installed one.

## 2026-08-03 — `ModuleNotFoundError: koval.adapters.backtrader`

**Symptom:** an import of `koval.adapters.backtrader` fails even with both
packages installed.
**Cause:** that path no longer exists. The adapter is top-level
`koval_backtrader` because `koval` is a regular package owned by the engine;
see [invariants.md](invariants.md).
**Fix:** import `koval_backtrader.*`. If you are reading old code or docs that
use the former path, they predate the split.

## 2026-08-03 — a trade appears that the strategy never opened

**Symptom:** the trade list contains a position with no matching entry
decision, usually on a bar where a stop and a target were both reachable.
**Cause:** Backtrader evaluates OCO cancellation after execution, so both legs
of a bracket can fill in the same bar.
**Fix:** `apply_oco_guard()` must have run. `backtest_runner` calls it at
import; a test that constructs `Cerebro` directly must call it too.

## Update this file when

A failure takes real time to diagnose. Record the symptom as it appeared, the
actual cause, and the fix — not a tidy retelling.
