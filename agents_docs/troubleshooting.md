# Troubleshooting

Dated failure memory. Newest first. Add an entry when a failure costs more
than a few minutes to understand — the next reader is usually you.

## 2026-10-29 — native Spot CI uses an engine without its public contracts

**Symptom:** all main CI jobs fail while collecting `test_spot_multi_order.py`:
engine 0.12.6 has no `koval.engine.spot_multi_order`. With published engine
0.12.8, the runtime-fixture test instead raises `KeyError: candles`.
**Cause:** adapter source adopted new contracts before the dependency floor and
installed-pair matrix adopted their first published engine 0.12.7. Separately,
the runtime directory now contains multiple JSON schemas; not every file is a
boundary/account fixture.
**Fix:** adapter 0.12.4 requires engine 0.12.7 and tests exact 0.12.7/0.12.8
pairs. Select the named boundary fixture without changing account expectations;
exercise the new public historical level graph and volume veto separately
through the real plugin loader. Retain all execution assertions, with no waivers
or skips. Local acceptance does not change the remote CI result until the owner
commits and pushes the code changes.

## 2026-10-21 — corrected engine exposes 12 paper-parity failures

**Symptom:** engine 0.12.6 with unchanged adapter 0.12.2 fails ten fixed and two
realistic generated parity scenarios. A long with SL 90 / TP 120 incorrectly
stops at 90 on `O125 H130 L80 C100`; adding a signal exit changes it to TP 125.
**Cause:** fixed OCO queue order and realistic intrabar stop-first selection
ignored an already working target at the known open. Realistic entry-bar target
pricing also reused an open that preceded the limit entry.
**Fix:** prioritize opening protection before intrabar selection, independently
of signal exits, and honor `allow_open_gap=False` for entry-bar targets.
Preserve profile activation differences, cost arithmetic and shared liquidity.
The accompanying midnight daily-loss regression is fixed by the engine's shared
account state; do not add duplicate accounting or reorder adapter notifications.

The old engine 0.12.5 can agree with the old adapter while both share these
defects. Its green historical result is not corrected-pair evidence. Adapter
0.12.3 rejects those engines through its dependency floor and verifies the
corrected 0.12.6 pair without weakening execution assertions.

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
