# Release process

## Version truth

The version lives in exactly one place: `[project].version` in
`pyproject.toml`. `koval_backtrader.__version__` reads it back from installed
distribution metadata, so there is no second copy to drift. There is no
`VERSION` file.

## Compatibility with the engine

The dependency is a range — currently `koval-engine>=0.11.0,<0.12.0` — never
an exact pin. A plugin that hard-pins one engine patch version forces every
downstream user into lockstep upgrades.

The range's upper bound is a real statement: it says this adapter speaks the
engine's current `ENGINE_PROTOCOL_VERSION`. When the engine bumps that
constant, `check_protocol_version()` starts rejecting specs, and the correct
response is a new release of this package with a widened range — not a
loosened bound hoping for the best.

## Cutting a release

1. Update `[project].version`.
2. Add a `## [x.y.z]` section to `CHANGELOG.md`. The release workflow extracts
   the notes from it and **fails if the section is missing**, so this is a
   gate, not a courtesy.
3. Confirm `./scripts/verify.sh` exits 0.
4. Confirm every version this release's range requires is already published.
   The dependency range is resolved by pip at install time, so publishing a
   release that requires an unpublished engine version makes the package
   uninstallable for everyone until the engine catches up. Check PyPI, not the
   local environment: a sibling working tree can satisfy the range locally
   while nothing satisfies it publicly.
5. The owner pushes a `v*` tag.

The tag triggers `release.yml`, which verifies on three Python versions,
checks that the tag matches the packaged version, extracts the changelog
entry, builds, compares package bytes and version metadata with
`scripts/check_dist.py --require-artifacts`, runs `twine check --strict`, publishes to PyPI through Trusted
Publishing (OIDC — no long-lived PyPI credential exists in this repository),
and only then creates the GitHub release.

Publishing is irreversible: a filename on PyPI can never be reused, even after
deletion. The gates run before the upload for that reason.

## Local release validation

Build with `python -m build`. Every local wheel and sdist must match current
package bytes and version metadata; the default suite rejects stale artifacts.
Then run `python scripts/check_dist.py --require-artifacts`,
`python -m twine check --strict dist/*`, and `./scripts/verify.sh`.
Install the wheel into a fresh environment outside the checkout, run `pip check`
and verify discovery plus the published engine fixtures from that environment.
Rebuild after changing any package source. Keep artifacts local; the tag workflow
builds and publishes independently from the human's committed tree.

Engine 0.11.0 is publicly available. The 0.11 review documents advanced parity
limits; never convert passing build checks into an exchange-fidelity claim.

## Update this file when

The release pipeline changes shape, or the engine compatibility policy
changes.
