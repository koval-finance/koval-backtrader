"""Packaging guards for the koval-backtrader distribution."""

from __future__ import annotations

import ast
import importlib.util
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


def test_engine_dependency_accepts_the_released_runtime_contract():
    from packaging.requirements import Requirement

    requirement = next(
        Requirement(dep)
        for dep in _pyproject()["project"]["dependencies"]
        if dep.startswith("koval-engine")
    )
    assert "0.11.0" in requirement.specifier
    assert "0.10.0" not in requirement.specifier


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


def _engine_package_root() -> Path:
    spec = importlib.util.find_spec("koval")
    assert spec is not None and spec.submodule_search_locations, "koval-engine must be installed"
    return Path(next(iter(spec.submodule_search_locations)))


def _backtrader_importers(root: Path) -> list[str]:
    return [
        str(path.relative_to(root))
        for path in sorted(root.rglob("*.py"))
        for name in _imported_roots(ast.parse(path.read_text(encoding="utf-8")))
        if name.split(".")[0] == "backtrader"
    ]


def test_the_installed_engine_never_imports_backtrader():
    """The licence boundary, checked from this side of it.

    koval-engine is MIT and must not link GPL code. Its own suite enforces
    this, but the consequence lands here: an engine release that grew a
    Backtrader import would relicense itself the moment it is installed, and
    this package is what puts Backtrader on the path in the first place.
    """
    root = _engine_package_root()
    modules = list(root.rglob("*.py"))

    assert len(modules) >= 20, f"only {len(modules)} engine modules found; the scan is broken"
    assert _backtrader_importers(root) == [], (
        f"koval-engine must not import backtrader: {_backtrader_importers(root)}"
    )


def test_the_backtrader_import_scan_finds_a_real_import():
    """Guards the guard: pointed at this GPL package, the scan must object."""
    assert _backtrader_importers(SRC) != []


def test_application_import_detector_catches_a_violation():
    tree = ast.parse("from koval.db.seeds import seed_system_strategies\nimport api.server\n")
    names = list(_imported_roots(tree))

    assert any(name.startswith(FORBIDDEN_IMPORT_PREFIXES) for name in names)
