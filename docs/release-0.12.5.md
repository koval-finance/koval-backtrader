# Release 0.12.5 preparation and adoption

Prepared 2026-10-30. This is a local release candidate until the owner commits,
pushes and tags the reviewed tree and the release workflow publishes it.
The package version is `0.12.5`; the Git tag must be `v0.12.5`.

## Contents and compatibility

This patch carries the [Founder Spot review fixes](technical-review-2026-10-30.md):

- Refuse instrument-invalid protection replacements without canceling working
  protection, changing carried-entry/account stop state or rejecting the filled
  entry intent. Invalid strategy instructions still raise.
- Require outer protocol 2 and execution contract 2 for native historical Spot
  Grid/DCA; legacy protocol 1 remains supported.
- Require `setuptools>=77.0.3` for the existing SPDX package metadata.
- Correct canonical identity documentation and complete the custom-engine
  input/capability contract.

Runtime dependencies and the engine protocol are unchanged. The supported
engine range remains `>=0.12.7,<0.13.0`; application adoption should retain
engine `0.12.8` and change only the adapter pin from `0.12.4` to `0.12.5`.
The native/numeric, paper and venue support boundaries remain unchanged.

Callers must recognize `ORDER_REJECTED` with
`operation="protection_replace"` as a refused replacement, not an unsuccessful
entry. Its working order ID, size, requested stop/target and instrument reason
are documented in [results](results.md#events).

## Candidate validation

Fresh Python 3.13.5 environments installed each exact published engine wheel
with the candidate adapter wheel. Both passed `pip check`, plugin discovery,
every installed package byte check and the full `scripts/verify.sh` gate:

| Installed pair | Full gate | Acceptance manifest |
| --- | --- | --- |
| Engine 0.12.7 / adapter 0.12.5 | 1174 passed, 85.76 s; lint/format clean | [0.12.7 pair](acceptance/2026-10-30/pair-engine-0.12.7-adapter-0.12.5-py313.json) |
| Engine 0.12.8 / adapter 0.12.5 | 1174 passed, 85.86 s; lint/format clean | [0.12.8 pair](acceptance/2026-10-30/pair-engine-0.12.8-adapter-0.12.5-py313.json) |

No tests failed or skipped in either pair. The isolated build, strict Twine
validation and distribution/source checks passed. The local candidate wheel is
96610 bytes, SHA-256
`6c84af34d545c2ce2f2004d5abecae97f3cb6d465e95a7351e8862449d423c07`.
Hosted Python 3.11/3.12/3.13 CI and tag-triggered publication remain owner steps;
only Python 3.13 was executed locally for this candidate.

The actual candidate wheel also passed 130 host historical/paper/numeric/native
recipe checks using existing application dependencies. Separate offline probes
confirmed the unchanged host Testnet/production 0.12.4 identities and current
artifact lock refuse 0.12.5, with zero venue requests. Those expected refusals
prove the migration boundary; they do not establish new host acceptance.

Reproduce candidate validation from this checkout:

```bash
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m build
.venv/bin/python scripts/check_dist.py --require-artifacts
.venv/bin/python -m twine check --strict dist/*
./scripts/verify.sh
```

Use `scripts/verify_pair.py` with each exact published engine wheel and
`dist/koval_backtrader-0.12.5-py3-none-any.whl` for clean installed-pair checks.
Never retain older-version wheels in `dist/`; move them to a separate archive
before validating the new version. Local artifact hashes identify local builds;
the hosted workflow rebuilds from the tagged commit, so downstream adoption
must obtain and verify the artifacts actually published to PyPI.

## Owner publication steps

1. Inspect `git status`, the complete diff and the new regression/release files.
   Confirm the checkout is `main` and reconcile any remote changes before
   committing. Check that `v0.12.5` does not already exist locally/remotely and
   PyPI has not acquired version 0.12.5 since preparation.
2. Stage the reviewed fixes, documentation, acceptance manifests and version
   metadata. Do not stage local environments, wheel caches or private handoffs.
3. Commit with DCO sign-off and push `main`. The commands below are for the
   owner; no agent executes them as part of preparation.

```bash
git add CHANGELOG.md README.md pyproject.toml src/koval_backtrader agents_docs docs \
  tests/test_package_metadata.py tests/test_spot_multi_order.py \
  tests/test_protection_replacement.py
git diff --cached --check
git diff --cached --stat
git commit -s -m "Release koval-backtrader 0.12.5"
git push origin main
```

4. Wait for every normal CI job on that commit: Python 3.11/3.12/3.13 tests,
   installed engine 0.12.7/0.12.8 pairs on Python 3.11/3.13, and DCO when
   applicable. If a repair is needed, commit and push it, then wait again.
5. Confirm the working tree is clean and `HEAD` is the exact green commit.
   Create and push only this annotated tag:

```bash
git tag -a v0.12.5 -m "koval-backtrader 0.12.5"
git push origin v0.12.5
```

6. Watch the Release workflow: verification, build/tag/changelog checks, four
   installed-pair jobs, PyPI Trusted Publishing and GitHub release creation.
   The existing workflow performs upload and release creation; do not also
   upload the local candidate by hand.
7. Verify the exact version-specific PyPI metadata and both published artifact
   hashes. Retain publication attestations, workflow URL and tagged commit with
   the application adoption evidence. If upload succeeded but GitHub release
   creation failed, inspect that job before retrying anything; PyPI filenames
   cannot be reused.

## Application adoption contract

Publication alone does not update the application. Use published artifacts,
not an editable override or a same-version candidate wheel, for final adoption.
The host's ordinary dependency pin, wheelhouse, image inputs, Testnet version
allowlists, production wheel/source identities and offline acceptance tooling
must agree on engine 0.12.8 / adapter 0.12.5 before claiming full adoption.

Keep previous artifact locks, accepted manifests and sealed run/session/candidate
identities intact. Add a new artifact lock derived from the new published wheel
bytes, update only the current selectors and obtain fresh offline acceptance.
An artifact lock records input identity; it is not by itself a new acceptance
receipt. Never copy old approval/acceptance hashes onto the new pair.

Changes to application source also change its source identity, so create final
runtime/candidate bindings after the application implementation is settled.
Old source-bound checkpoints, archived runs and paper recovery retain their
original environment or require a fresh run/candidate. Do not relabel them to
make replay or admission pass.

Required host checks include package/wheel integrity, legacy and numeric
historical contracts, native recipe archive/replay, paper comparisons, exact
Testnet identity refusal, production old-lock refusal and the newly bound
offline durability suite. Run all application CI gates before deployment.
No version-pin edit, simulator test or new offline receipt authorizes Start,
Testnet traffic or production orders.

The repository-specific current lock inventory, exact file touchpoints and
copyable app-agent handoff are retained separately in the maintainer's local
release bundle; they are not part of the public distribution.
