# Security Policy

## Supported versions

| Version | Supported |
|---|---|
| 0.9.x | yes |
| < 0.9 | no |

## Reporting a vulnerability

Use GitHub's private reporting:
[Report a vulnerability](https://github.com/koval-finance/koval-backtrader/security/advisories/new).
Please do not open a public issue for a security problem.

Expect an acknowledgement within a few days and an assessment within two
weeks. These are best-effort targets from a small maintainer team, not a
contractual SLA. If a fix is warranted you will be credited in the advisory
unless you ask otherwise.

## What is in scope

This package runs a simulation over historical candles. It holds no
credentials and opens no network connection, which rules out most of the usual
categories. What matters here is different:

- **Any code path from this package to a real trading venue.** There must not
  be one. A demonstration that a backtest can reach a live endpoint — directly
  or by influencing the engine's sandbox brokers — is the highest-severity
  report this project accepts.
- **Arbitrary code execution from untrusted input.** Strategy graphs are data.
  A graph, OHLCV file, or execution config that causes code execution when
  passed to `BacktraderBacktestEngine.run()` is a vulnerability.
- **Licence-boundary breaks that mislead downstream users**, such as a change
  that causes MIT-licensed code to link this GPL package without that being
  visible.

## What is not in scope

- Inaccurate backtest results. Execution modelling is fees-only by design and
  documented as such in the README; optimistic results are a known limitation,
  not a vulnerability.
- Vulnerabilities in Backtrader itself. Report those upstream, though telling
  us as well is appreciated if this package's use of it is affected.
