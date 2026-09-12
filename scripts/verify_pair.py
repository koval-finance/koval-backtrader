#!/usr/bin/env python3
"""Verify two exact wheels in a fresh environment and retain an acceptance manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
import zipfile
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def check_installed_package(wheel: Path, package: Path, prefix: Path) -> dict:
    if not package.resolve().is_relative_to(prefix.resolve()):
        raise ValueError(f"loaded package is outside the isolated environment: {package}")
    with zipfile.ZipFile(wheel) as archive:
        expected = {
            name: archive.read(name)
            for name in archive.namelist()
            if name.startswith(package.name + "/") and not name.endswith("/")
        }
    installed = {
        package.name + "/" + path.relative_to(package).as_posix(): path.read_bytes()
        for path in package.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }
    if not expected or expected != installed:
        raise ValueError(f"installed package bytes differ from wheel: {wheel.name}")
    return {"wheel": wheel.name, "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()}


def probe(engine: Path, plugin: Path) -> dict:
    import koval
    from koval.engine.backtest_engine import load_backtest_engine

    import koval_backtrader

    loaded = load_backtest_engine()
    if type(loaded).__module__ != "koval_backtrader.backtest_runner":
        raise ValueError("plugin discovery loaded an unexpected implementation")
    packages = {}
    for name, wheel, module in (
        ("koval-engine", engine, koval),
        ("koval-backtrader", plugin, koval_backtrader),
    ):
        packages[name] = check_installed_package(
            wheel, Path(module.__file__).parent, Path(sys.prefix)
        )
        packages[name]["version"] = metadata.version(name)
    return {
        "schema_version": "koval_installed_pair_acceptance_v1",
        "packages": packages,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "dependencies": dict(
            sorted((d.metadata["Name"], d.version) for d in metadata.distributions())
        ),
        "entry_point": "koval_backtrader.backtest_runner:create_engine",
        "scope": "offline_simulation_conformance",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument("--plugin", type=Path, required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--probe", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps({"gates": {"verify": "not_run"}, "status": "preparing_environment"}) + "\n"
    )
    engine, plugin = args.engine.resolve(strict=True), args.plugin.resolve(strict=True)
    if args.probe:
        output.write_text(json.dumps(probe(engine, plugin), indent=2) + "\n")
        return 0
    with tempfile.TemporaryDirectory(prefix="koval-pair-") as temporary:
        directory = Path(temporary)
        environment = directory / "venv"
        subprocess.run([args.python, "-m", "venv", str(environment)], check=True)
        python = str(environment / "bin/python")
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in {"PYTHONPATH", "PYTHONHOME", "KOVAL_BACKTEST_ENGINE"}
        }
        env["KOVAL_VERIFY_PYTHON"] = python
        subprocess.run(
            [python, "-m", "pip", "install", str(engine), str(plugin) + "[dev]"],
            cwd=directory,
            env=env,
            check=True,
        )
        subprocess.run([python, "-m", "pip", "check"], cwd=directory, env=env, check=True)
        subprocess.run(
            [
                python,
                "-I",
                str(Path(__file__).resolve()),
                "--probe",
                "--engine",
                str(engine),
                "--plugin",
                str(plugin),
                "--output",
                str(output),
            ],
            cwd=directory,
            env=env,
            check=True,
        )
        manifest = json.loads(output.read_text())
        digest = hashlib.sha256()
        sources = [
            ROOT / "scripts/verify.sh",
            Path(__file__).resolve(),
            *sorted((ROOT / "tests").glob("*.py")),
        ]
        for source in sources:
            digest.update(
                source.relative_to(ROOT).as_posix().encode() + b"\0" + source.read_bytes() + b"\0"
            )
        manifest["verification_source_sha256"] = digest.hexdigest()
        manifest["gates"] = {
            "pip_check": "passed",
            "discovery_and_wheel_bytes": "passed",
            "verify": "running",
        }
        output.write_text(json.dumps(manifest, indent=2) + "\n")
        completed = subprocess.run(
            [str(ROOT / "scripts/verify.sh")], cwd=ROOT, env=env, check=False
        )
        manifest["gates"]["verify"] = "passed" if completed.returncode == 0 else "failed"
        output.write_text(json.dumps(manifest, indent=2) + "\n")
        return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
