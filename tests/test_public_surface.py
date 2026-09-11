"""Paths that must never be published in this repository.

Maintainer-private context lives in ignored directories; a contributor may
keep local copies, but git must never track them. The public agent entry
files (AGENTS.md and its per-tool pointers) are the opposite: they are part
of the published tree and ignore rules must not hide them. This is a test
rather than a habit because a habit cannot fail the build.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN_PATHS = (
    ".private",
    ".agents",
    ".claude",
    "agent_docs",
    "docs/superpowers",
    # The engine owns this import namespace; see agents_docs/invariants.md.
    "src/koval",
)
FORBIDDEN_PUBLIC_DOCUMENTS = (
    "agents_docs/application_integration.md",
    "agents_docs/release_0_11_1.md",
)
REQUIRED_PUBLIC_PATHS = (
    ".cursor/rules/koval-backtrader.mdc",
    ".github/CODEOWNERS",
    ".github/copilot-instructions.md",
    ".github/dependabot.yml",
    ".github/workflows/ci.yml",
    ".github/workflows/mirror.yml",
    ".github/workflows/release.yml",
    ".github/workflows/scorecard.yml",
    "AGENTS.md",
    "CLAUDE.md",
    "GEMINI.md",
    "agents_docs/README.md",
    "scripts/verify.sh",
)


def _is_git_worktree() -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


@pytest.mark.skipif(not _is_git_worktree(), reason="tracked-file guard requires a Git checkout")
def test_no_private_path_is_tracked_by_git():
    tracked = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "--", *FORBIDDEN_PATHS],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert tracked == [], f"private paths must not be tracked: {tracked}"


@pytest.mark.skipif(not _is_git_worktree(), reason="tracked-file guard requires a Git checkout")
def test_no_private_record_is_tracked_as_public_agent_documentation():
    tracked = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "--", *FORBIDDEN_PUBLIC_DOCUMENTS],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert tracked == [], f"private records must not be public agent documentation: {tracked}"


@pytest.mark.skipif(not _is_git_worktree(), reason="tracked-file guard requires a Git checkout")
def test_cursor_directory_tracks_only_rules():
    tracked = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "--", ".cursor"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    strays = [path for path in tracked if not path.startswith(".cursor/rules/")]
    assert strays == [], f"only .cursor/rules/ may be tracked under .cursor/: {strays}"


def test_gitignore_covers_every_forbidden_path():
    ignored = {
        line.strip().rstrip("/")
        for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    agent_paths = [p for p in FORBIDDEN_PATHS if not p.startswith("src/")]
    missing = [p for p in agent_paths if p.rstrip("/") not in ignored]
    assert missing == [], f".gitignore must list: {missing}"


def test_gitignore_does_not_hide_public_repository_automation():
    ignored = {
        line.strip().rstrip("/")
        for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    hidden = [path for path in REQUIRED_PUBLIC_PATHS if path in ignored]
    assert hidden == [], f"public repository automation must not be ignored: {hidden}"


def test_repository_tests_win_over_an_installed_tests_package(tmp_path):
    """Cross-test imports must not depend on namespace-package resolution."""
    foreign_root = tmp_path / "foreign"
    foreign_tests = foreign_root / "tests"
    foreign_tests.mkdir(parents=True)
    (foreign_tests / "__init__.py").write_text("", encoding="utf-8")
    env = os.environ | {"PYTHONPATH": str(foreign_root)}

    imported = subprocess.run(
        [sys.executable, "-c", "import tests.test_runtime_conformance"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert imported.returncode == 0, imported.stdout + imported.stderr


def test_no_module_imports_the_engines_exchange_clients():
    """The venue clients must stay out of a package that never reaches a venue.

    `koval.exchanges` pulls in the Binance and WhiteBIT clients and the HTTP
    stack behind them. Importing it for a helper contradicts the no-live-path
    invariant and makes loading the entry point roughly a second slower.
    """
    offenders = [
        f"{path.name}:{number}: {line.strip()}"
        for path in sorted((ROOT / "src" / "koval_backtrader").glob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if re.search(r"^\s*(import|from)\s+koval\.exchanges\b", line)
    ]
    assert offenders == [], "koval.exchanges imported into the plugin:\n" + "\n".join(offenders)
