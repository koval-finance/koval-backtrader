"""Release workflow guards for the irreversible PyPI publication path.

A filename on PyPI can never be reused, even after deletion. Every gate has
to run before the upload, and the ordering below is the only thing that
guarantees it.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
MIRROR_WORKFLOW = ROOT / ".github" / "workflows" / "mirror.yml"
SCORECARD_WORKFLOW = ROOT / ".github" / "workflows" / "scorecard.yml"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
CHANGELOG = ROOT / "CHANGELOG.md"
README = ROOT / "README.md"


def _project_version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_github_workflows_are_valid_yaml():
    workflows = ROOT / ".github" / "workflows"
    paths = sorted((*workflows.glob("*.yml"), *workflows.glob("*.yaml")))
    for path in paths:
        try:
            yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as error:
            pytest.fail(f"invalid workflow YAML in {path.relative_to(ROOT)}: {error}")


def test_release_build_runs_all_quality_gates_before_artifact_upload():
    workflow = _workflow_text()
    upload = workflow.index("actions/upload-artifact@")

    required_before_upload = (
        "ruff check .",
        "ruff format --check .",
        "pytest -q",
        "python -m build",
        "python scripts/check_dist.py --require-artifacts",
        "twine check --strict dist/*",
    )
    missing = [command for command in required_before_upload if command not in workflow[:upload]]
    assert missing == [], f"release artifact is uploaded before these gates run: {missing}"


def test_tag_must_match_the_packaged_version():
    workflow = _workflow_text()
    check = workflow.index("does not match pyproject version")
    build = workflow.index("python -m build")

    assert check < build


def test_changelog_is_validated_before_pypi_publish():
    workflow = _workflow_text()
    changelog_validation = workflow.index("No changelog entry found")
    publish = workflow.index("pypa/gh-action-pypi-publish@")

    assert changelog_validation < publish


def test_release_docs_match_the_project_version():
    version = _project_version()
    minor_series = version.rsplit(".", 1)[0]
    changelog = CHANGELOG.read_text(encoding="utf-8")
    readme = README.read_text(encoding="utf-8")

    assert re.search(
        rf"^## \[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}}$",
        changelog,
        re.MULTILINE,
    )
    assert (
        f"[Unreleased]: https://github.com/koval-finance/koval-backtrader/compare/v{version}...HEAD"
    ) in changelog
    assert (f"[{version}]: https://github.com/koval-finance/koval-backtrader/compare/") in changelog
    assert f"**Status:** {minor_series}.x." in readme


def test_installed_pair_ci_covers_the_0121_engine_release():
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    versions = workflow["jobs"]["installed-pair"]["strategy"]["matrix"]["engine-version"]

    assert versions == ["0.11.1", "0.12.0", "0.12.1"]


def test_pypi_publish_precedes_the_public_github_release():
    workflow = _workflow_text()

    assert "build:\n    needs: verify" in workflow
    assert "publish:\n    needs: build" in workflow
    assert "github-release:\n    needs: publish" in workflow


def test_publish_uses_trusted_publishing_rather_than_a_stored_token():
    workflow = _workflow_text()

    assert "id-token: write" in workflow
    assert "no long-lived PyPI token is used" in workflow
    assert "PYPI_API_TOKEN" not in workflow
    assert "password:" not in workflow


def test_every_action_is_pinned_to_a_commit_sha():
    unpinned = []
    for path in (WORKFLOW, MIRROR_WORKFLOW, SCORECARD_WORKFLOW, ROOT / ".github/workflows/ci.yml"):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if not stripped.startswith("- uses:"):
                continue
            reference = stripped.split("@", 1)[1].split()[0] if "@" in stripped else ""
            if len(reference) != 40 or not all(c in "0123456789abcdef" for c in reference):
                unpinned.append(f"{path.name}:{number}: {stripped}")

    assert unpinned == [], f"actions must be pinned to a full commit SHA: {unpinned}"


def test_scorecard_job_can_read_repository_contents():
    workflow = SCORECARD_WORKFLOW.read_text(encoding="utf-8")

    assert (
        "    permissions:\n"
        "      contents: read\n"
        "      security-events: write\n"
        "      id-token: write"
    ) in workflow


def test_tag_triggered_mirror_pushes_the_fetched_main_ref():
    workflow = MIRROR_WORKFLOW.read_text(encoding="utf-8")

    assert "refs/remotes/origin/main:refs/heads/main" in workflow
    assert "git push gitlab main --tags" not in workflow


def test_mirror_targets_this_repository():
    workflow = MIRROR_WORKFLOW.read_text(encoding="utf-8")

    assert "koval-backtrader.git" in workflow
    assert "koval-engine.git" not in workflow
