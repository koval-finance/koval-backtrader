# SPDX-License-Identifier: GPL-3.0-or-later
"""Actual loaded-package source identity for the additive historical Spot lane."""

from hashlib import sha256
from importlib import metadata
from pathlib import Path

import koval


def source_identity():
    """Distinguish unpublished bytes sharing a distribution version label."""
    packages = {}
    for name, root in (
        ("koval-engine", Path(koval.__file__).parent),
        ("koval-backtrader", Path(__file__).parent),
    ):
        digest = sha256()
        for path in sorted(root.rglob("*")):
            if path.is_file() and (path.suffix in {".py", ".json"} or path.name == "CHECKSUMS.txt"):
                digest.update(
                    path.relative_to(root).as_posix().encode() + b"\0" + path.read_bytes() + b"\0"
                )
        packages[name] = {
            "distribution_metadata_version": metadata.version(name),
            "source_sha256": digest.hexdigest(),
        }
    return packages
