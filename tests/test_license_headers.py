"""Every source file must carry the GPL identifier.

This package is GPL-3.0 because it links Backtrader. A file that loses its
SPDX header is a file whose licence is ambiguous to every automated scanner
downstream, so the header is a test rather than a convention.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "koval_backtrader"
SPDX = "# SPDX-License-Identifier: GPL-3.0-or-later"
HEADER_LINES = 3


def _has_spdx_header(text: str) -> bool:
    return SPDX in text.splitlines()[:HEADER_LINES]


def test_every_source_file_has_the_gpl_spdx_header():
    offenders = [
        str(path.relative_to(ROOT))
        for path in SRC.rglob("*.py")
        if not _has_spdx_header(path.read_text(encoding="utf-8"))
    ]

    assert offenders == [], f"missing GPL SPDX header: {offenders}"


def test_the_source_tree_is_not_empty():
    """Guards the guard: an empty rglob would pass the test above vacuously."""
    assert len(list(SRC.rglob("*.py"))) >= 5


def test_header_detector_rejects_a_file_without_the_identifier():
    assert not _has_spdx_header('"""No licence here."""\n')
    assert not _has_spdx_header("\n\n\n" + SPDX)  # too far down to count


def test_header_detector_accepts_the_real_thing():
    assert _has_spdx_header(f'{SPDX}\n"""Module."""\n')


def test_gpl_licence_text_is_shipped():
    licence = (ROOT / "LICENSE").read_text(encoding="utf-8")

    assert "GNU GENERAL PUBLIC LICENSE" in licence
    assert "Version 3, 29 June 2007" in licence
