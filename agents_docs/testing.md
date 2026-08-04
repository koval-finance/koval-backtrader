# Testing

```bash
./scripts/verify.sh                       # the full gate
.venv/bin/python -m pytest -q             # tests only
.venv/bin/python -m pytest tests/test_bt_adapter.py -q
```

## Layout

`tests/` is flat and mirrors the module names: `test_bt_adapter.py`,
`test_bt_adapter_htf.py`, `test_bt_analyzers.py`, `test_oco_patch.py`,
`test_backtest_runner.py`, `test_state_injection_arrays.py`, and
`test_block_assembler_integration.py` for the end-to-end path.

Repository-level guards sit alongside them: entry point, licence headers,
package metadata, public surface, public language, agent docs, release
workflow, sdist contents, version.

## No markers

The engine excludes `backtrader`-marked tests because it must not depend on
Backtrader. Here every test needs it, so there is no subset to exclude and no
partial run to defend. `pytest -q` is the whole suite.

## What "tested" means

- Tests are written first and observed failing. This package decides where
  simulated orders fill; fill logic, bracket movement, or state transitions
  without a test that was seen failing are not accepted.
- Cover boundaries, not happy paths: the bar where a stop and a target are
  both eligible, an entry that fills at a gap, a trailing stop that would move
  backwards, an empty feed.
- Assert on observable output — emitted events, closed trades, equity points —
  not on adapter internals. Internals are free to change; the contract is not.

## Writing a Backtrader test

The established shape is a small `bt.Strategy` or `DeclarativeStrategy` probe
that records what it saw, driven over a synthetic `pd.DataFrame` feed. Build
price series that make the case unambiguous rather than realistic: a clean
trend when testing entries, a single engineered gap when testing fills.

Call `apply_oco_guard()` at module import in any test that places bracket
orders — the patch is global and the runner applies it, so a test that skips
it is testing unpatched Backtrader.

## Guards have to be seen failing

Every guard in this repository was verified by breaking the thing it protects
and watching it go red. If you add one, do the same and say so in the report.
A guard that has only ever passed is decoration.

## Update this file when

The suite gains a directory, a fixture convention changes, or a new class of
guard is added.
