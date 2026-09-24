"""The acceptance runner must identify installed wheel bytes, never sibling imports."""

import importlib.util
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]


def pair_checker():
    path = ROOT / "scripts/verify_pair.py"
    assert path.is_file(), "a reproducible installed-pair acceptance command is required"
    spec = importlib.util.spec_from_file_location("verify_pair", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("defect", [None, "changed", "missing", "extra", "outside_environment"])
def test_loaded_package_must_match_wheel_bytes_inside_the_environment(tmp_path, defect):
    check = pair_checker()
    prefix = tmp_path / "venv"
    package = (
        tmp_path / "sibling" if defect == "outside_environment" else prefix / "site-packages"
    ) / "example"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("original\n" if defect != "changed" else "changed\n")
    if defect != "missing":
        (package / "module.py").write_text("module\n")
    if defect == "extra":
        (package / "extra.py").write_text("extra\n")
    wheel = tmp_path / "example-1.0.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("example/__init__.py", "original\n")
        archive.writestr("example/module.py", "module\n")
    if defect is None:
        assert check.check_installed_package(wheel, package, prefix)["sha256"]
    else:
        with pytest.raises(ValueError, match="installed|environment"):
            check.check_installed_package(wheel, package, prefix)


def test_dependency_range_requires_the_engine_futures_contract():
    import tomllib

    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    requirement = next(
        Requirement(d) for d in config["project"]["dependencies"] if d.startswith("koval-engine")
    )
    assert "0.12.1" not in requirement.specifier
    assert "0.13.0" in requirement.specifier
    assert "0.14.0" not in requirement.specifier


def test_verify_can_run_the_full_gate_with_an_isolated_interpreter():
    assert "KOVAL_VERIFY_PYTHON" in (ROOT / "scripts/verify.sh").read_text()


@pytest.mark.parametrize("missing_wheel", [False, True])
def test_failed_pair_attempt_does_not_leave_a_previous_passing_manifest(tmp_path, missing_wheel):
    wheel = tmp_path / "placeholder.whl"
    if not missing_wheel:
        wheel.write_bytes(b"placeholder")
    report = tmp_path / "acceptance.json"
    report.write_text(json.dumps({"gates": {"verify": "passed"}}))
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/verify_pair.py"),
            "--engine",
            str(wheel),
            "--plugin",
            str(wheel),
            "--python",
            str(tmp_path / "missing-python"),
            "--output",
            str(report),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert json.loads(report.read_text())["gates"]["verify"] != "passed"
