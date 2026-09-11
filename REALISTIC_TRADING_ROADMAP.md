# Realistic trading roadmap — koval-backtrader

Status updated 2026-09-11 for plugin 0.11.0 and **published engine 0.11.0**.
This is the repository-local backlog. Observable behavior lives in
[docs/execution-model.md](docs/execution-model.md), and exact review findings
and test evidence in [docs/execution-validation.md](docs/execution-validation.md).

The plugin owns independent historical matching, Backtrader integration,
accounting and result disclosure. The MIT engine owns strategy/runtime contracts,
normalized evidence and paper/sandbox execution. Application storage, promotion,
export and UI integration remain application work. No venue order path belongs
in this GPL plugin.

## P0 — Conformance and defect prevention

- [x] **BT-005: remove stale divergences.** The old favorable-limit, affordability,
  target-hook and spot exceptions no longer mask the published engine.
- [x] **BT-006: dispatch fixtures by contract/profile version.** Run every fixture,
  including v2 ambiguity, direct setup fixtures and spot refusals. Use the
  engine's strict comparator; no skipped fixture fallback.
- [x] **BT-007 / BT-004: actual account contract.** Graph contexts bind to broker
  fills, AccountSnapshot fields include daily loss/fees/funding/margin, and stop
  updates use final-pair validation. UTC daily baseline follows engine semantics.
- [x] **BT-008: spot semantics.** Reject spot leverage and softly refuse shorts;
  actual LiveEngine replay proves a subsequent long and a target update survive.
- [x] **BT-009 / BT-003: canonical market vocabulary.** Use the public
  `koval.engine.market_identity` API; do not import `koval.exchanges` directly.
  Preserve old input aliases but emit engine identities and reject unknown venues.
- [x] **BT-010: account-state cost.** Incremental ledger totals; repeated snapshots
  do not iterate trade history. Full entries remain available for audit.
- [x] **BT-011: artifact guard.** Compare source bytes and version metadata in
  wheel/sdist; run in the default suite and after release build, before upload.
- [x] **BT-001: independent matching coverage.** Both profiles, seeded cases,
  all public fixtures, costs, account state, OCO, gaps, replacement and refusal.
- [x] **BT-002: successor profile.** `ohlcv_realistic_v2`, entry-bar protection,
  deterministic stop-first ambiguity and local before-cost outcome sensitivity.
  Preserve v1 timing; do not present local alternatives as full-strategy bounds.

## R1 — Evidence-bound reproducibility

- [x] **BT-101:** emit `koval_run_identity_v1`, engine candle/profile/evidence
  hashes, graph identity, actual processed primary bounds, software and source
  fingerprints, plus retained plugin feed hashes.
- [x] Replay normalized evidence from JSON. Unidentified v1 data remains readable
  but not comparable. Caller provenance never awards a `full` grade.
- [ ] Application-owned immutable archives, historical completeness validation,
  sufficient warmup and matching terminal policies must still be demonstrated
  before a run is certified by a consumer.

## R2 — Perpetual funding

- [x] **BT-201:** accept normalized complete FundingSeries, settle signed cashflows
  before orders using archived settlement marks, and reconcile funding separately
  in account, session ledger, run costs and research metrics.
- [x] Test long/short, positive/negative rates, exact boundary and exhausted coverage.
- [ ] Trade-level allocation and funding-adjusted closed-trade performance are
  deferred. Current closed-trade PnL excludes account-level funding by design.
- [ ] Empty-series coverage cannot be independently reconstructed from the current
  engine normalized object; retain source evidence and validate upstream.

## R3 — Historical venue fees

- [x] **BT-301:** consume fee schedule, tier, currency and discount provenance;
  use time-valid evidence per fill and for affordability; expose approximations.
- [ ] Observed maker/taker execution classification needs richer market evidence.
  The current engine bar rule assumes resting limit entries are maker.
- [ ] Cross-currency fee conversion is unsupported and rejected. Do not fold it
  into slippage. Archive conversion evidence and agree a ledger contract first.

## R4 — Venue constraints and liquidation

- [x] **BT-401:** normalize tick/step/minimum/price-band constraints, normalize
  replacements and final risk/volume-limited quantities, reject invalid entries.
- [x] Single-position linear maintenance-margin liquidation from archived marks,
  with funding first, separate liquidation fees, events and reconciliation.
- [ ] Full historical venue filter coverage and native portfolio/isolated/inverse
  margin remain unimplemented. Contract size other than 1 is refused.
- [ ] Engine paper must reapply steps after risk sizing before general R4 parity
  can be claimed. See the release review's exact reproduction.

## R5 — Partial fills, impact and latency proxy

- [x] **BT-501:** cumulative quantity and delta fees, deterministic IDs, partial
  OCO resizing, carry/cancel entry remainders, residual end-of-data valuation.
- [x] One shared bar-volume budget debited by actual fills, lagged calibration,
  and submission/acknowledgement/fill/protection activation timelines.
- [ ] Nonzero cancellation/replacement latency is refused until both runtimes
  support the lifecycle. No simulated millisecond precision from coarse bars.
- [ ] Fix engine residual valuation and pre-risk liquidity reservation, then
  remove the exact used comparator waiver and add wider full-runtime graph tests.
- [ ] Order-book depth, queue priority and real-market impact are unavailable;
  this tier remains an OHLCV proxy permanently unless richer data is supplied.

## Research metrics

- [x] **BT-601:** expectancy, exposure, underwater time, holding time, excursions,
  raw segment inputs, explicit sample size and unavailable values.
- [x] Separate all-fill cost totals from closed-trade ratios; expose signed run
  funding separately and unfinished positions separately from realized trades.
- [x] Correct the old lower-bound claim: bar extremes can occur outside the
  actual holding interval. Partial-size excursions are approximate diagnostics.
- [ ] Funding-adjusted trade ratios, regime definitions, walk-forward/OOS studies
  and benchmark selection need explicit contracts or consumer orchestration.

## Engine handoff before general interchangeability

1. Public graph-account injection, backed by actual broker fills and cashflows.
2. Mark partial residual exposure at candle close before strategy decisions.
3. Re-quantize quantities after risk/participation caps.
4. Consume only actual filled liquidity; preserve unused capacity for protection.
5. Agree terminal policy and test full LiveEngine gap containment across v2 cases.

Engine 0.11 already published canonical market and run identity, v2 fixtures,
soft spot refusal and dynamic target updates. Those earlier requests are closed.
Do not repeat obsolete 0.10 local-artifact claims. New engine changes require
re-running the harness against the newly published distribution.

## Release and completion gates

1. Observe failing behavior tests before changes, then run `./scripts/verify.sh`.
2. Build wheel and sdist; compare them to the tree and run strict Twine validation.
3. Install the wheel in an isolated environment, check dependencies and plugin
   discovery, and execute public fixtures through that installed artifact.
4. Keep both documentation trees current. Preserve SPDX and one-way imports.
5. A human owns commit, push, tag and publication. Application adoption is later.

A passed offline suite establishes the documented simulation contract only.
No tier promises identical Binance/WhiteBIT fills or future returns.
