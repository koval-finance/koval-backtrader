# Documentation

Human-facing documentation for koval-backtrader. Start with whichever line
below matches what you are doing.

| I want to | Read |
|---|---|
| Run my first backtest | [getting-started.md](getting-started.md) |
| Understand what a result means | [results.md](results.md) |
| Know whether I can trust the numbers | [execution-model.md](execution-model.md) |
| Align runtime windows and inspect execution evidence | [runtime-assurance.md](runtime-assurance.md) |
| Inspect cost-model research and follow-ups | [execution-research.md](execution-research.md) |
| Audit execution tests and reconciliation | [execution-validation.md](execution-validation.md) |
| Review timing fixes and deferred effects | [execution-plan.md](execution-plan.md) |
| Write a strategy | [strategies.md](strategies.md) |
| Change the adapter's code | [architecture.md](architecture.md) |
| Replace Backtrader with something else | [custom-engines.md](custom-engines.md) |
| Fix something that is broken | [troubleshooting.md](troubleshooting.md) |

If you read only one page, make it
[execution-model.md](execution-model.md). Everything a backtest claims rests
on the mechanics described there, and most disappointments with backtesting
come from not knowing them.

## The rest of the repository

| File | Purpose |
|---|---|
| [../README.md](../README.md) | What this package is and why it is separate from koval-engine |
| [../CONTRIBUTING.md](../CONTRIBUTING.md) | Setup, the test-first rule, DCO sign-off, what CI enforces |
| [../SECURITY.md](../SECURITY.md) | What counts as a vulnerability here, and how to report one |
| [../CHANGELOG.md](../CHANGELOG.md) | Released versions and what changed |
| [../AGENTS.md](../AGENTS.md) | Entry file for AI coding agents; [../agents_docs/](../agents_docs/README.md) holds the depth |

The `agents_docs/` tree overlaps this one on purpose. It is written for
agents working inside the repository — invariants and the tests that pin
them, the work loop, release mechanics — and is deliberately terse. These
pages are for people, and go further into behaviour and rationale.
