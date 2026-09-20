# Troubleshooting

Dated failure memory. Newest first. Add an entry when a failure costs more
than a few minutes to understand — the next reader is usually you.

## 2026-09-20 — installed-pair CI fails only on engine 0.11.1

**Symptom:** the Python 3.11 and 3.13 jobs for engine 0.11.1 fail in
`test_indicator_preparation.py` with `ProtocolVersionError: runtime boundaries
require koval-engine 0.12 or later`; the 0.12.0 and 0.12.1 pairs pass.
**Cause:** the preparation tests reused `spec_for()`, which deliberately attaches
the 0.12 runtime-boundary contract even when the installed `EngineRunSpec` does
not declare it. The tests accidentally coupled an optional preparation hook to
an unrelated newer protocol feature.
**Fix:** clear the runtime contract when the installed engine lacks that field.
The same test still asserts boundary-trimmed preparation on 0.12+, while 0.11.1
asserts preparation of the full simulator feed. Re-run every installed pair.

## 2026-09-12 — closed HTF history changes when a future row changes

**Symptom:** after the first HTF close, changing later candles changes previously
available HTF arrays. The first-close cutoff tests alone still pass.
**Cause:** `LineBuffer.get(ago=...)` uses positive offsets to read forward.
Skipping a forming row with `ago=skip` selected later preloaded data.
**Fix:** use `ago=-skip`, assert history after a closed row is followed by a
forming row, and mutate future HTF values independently. The fix applies to
all profiles. Explicit runtime boundaries also align the rolling HTF history
with the paper runtime and gate entry until required HTF data is available.

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
