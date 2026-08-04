"""Packaging guards for the koval-backtrader distribution."""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "koval_backtrader"
# Application-only namespaces. The engine is MIT and shared; the app is not.
FORBIDDEN_IMPORT_PREFIXES = ("api", "koval.db")


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_distribution_ships_the_py_typed_marker():
    package_data = _pyproject()["tool"]["setuptools"]["package-data"]

    assert "py.typed" in package_data["koval_backtrader"]
    assert (SRC / "py.typed").is_file()


def test_only_the_koval_backtrader_package_is_published():
    """A `koval` package here would collide with the engine's own `koval`.

    The engine ships `koval/__init__.py`, making `koval` a regular package. Two
    distributions claiming it would shadow each other depending on path order —
    the kind of bug that produces an ImportError on one machine and silence on
    another.
    """
    found = _pyproject()["tool"]["setuptools"]["packages"]["find"]

    assert found["where"] == ["src"]
    assert found["include"] == ["koval_backtrader*"]
    assert not (ROOT / "src" / "koval").exists()


def test_the_engine_dependency_is_a_range_not_an_exact_pin():
    """A plugin that hard-pins one engine patch version is unusable downstream."""
    engine = next(
        dep for dep in _pyproject()["project"]["dependencies"] if dep.startswith("koval-engine")
    )

    assert "==" not in engine
    assert ">=" in engine and "<" in engine


def test_backtrader_is_a_declared_runtime_dependency():
    dependencies = _pyproject()["project"]["dependencies"]

    assert any(dep.startswith("backtrader") for dep in dependencies)


def test_the_licence_is_declared_as_gpl():
    project = _pyproject()["project"]

    assert project["license"] == "GPL-3.0-or-later"
    assert project["license-files"] == ["LICENSE"]


def _imported_roots(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module


def test_no_source_file_imports_application_code():
    """The dependency arrow points at the engine only, never back at the app."""
    offenders = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for name in _imported_roots(tree):
            if name == "api" or name.startswith(FORBIDDEN_IMPORT_PREFIXES):
                offenders.append(f"{path.relative_to(ROOT)}: {name}")

    assert offenders == [], f"adapter must not import application code: {offenders}"


def test_application_import_detector_catches_a_violation():
    tree = ast.parse("from koval.db.seeds import seed_system_strategies\nimport api.server\n")
    names = list(_imported_roots(tree))

    assert any(name.startswith(FORBIDDEN_IMPORT_PREFIXES) for name in names)
