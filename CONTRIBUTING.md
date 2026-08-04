# Contributing

Thanks for considering a contribution. Maintainer response times are
best-effort.

## Setup

```bash
git clone https://github.com/koval-finance/koval-backtrader.git
cd koval-backtrader
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

Requires Python 3.11+. Installing pulls `koval-engine` from PyPI.

## Tests and lint

```bash
./scripts/verify.sh
```

That runs, in order:

```bash
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/python -m pytest -q
```

CI runs exactly these on Python 3.11, 3.12, and 3.13. Exit code 0 is the
definition of done.

Unlike koval-engine, there is no excluded test subset here — every test in
this repository needs Backtrader, which is the package's whole purpose.

## Tests come first

Write the failing test, watch it fail, then implement. This is not a style
preference: this package decides where simulated orders fill and what a closed
trade reports, and those numbers are what people use to judge whether a
strategy is worth money. Fill logic, bracket movement, or state transitions
without a test that was observed failing are not accepted.

## Rules CI enforces

If a guard fails, fix the cause — do not adjust the guard.

| Guard | Rule |
|---|---|
| `tests/test_entry_point.py` | The `koval.backtest_engines` entry point resolves with no environment variable, and the declared entry point matches the installed one. If this breaks, the package installs but does nothing. |
| `tests/test_license_headers.py` | Every `.py` under `src/` carries `# SPDX-License-Identifier: GPL-3.0-or-later`. |
| `tests/test_package_metadata.py` | No source file imports application code; no `koval` package is published from this distribution; the engine dependency stays a range. |
| `tests/test_public_surface.py` | Maintainer-private paths are never tracked by git. |
| `tests/test_public_language.py` | No private planning references in the published tree. |
| `tests/test_release_workflow.py` | The release pipeline gates on lint, tests, and a changelog entry before publishing. |

Two more expectations, not automated:

- **No new runtime dependencies** without opening an issue first. The list is
  `koval-engine`, `backtrader`, `numpy`, `pandas`, and keeping it short is
  deliberate.
- **Never claim the `koval` import namespace.** koval-engine ships
  `koval/__init__.py` as a regular package, so a second distribution adding to
  it shadows unpredictably rather than merging. This package is top-level
  `koval_backtrader`.

## Upgrading Backtrader

`src/koval_backtrader/oco_patch.py` reproduces Backtrader broker internals to
fix a bug where both legs of a bracket can fill in the same bar. An upstream
change to that code path can corrupt fills silently instead of raising. Treat
any Backtrader version bump as a behavioural change: run the full suite and
say in the pull request that you did.

## Developer Certificate of Origin

Every commit must carry a `Signed-off-by` line, added by `git commit -s`. It
certifies that you wrote the patch or have the right to submit it under this
project's licence. The full text is at
[developercertificate.org](https://developercertificate.org/).

CI rejects commits without it. Fix an existing branch with
`git commit --amend -s` or `git rebase --signoff main`.

## Pull requests

Open an issue first for anything beyond a bug fix. Keep the pull request
focused on one change, describe what you tested, and make sure
`./scripts/verify.sh` passes locally before asking for review.
