# Founder Spot Beta technical review

Review date: 2026-10-30. Baseline: clean checkout at
`2705db877233e88938b22a57cd825e4fe68cf503`, adapter 0.12.4, installed engine
0.12.8, Backtrader 1.9.78.123, Python 3.13.5, NumPy 2.5.3 and pandas 3.0.5.
This report preserves the pre-release review of a local 0.12.4 increment; its
artifact hashes and measurements below belong to that snapshot. The fixes are
now prepared for [release 0.12.5](release-0.12.5.md), whose separate acceptance
record identifies the new candidate. Preparation does not establish publication.

## Verdict and scope

The architecture is suitable for the historical research part of Founder Spot
Beta. The review found one execution failure, one native admission mismatch,
one build requirement mismatch and two documentation/API-example defects;
all are addressed below. No replacement architecture or broad refactor is
required for this scope.

This package is an offline simulator. Engine-owned paper evaluation, application
candidate management, exchange gateways, Testnet, production execution and
operational acceptance remain separate responsibilities. A passing adapter gate
does not establish readiness of the complete application or authorize orders.

The review covered every source module, the public entry point and run protocol,
execution profiles, evidence and identity, graph integration, input causality,
cash/fees/inventory, partial and OCO lifecycle, terminal handling, native Spot
checkpoint recovery, metrics, packaging, CI and both documentation trees.
Normal application execution-config construction and historical/paper contract
tests were inspected and exercised against the working-tree adapter. No sibling
source, persisted application data or historical acceptance seal was changed.

## Findings and fixes

### F1 — Important: residual protection replacement could abort a valid run

A partial protective or signal close can leave inventory smaller than the
instrument's minimum quantity/notional for a new order. The existing protective
order can still cover that residual, but a subsequent trailing/target update
called instrument normalization and propagated its `ValueError`, aborting the
whole backtest.

`bt_adapter.py` now validates the strategy instruction, attempts normalization,
and treats an instrument refusal as a rejected replacement. It returns before
canceling the existing stop/OCO pair or changing the account stop, carried-entry
protection or stop history. Successfully normalized replacements still receive
the final validation. Malformed, nonfinite and risk-widening strategy instructions
continue to raise.

The new `ORDER_REJECTED` event has `operation="protection_replace"`, the working
`protection_order_id`, direction, residual size, requested stop/target and an
`instrument_constraint` reason. It does not mark a successful entry intent as
rejected. [The event contract](results.md#events) documents the payload.

Eight regressions in `test_protection_replacement.py` cover minimum quantity and
notional with bracket/disabled targets, retained orders/account/history, a carried
entry remainder, and invalid strategy instructions. Four original residual-filter
cases were observed failing before implementation. The hand-calculated control
keeps the original stop at 80: fills of 0.6 and 0.4 close one unit for a 20 quote
loss; the refused stop at 90 never changes the fills or final balance of 9980.

### F2 — Moderate: native Spot accepted the wrong outer protocol

The native historical Spot documentation requires protocol 2 and execution
contract 2. The shared engine validator intentionally accepts protocol 1 for
legacy graphs, so native Grid/DCA could also run with outer protocol 1.

`spot_runner.py` now explicitly refuses that native request with
`ProtocolVersionError` before negotiation, session construction or journal
output. A normal-loader regression was observed failing before the guard and
passes afterward. Existing legacy runs retain protocol 1; a separate real-loader
probe confirmed equal trades, equity and metrics under protocols 1 and 2.
Current application native requests already use protocol 2.

### F3 — Moderate: the build backend floor did not support package metadata

Both build and development requirements allowed `setuptools>=68`, while the
project uses a string SPDX license expression and `project.license-files`.
Setuptools introduced that metadata support in version 77, as documented in
[its configuration reference](https://setuptools.pypa.io/en/latest/userguide/pyproject_config.html).
An otherwise requirement-compliant older backend could therefore refuse the
project metadata.

Both declarations now require `setuptools>=77.0.3`. Four dependency-admission
guards were observed failing first and now reject 68.0.0/76.1.0 while admitting
77.0.3. Local build verification uses the installed backend 83.0.0. An attempted
separate 76.1.0 metadata probe could not download that backend because PyPI DNS
resolution failed, including on the approved retry; no claim is made that the
old-backend build was executed. The metadata incompatibility is supported by
the primary documentation and the former declared requirement.

### F4 — Minor: identity documentation named an absent output field

`results.md` and `troubleshooting.md` directed callers to `comparability`, which
the actual canonical identity does not emit. They now name
`reproducibility_grade` and its `identified_simulation` / `not_comparable` values.
The separate legacy `reproducibility.level="partial"` field retains its existing
meaning. No identity encoding or old-result compatibility was changed.

### F5 — Minor: custom-engine documentation omitted execution negotiation

The run-spec table omitted execution contract version, required capabilities
and runtime boundaries. Its illustrative custom engine also checked the outer
protocol without refusing execution assumptions it did not implement.

The table now describes every current `EngineRunSpec` field. The example
negotiates its empty capability set and contract 1, and refuses explicit
execution-model/evidence settings. Direct probes confirmed the example still
runs its documented illustration and rejects contract 2, required
`same_bar_protection` and explicit execution models. Documentation/examples
tests pass. This changes the example, not the adapter's runtime API.

## Architecture, contracts and functionality assessment

| Area | Assessment and practical implication |
| --- | --- |
| Package boundary | Engine contracts/evaluation remain MIT-owned; this top-level GPL plugin imports no application or venue clients and does not claim the `koval` namespace. Keep this separation. |
| Backtrader coupling | Private broker hooks and the process-wide idempotent OCO patch are deliberate coupling points. The reviewed Backtrader 1.9.78.123 is exercised by the suite; a future dependency upgrade still requires full execution and package verification. |
| Public API | The normal loader returns `BacktraderBacktestEngine`; `EngineRunSpec` and `BacktestResult` remain plain/picklable. Protocol/capability refusal happens before supported execution resources are created. Native Spot now enforces its additional protocol requirement. |
| Responsibility split | Graph semantics and shared accounting contracts belong to the engine. The plugin owns historical matching, Backtrader translation, execution evidence and result extraction. Application persistence, jobs, HTTP/MCP APIs and venue transport stay above this boundary. |
| Spot cash | Canonical Spot enforces long-only and leverage one. Available purchase capacity is unspent quote cash; unrealized gains cannot fund another purchase. Normal app v1/v2 config construction supplies canonical market identity. |
| Input chronology | Costed feeds validate finite OHLCV, integer increasing clocks and price bounds. V2 validates aligned contiguous streams; numeric admission also checks every primary timestamp and the fixed history budget. Runtime boundaries constrain warmup/evaluation. |
| Causality | Entries follow closed-bar decisions; higher-timeframe arrays expose only completed bars. Existing future-suffix mutation tests, numeric tests and native prefix probes cover the declared availability model. Indicator preparation preserves scalar results through the rolling-history boundary. |
| Protection/lifecycle | Opening stop/target priority, ambiguous intrabar policy, partial inventory, shared liquidity, entry-remainder cancellation and signal closes have explicit tests. F1 closes the residual-filter replacement failure without relaxing instrument rules. |
| Accounting/evidence | Fills, fees, cashflow ledger and terminal positions reconcile. Research metrics use persisted trade/fill/account data. Legacy `gross_realized_pnl` is a documented net-of-commission alias; consumers should use precise price/net fields. |
| Native Grid/DCA | A separate Decimal matcher delegates recipe decisions/reservations/accounting to the public engine. All 16 public fixtures are exercised, including candle/paper comparisons. Native results intentionally use `spot_execution_v1` lots/baskets rather than inventing bracket trades. |
| Recovery/terminal behavior | Native checkpoints bind graph, instrument, policy and software identities; incomplete boundaries refuse recovery. Terminal retain/flatten policies are explicit. Numeric graph state restore is not broker/order resume. |
| Audit/identity | Decisions, orders, fills and trades have run-local links. Loaded source fingerprints distinguish local modifications from version labels. Canonical comparability and legacy provenance grades remain separate. |
| Code design | Focused execution/evidence/identity modules support incremental changes. The large strategy bridge and realistic broker remain maintenance hotspots, but no verified defect justifies a broad rewrite in this increment. |
| Packaging/CI | Namespace, licensing, entry-point, source archive and artifact-byte guards remain intact. CI declares Python 3.11/3.12/3.13, with exact engine 0.12.7/0.12.8 pair jobs on 3.11/3.13. F3 corrects the build metadata floor. |

## Readiness limits and follow-up ownership

- **Historical Founder Spot research:** use explicit canonical Spot identity and
  versioned costs, preserve input archives and exact software identity, and
  distinguish a retained open holding from a completed trade. The reviewed app
  path supplies that identity; legacy label-only requests intentionally remain
  unconstrained for replay and must not be treated as a Spot safety contract.
- **Numeric values and historical signal exits:** availability is capability-
  and profile-specific. The public numeric runner supports one timeframe and a
  1000-bar history budget. Numeric broker recovery and paper/venue support are
  not implied by historical acceptance.
- **Native Grid/DCA:** one bound market/timeframe, bounded recipe, explicit
  evidence and terminal policy. Growing reducer state and checksummed snapshots
  limit long-history scalability; this was already documented and remains an
  engineering limit. No new performance guarantee is inferred from fixture tests.
- **Simulation realism:** OHLCV paths, intrabar ambiguity and volume participation
  are declared models. Queue priority, order-book depth and actual exchange
  accuracy are not established by deterministic parity tests.
- **Application/release:** these source changes require a human release and
  subsequent application adoption. The app's recorded production composition
  still has exact older-adapter acceptance locks; the earlier adoption record
  reports non-green production tests. Those locks were not edited or reissued
  here, and the complete app suite was not rerun. Application production
  acceptance remains a separate open gate.
- **Operations:** no venue request, keys, deployment, session Start, release or
  Git history mutation occurred. Continuous paper observation, Testnet and
  separately authorized production acceptance remain outside adapter verification.

## Validation record

The starting full gate passed: **1161 tests**, lint and formatting, in 86.05 s.
New behavior was tested before implementation, as described in F1–F3.

Final `./scripts/verify.sh`: **PASS, exit 0**, including Ruff lint, formatting
and **1174 passed in 98.94 s**, with no failures or skips. Thirteen regression
cases were added to the baseline's 1161 tests. The complete log is retained in
the local review evidence directory. Only Python 3.13 was executed locally in
this review; configured CI coverage is not a claim that other Python versions
were run here.

Additional completed checks:

- Independent final review: no important findings; 96 focused tests passed.
- Wheel and sdist build: passed with backend 83.0.0 and `--no-isolation`.
- `scripts/check_dist.py --require-artifacts`, `twine check --strict` and
  `pip check`: passed.
- Application historical execution, paper contracts, numeric API and native
  recipe API/archiving/replay: **130 passed**, with numeric availability required
  and `--runxfail`. These tests loaded the candidate wheel from a separate
  directory using existing app dependencies; no app installation was replaced.
- Candidate plugin package bytes matched its wheel, and the loaded app engine
  bytes matched the preserved engine 0.12.8 wheel. This is a built-wheel
  integration check, not a newly resolved isolated dependency environment.
- Candidate wheel SHA-256:
  `29ac9f0f09e490c09e6838829da786584670ece95e85c1d89c0a33b9a04e3360`.
- Engine wheel SHA-256:
  `d37968eefe25e97837728e1ccfe3e2fa05e6ffade85b48e8d54b7c4ded1517df`.

Two intermediate validation failures were corrected without weakening guards:
the first combined adapter gate had 1173 passes and one failure in
`test_the_readme_sends_readers_to_the_documentation` because the new report
lacked a root README link. That link was added, and all 19 docs/examples tests
then passed. The first expanded app run used a source override; editable
distribution metadata hid wheel-relative native recipe files, producing 122
passes and eight recipe-admission failures. Repeating the same suite from the
actual rebuilt wheel produced the 130 passes above. No application admission
check was mocked or bypassed to obtain that result.

Suggested human commit message:
`Fix Founder Spot protection replacement and tighten historical contracts`.
