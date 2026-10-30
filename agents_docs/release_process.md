# Release process

## Version truth

The version lives in exactly one place: `[project].version` in
`pyproject.toml`. `koval_backtrader.__version__` reads it back from installed
distribution metadata, so there is no second copy to drift. There is no
`VERSION` file.

## Compatibility with the engine

The dependency is a range — currently `koval-engine>=0.12.7,<0.13.0` — never
an exact pin. A plugin that hard-pins one engine patch version forces every
downstream user into lockstep upgrades.

The range's upper bound is a real statement: it says this adapter speaks the
engine's current `ENGINE_PROTOCOL_VERSION`. When the engine bumps that
constant, `check_protocol_version()` starts rejecting specs, and the correct
response is a new release of this package with a widened range — not a
loosened bound hoping for the best.

Adapter 0.12.5 requires the engine 0.12.7 native Spot and numeric contracts,
including the earlier 0.12.6 chronology and midnight-risk corrections.
Older engines are outside this release's dependency range; users
reproducing old results retain their exact earlier engine and adapter pins.
Normal CI and release CI both verify engine 0.12.7 and 0.12.8 pairs on Python
3.11/3.13, in addition to the normal 3.11/3.12/3.13 suite. No parity waiver,
tolerance or skip substitutes for corrected-pair acceptance. See the
[current validation scope](../docs/execution-validation.md#spot-chronology-correction).

Both engine versions are published. Download and verify their exact PyPI wheels
before adapter acceptance: both workflows install from PyPI. Never patch a
sibling virtualenv or treat a source override as installed-pair release evidence.

## Cutting a release

Native Grid/DCA uses the published engine 0.12.7 contract; follow the exact-wheel
[Spot release prerequisites](spot_multi_order.md) before publishing the adapter.
The local 0.12.5 candidate and its passing acceptance do not establish publication.
The [0.12.5 guide](../docs/release-0.12.5.md) owns this patch's manual sequence
and downstream software-identity migration rules.

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
and only then creates the GitHub release. Publication also requires the
installed-pair job to pass against the exact built wheel and published engines.

Publishing is irreversible: a filename on PyPI can never be reused, even after
deletion. The gates run before the upload for that reason.

## Local release validation

Build and development environments require `setuptools>=77.0.3` for the
SPDX `project.license` expression and `project.license-files` metadata. Older
backends cannot parse this format; do not lower the requirement to reuse an
older environment. Both dependency declarations are guarded by
`tests/test_package_metadata.py`.

Build with `python -m build`. Every local wheel and sdist must match current
package bytes and version metadata; the default suite rejects stale artifacts.
The sdist explicitly prunes ignored local planning files; the archive test
plants a sentinel to prove these files cannot travel with public documentation.
Then run `python scripts/check_dist.py --require-artifacts`,
`python -m twine check --strict dist/*`, and `./scripts/verify.sh`.
Install the wheel into a fresh environment outside the checkout, run `pip check`
and verify discovery plus the published engine fixtures from that environment.
Rebuild after changing any package source. Keep artifacts local; the tag workflow
builds and publishes independently from the human's committed tree.

For 0.11.1, publish engine 0.11.1 first and verify its PyPI availability before
pushing the plugin tag so the release gate resolves the corrected engine pair.
The declared floor remains 0.11.0, with its prior graph fallback; complete
0.11.1 parity acceptance requires engine 0.11.1. Candidate wheel acceptance
is recorded in [../docs/execution-validation.md](../docs/execution-validation.md).
Never convert passing build checks into an exchange-fidelity claim.

For 0.12.1, publish engine 0.12.1 first: the exact-pair CI matrix downloads
0.11.1, 0.12.0 and 0.12.1 wheels and verifies the built plugin on Python
3.11/3.13.
Use [../scripts/verify_pair.py](../scripts/verify_pair.py) with the exact candidate
wheel before publication; it invokes the complete gate using
`KOVAL_VERIFY_PYTHON`, checks installed bytes and emits a hash manifest.
Protocol 2 runtime boundaries are optional; the old default path remains
available on 0.11.1, while 0.12.0 remains in the matrix to protect patch-level
compatibility. The historical 0.11.0 fallback is not a full parity claim.
The measured matrix belongs in
[../docs/execution-validation.md](../docs/execution-validation.md).

## Update this file when

The release pipeline changes shape, or the engine compatibility policy
changes.

## 0.12.2 position management release

Engine 0.12.4 is published and provides the historical exit request, cached
strategy hooks and explicit take-profit mode. Use its PyPI wheel, never the
same-version development artifact. Record PyPI hashes and verify both PEP 740
attestations against `koval-finance/koval-engine` before adapter preparation.
The declared range stays `koval-engine>=0.11.0,<0.13.0`; new capabilities require
the actual new contracts and a supported Binance Spot v2 configuration. Older
entry graphs remain supported, with explicit refusal of new capabilities.

CI and tag release verification retain engine 0.11.1/0.12.0/0.12.1 and add
0.12.2/0.12.4 on Python 3.11/3.13. The tag workflow tests the downloaded build
artifacts before publication; it does not rebuild a different wheel for the
pair gate. Its publish job depends on both build and installed-pair success.
The normal Python 3.11/3.12/3.13 gate remains required. Configured CI coverage
is not evidence that those runtimes were tested locally. The 0.11.0 fallback
remains available without claiming full cross-runtime parity. Record actual
local coverage in [the validation record](../docs/execution-validation.md).
