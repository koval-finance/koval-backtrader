# Execution validation and 0.11 release review

Prepared 2026-09-11 against **published PyPI koval-engine 0.11.0** and
Backtrader 1.9.78.123. The engine's
[Runtime and identity contract — 0.11](https://github.com/koval-finance/koval-engine/blob/v0.11.0/agents_docs/runtime_contract.md)
was read before implementing the plugin changes. The source of truth for
observable rules is [execution-model.md](execution-model.md).

Earlier notes attributing 694-line versus 1656-line paper brokers to the
published 0.10.0 release confused a local artifact with the release. Those
claims and the associated stale exemptions are superseded by this record.

## Verification matrix

| Area | Evidence |
|---|---|
| Discovery and GPL boundary | `test_entry_point.py`, `test_package_metadata.py`, `test_license_headers.py`, `test_public_surface.py` |
| Every published fixture, dispatched by version | `test_parity_fixtures.py`; includes v2 ambiguity and spot rejection, no fixture skipped |
| Independent v1/v2 matching and account state | `test_paper_parity.py`; hand-written cases and 120 deterministic generated cases per profile, including market/limit/stop entries |
| Actual LiveEngine integration | `test_engine_signal_parity.py`; real replay, spot rejection followed by a long, dynamic target, identical primary stream hashes |
| Graph account and constant-time totals | `test_execution_account.py`; real GraphExecutor context uses actual prices/fees, complete AccountSnapshot fields, no historical ledger iteration during snapshots |
| Funding and evidence fees | `test_realistic_evidence.py`; both sides/rate signs, boundary order, coverage failures, maker/taker assumptions, evidence-driven affordability |
| Instruments and liquidation | Same file; tick/step normalization, replacement, risk caps, mark coverage, liquidation before protection, event/ledger reconciliation |
| Partial lifecycle, impact and latency | Same file; carry/cancel, cumulative quantities and fees, partial OCO, one volume budget, lagged calibration, stable IDs, submission delay |
| Exact inputs and replay | `test_run_identity.py`, JSON evidence round-trip in `test_realistic_evidence.py` |
| Metrics and open positions | `test_research_metrics.py`, weighted closed-trade attribution and two independent reconciliation equations |
| Artifact identity and release | `test_dist_artifacts.py`, `test_release_workflow.py`, `test_sdist_contents.py` |

The release gate is `./scripts/verify.sh`: lint, formatting and the complete
suite. Build wheel and sdist, run `scripts/check_dist.py --require-artifacts`,
then `twine check --strict dist/*`. The default suite checks any local dist
artifacts too; rebuild them after changing package source. Do not install a
stale local artifact into another repository to measure parity.

## Release verification

Validated locally on Python 3.13.5 with koval-engine 0.11.0,
Backtrader 1.9.78.123, NumPy 2.5.3 and pandas 3.0.5:

- `./scripts/verify.sh`: **787 passed**, lint and formatting passed.
- Wheel and sdist built successfully; package-byte and version-metadata checks
  passed for both; strict Twine validation passed.
- Fresh virtual environment outside the checkout: installed the wheel and
  published engine through pip, verified the `site-packages` import path and
  `load_backtest_engine()` discovery, and passed `pip check`.
- **59 tests passed through the installed wheel**: all public parity fixtures,
  entry-point checks, advanced evidence cases and actual LiveEngine integration.
- `git diff --check` passed. Commit, push, tag and publication are human-owned.

The release workflow additionally runs its Python 3.11/3.12/3.13 matrix after
the owner pushes a tag. That remote run is not claimed as completed here.

## Review findings corrected in this repository

The implementation was reviewed after the initial changes and exercised with
additional failing regressions. Corrections include:

- Actual broker ledger binding, including the graph executor's account, instead
  of a backtest-only attribute the graph did not read. Fees no longer create an
  artificial pre-fee equity peak in costed account snapshots.
- Atomic protective validation and instrument normalization, preserving targets
  during stop replacement and protecting partial entry remainders.
- Weighted partial entry/exit accounting, distinct deterministic order IDs,
  liquidation fees in closed events, and an auditable session account ledger.
- Funding settlement before orders, quote-fee validation, evidence fee precedence
  in affordability and explicit refusal of missing mark coverage even when flat.
- Re-quantizing entry quantity after risk/volume caps and charging liquidity only
  for actual fills. Favorable tick rounding no longer breaks reconciliation.
- Floating-point residuals in stepped partial exits no longer strand rounding
  dust; non-quote collateral is refused by linear accounting.
- Primary-clock deduplication and trimming of trailing HTF data. Three feeds are
  rejected because only one primary and one HTF can reach the strategy.
- Separate all-fill totals from closed-trade cost ratios; do not call OHLC
  excursion diagnostics lower bounds or caller-provided identities certification.
- Distribution byte/metadata checks before upload. The newly added guard was
  observed rejecting the pre-existing stale local 0.11.0 artifacts.

## Remaining engine 0.11 integration gaps

These are limits on cross-runtime conformance, not instructions to change
engine source from this repository. Keep the plugin's financially consistent
behavior and use the following cases when preparing the next engine patch.

| Reason code | Reproduction and required engine behavior |
|---|---|
| `paper_partial_exit_marks_residual_at_fill` | `test_lagged_impact_is_disclosed_per_fill`: entry quantities 1 and 0.9880551136809625 around 100.0315; first target exit 1 at 119.96205266807799 on a bar closing 120. Plugin marks remaining exposure at 120 (equity 10039.660474288836); paper marks it at the exit price (10039.622980233482). Fills and final flat equity match. Mark residual exposure at bar close before the next decision. |
| `paper_risk_quantity_not_requantized` | `test_risk_capped_entries_remain_on_the_venue_quantity_step`: requested 2 units, entry 100, stop 90, step 0.1, commission 4 bps, slippage 10 bps. Engine risk sizing produces 1.9481776940667475 after normalizing the order; plugin floors the final quantity to 1.9. Reapply the venue step/minima after every sizing cap. |
| `paper_reserves_unfilled_liquidity` | `test_risk_clipping_consumes_only_actual_liquidity`: 2-unit shared bar budget, requested entry 2, commission 4 bps and a stop-touch entry bar. Risk sizing reduces the entry. Plugin makes the remaining budget available to the protective exit that bar; engine consumes the requested allocation first and withholds that liquidity. Debit only actual fills. |
| `graph_account_not_bound_to_runtime` | Engine `GraphStrategy._ctx()` reads its private PlatformAccountState, while LiveEngine maintains another broker-backed account. `_on_open()` still forwards the requested setup to the graph hook. Plugin's `strategy_account.py` binds graph reads to actual fills. Engine needs a public authoritative account binding hook, with graph-context tests for fees, funding, partial quantities and gaps. |
| `terminal_policy_differs` | Backtest retains and marks final exposure; LiveEngine finalization closes it and charges applicable exit costs. Compare pre-finalization snapshots, or choose an explicitly agreed terminal policy before comparing totals. |

Only the specific partial-exit bar equity path has a used, reason-coded waiver
in the advanced differential harness. That waiver fails when it becomes stale;
fill prices, quantities, fees, balance and terminal flat equity still match.
The quantity and liquidity regressions assert the plugin's corrected behavior;
they are not evidence that those combinations currently agree with engine.
Baseline `test_paper_parity.py` has no active waivers.

Graph-dependent account decisions, instrument-plus-risk sizing and costed
partial execution must not be advertised as generally interchangeable with
engine 0.11.0 paper results. Also verify LiveEngine's gap-containment checks when
expanding full-runtime tests beyond the PaperBroker fixtures.

## Evidence and model limits

The fee liquidity role is a bar-model assumption: a resting entry limit is
classified maker, with other fills taker. Current venue snapshots do not become
historical evidence by assigning them a backtest date. Cross-currency fee
conversion and complete historical venue filters are not implemented.

The instrument contract represents a subset of exchange rules. Liquidation is
single-position linear cross-margin using archived marks and maintenance tiers,
not portfolio, isolated, inverse or delivery margin. Cancellation/replacement
latency is refused when nonzero. Funding coverage on an empty normalized series
remains an upstream caller assertion; the archive must be retained separately.

A calibrated volume/impact proxy cannot establish queue priority or observed
bid/ask. No venue credentials, orders, application changes or release publication
are part of this work. A successful offline gate is not a promotion approval
for an application's live or sandbox trading tier.
