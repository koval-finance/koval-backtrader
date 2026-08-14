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
| No private planning language reaches the published tree | [`tests/test_public_language.py`](../tests/test_public_language.py) |
| The sdist contains the whole public test suite | [`tests/test_sdist_contents.py`](../tests/test_sdist_contents.py) |
| The release pipeline gates on lint, tests, and the changelog | [`tests/test_release_workflow.py`](../tests/test_release_workflow.py) |

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

## Update this file when

A guard test is added, removed, or changes what it protects; or a rule moves
between "pinned by a test" and "the reviewer's job".
