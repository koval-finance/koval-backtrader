# AGENTS.md — koval-backtrader

Context for AI coding agents and their humans. Start here; depth lives in
[agents_docs/README.md](agents_docs/README.md).

## What this is

koval-backtrader runs [koval-engine](https://github.com/koval-finance/koval-engine)
strategy graphs through [Backtrader](https://github.com/mementum/backtrader).
It is a plugin, not a framework: the engine defines `BacktestEngineProtocol`
and discovers implementations through the `koval.backtest_engines` entry-point
group, and this package is one.

The engine is MIT and cannot import Backtrader, which is GPL-3.0. That is the
entire reason this repository exists as a separate distribution. Installing
`koval-engine` alone gives you a strategy engine that cannot run a backtest;
installing this alongside it completes the picture.

Distribution: `koval-backtrader`. Import package: `koval_backtrader`.

## Non-negotiables

[agents_docs/invariants.md](agents_docs/invariants.md) says which test pins
each of these and which are the reviewer's job instead. They carry the same
weight either way.

- **GPL-3.0-or-later, and every source file says so.** Each `.py` under `src/`
  starts with `# SPDX-License-Identifier: GPL-3.0-or-later`.
- **The dependency arrow points one way.** This package imports `koval.engine`
  and `koval.strategy` freely. Nothing here may import application code, and
  nothing in the engine may import this. An import in the wrong direction
  relicenses an MIT codebase by accident.
- **Never claim the `koval` import namespace.** The engine ships
  `koval/__init__.py` as a regular package; a second distribution adding to it
  shadows unpredictably. This package is top-level `koval_backtrader`.
- **No real-money code path.** Backtests are simulations over historical
  candles. Never add an order path that reaches a live venue.
- **English only** in code, comments, docstrings, tests, and docs.
- **Agents never commit.** No commits, pushes, tags, rebases, merges, or
  history rewrites. Prepare changes, run verification, suggest a commit
  message, stop. All git actions belong to a human.
- **When a guard test fails, fix the cause — never the guard.**

## Setup

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

Requires Python 3.11+. Installing pulls `koval-engine` from PyPI.

## Definition of done

```bash
./scripts/verify.sh
```

Exit code 0 means done: lint, formatting, and the full test suite. Nothing
else counts, and no prose argument substitutes for it.

Every test here needs Backtrader, so unlike the engine there is no marker to
exclude and no partial run to defend.

## Repository map

```
src/koval_backtrader/
├── backtest_runner.py   BacktraderBacktestEngine and the create_engine factory
├── bt_adapter.py        DeclarativeStrategy → bt.Strategy bridge, HTF injection
├── bt_analyzers.py      equity-curve and closed-trade analyzers
├── execution_config.py  versioned assumptions and legacy fee resolution
├── execution_broker.py  synthetic price costs and actual-fill ledger
├── execution_audit.py   model metadata, attribution and reconciliation
└── oco_patch.py         guard against a Backtrader OCO ghost-trade bug
```

`tests/` is flat and mirrors those module names.

Two documentation trees, and they are not interchangeable. `agents_docs/` is
this one: invariants, the work loop, release mechanics.
[docs/](docs/README.md) is written for users and contributors and goes deeper
into observable behaviour — fill rules, output fields, event payloads. When a
change alters what a user can observe, both trees need updating, and
`tests/test_docs.py` pins the human tree's links the way
`tests/test_agents_docs.py` pins this one's.

## The seam

```toml
[project.entry-points."koval.backtest_engines"]
backtrader = "koval_backtrader.backtest_runner:create_engine"
```

`load_backtest_engine()` reads that group, takes the entry point named
`backtrader`, imports it, and calls it. Installing the package is the whole
configuration story — no environment variable, no import, no wiring. The
`KOVAL_BACKTEST_ENGINE` variable exists as an override for development and
takes a module path, not an entry-point name.

`tests/test_entry_point.py` is the guard, and it is the most important file
here: if it fails, `koval backtest` is broken for every user even when every
other test is green.

## How to work here

1. Restate the task and name what is out of scope before editing anything.
2. Write the failing test first and watch it fail. This package decides where
   simulated orders fill; calculation or state-transition logic without a test
   that was observed failing is not accepted.
3. Implement the minimum that makes it pass. No drive-by refactoring.
4. Run `./scripts/verify.sh`.
5. Review your own diff, report, and stop before any git action.

Full workflow: [agents_docs/agent_workflow.md](agents_docs/agent_workflow.md).

## Where to read next

| Task | Read first |
|---|---|
| Anything touching licensing or the seam | [agents_docs/invariants.md](agents_docs/invariants.md) |
| Adapter internals, fills, event flow | [agents_docs/architecture.md](agents_docs/architecture.md) |
| Test failure or unexpected behaviour | [agents_docs/troubleshooting.md](agents_docs/troubleshooting.md) |
| Writing or changing tests | [agents_docs/testing.md](agents_docs/testing.md) |
| Cutting a release | [agents_docs/release_process.md](agents_docs/release_process.md) |
| What a user actually observes | [docs/execution-model.md](docs/execution-model.md), [docs/results.md](docs/results.md) |
| Everything else | [agents_docs/README.md](agents_docs/README.md) |
