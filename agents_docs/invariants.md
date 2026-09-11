# Invariants

Every load-bearing rule in this repository, and what enforces it. A rule with
a test is checked on every run; a rule without one is the reviewer's job, and
saying so explicitly is more honest than implying automation that does not
exist.

## Pinned by tests

| Invariant | Test |
|---|---|
| Every `.py` under `src/` carries the GPL SPDX header | [`tests/test_license_headers.py`](../tests/test_license_headers.py) |
| The GPL-3.0 licence text ships with the distribution | [`tests/test_license_headers.py`](../tests/test_license_headers.py) |
| The `backtrader` entry point resolves with no environment variable | [`tests/test_entry_point.py`](../tests/test_entry_point.py) |
| The declared entry point matches the installed one | [`tests/test_entry_point.py`](../tests/test_entry_point.py) |
| No source file imports application code | [`tests/test_package_metadata.py`](../tests/test_package_metadata.py) |
| The installed MIT engine never imports Backtrader | [`tests/test_package_metadata.py`](../tests/test_package_metadata.py) |
| Every hook, event, and trade record agrees on a trade's id | [`tests/test_bt_adapter.py`](../tests/test_bt_adapter.py), [`tests/test_backtest_runner.py`](../tests/test_backtest_runner.py) |
| `on_bar()` runs once per bar, as it does in the live runner | [`tests/test_bt_adapter.py`](../tests/test_bt_adapter.py) |
| No `koval` package is published from this distribution | [`tests/test_package_metadata.py`](../tests/test_package_metadata.py) |
| The engine dependency is a range, not an exact pin | [`tests/test_package_metadata.py`](../tests/test_package_metadata.py) |
| Maintainer-private paths are never tracked by git | [`tests/test_public_surface.py`](../tests/test_public_surface.py) |
| No plugin source directly imports venue clients | [`tests/test_public_surface.py`](../tests/test_public_surface.py) |
| No private planning language reaches the published tree | [`tests/test_public_language.py`](../tests/test_public_language.py) |
| The sdist contains the whole public test suite | [`tests/test_sdist_contents.py`](../tests/test_sdist_contents.py) |
| The release pipeline gates on lint, tests, and the changelog | [`tests/test_release_workflow.py`](../tests/test_release_workflow.py) |
| Fills, costs and account state agree with the MIT paper broker | [`tests/test_paper_parity.py`](../tests/test_paper_parity.py) |
| Every divergence from paper carries a declared reason code, and a stale exemption fails | [`tests/test_paper_parity.py`](../tests/test_paper_parity.py) |
| Every shipped engine parity fixture is reproduced in full, none skipped | [`tests/test_parity_fixtures.py`](../tests/test_parity_fixtures.py) |
| A declared spot market permits neither shorts nor leverage | [`tests/test_market_identity.py`](../tests/test_market_identity.py) |
| A run that cannot prove its inputs is graded `partial`, never `full` | [`tests/test_run_identity.py`](../tests/test_run_identity.py) |
| Strategies see actual fills and the engine's own account state | [`tests/test_execution_account.py`](../tests/test_execution_account.py) |
| No metric is reported as zero or infinity when it is unavailable | [`tests/test_research_metrics.py`](../tests/test_research_metrics.py) |

## The licence boundary

`koval-engine` is MIT. Backtrader is GPL-3.0. Linking them makes the combined
work GPL, so the combination lives here and only here.

The arrow points one way:

```
koval-backtrader (GPL-3.0)  ──imports──▶  koval-engine (MIT)
```

This package may import `koval.engine` and `koval.strategy` freely. The engine
may never import this package; its own `tests/test_license_boundary.py`
enforces that from the other side. Neither side may import the application
that consumes both.

The practical failure mode is not malice, it is convenience: someone needs one
helper from the app, imports it, and an MIT codebase silently acquires a GPL
dependency. That is why the direction is a test and not a note.

`koval.exchanges` is a narrower case of the same convenience, and it is
forbidden. It carries the Binance and WhiteBIT clients and the HTTP stack
behind them, so importing it for one helper contradicts the no-live-venue rule
and puts a network client behind the entry point every consumer imports. 0.10.0
removed exactly such an import, added for a timeframe-duration lookup that
`koval.engine.timeframe_utils` already answers. If a helper you need lives only
under `koval.exchanges`, that is a signal it belongs in `koval.engine`, not a
reason to reach across.

## The import namespace

`koval-engine` ships `koval/__init__.py`, which makes `koval` a *regular*
package rooted in its own `site-packages` directory. Python resolves a regular
package the moment it finds one and stops scanning, so a second distribution
that also ships into `koval/` does not merge with it — one shadows the other,
and which one wins depends on path order.

This package is therefore top-level `koval_backtrader`. It must never grow a
`src/koval/` directory, however natural `koval.adapters.backtrader` may look.

## No real-money path

A backtest replays historical candles through a simulated broker. There is no
venue connection here and there must never be one: order routing to a real
exchange belongs to the engine's sandbox brokers, which are allowlisted to
paper and sandbox endpoints. Adding a network order path to this package would
route around that allowlist entirely.

## Backtrader version coupling

`oco_patch.py` reproduces Backtrader internals to fix a ghost-trade bug in OCO
handling. That is a deliberate, fragile coupling: an upstream change to the
patched code path can corrupt fills silently rather than raising. Treat any
Backtrader upgrade as a behavioural change requiring a full test run, and read
[troubleshooting.md](troubleshooting.md) first.

`execution_broker.py` also couples to `BackBroker._execute`: hypothetical
submission checks must stay separate from real executions, and actual fill
prices must reach the broker before fees and account value are computed.
Any upstream upgrade needs the limit/gap, cash-rejection, OCO and reconciliation
tests in addition to a source review. The audited version and source hash are
recorded in [../docs/execution-research.md](../docs/execution-research.md).

## Parity with the MIT paper broker

`ohlcv_fixed_v1` and the engine's `paper_ohlcv_fixed_v1` are two independent
implementations of one contract. That is the point: agreement between them is
evidence, where agreement between a thing and itself is not.

`tests/test_paper_parity.py` compares baseline v1/v2 fills and full account
snapshots with no active waivers. `tests/test_realistic_evidence.py` adds evidence
and partial lifecycle scenarios. It has one exact bar-equity waiver for the
published engine's partial-exit mark defect, using the engine comparator:
unexpected differences and unused waivers both fail. Quantity/volume regressions
also pin deliberate plugin corrections. Never broaden a waiver to silence a
failure. Update [the review record](../docs/execution-validation.md) when an
upstream fix changes these boundaries.

`strategy_account.py` is a version-scoped private bridge: graph contexts must
read the actual broker account. Do not remove its graph regression until the
engine exposes and the plugin adopts a public binding hook.

Source distribution identity is pinned by `tests/test_dist_artifacts.py` and
`scripts/check_dist.py`. A version is not enough if local package bytes differ.

## Update this file when

A guard test is added, removed, or changes what it protects; or a rule moves
between "pinned by a test" and "the reviewer's job".
