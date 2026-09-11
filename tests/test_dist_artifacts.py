"""Built artifacts must carry the current package bytes and metadata."""

import importlib.util
import io
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def checker():
    spec = importlib.util.spec_from_file_location("check_dist", ROOT / "scripts/check_dist.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_local_distributions_match_the_working_tree():
    assert checker().main(["check_dist"]) == 0


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
@pytest.mark.parametrize("defect", [None, "changed", "missing", "unexpected", "version"])
def test_artifact_gate_checks_content_and_metadata(tmp_path, monkeypatch, kind, defect):
    gate = checker()
    source = tmp_path / "src/koval_backtrader"
    source.mkdir(parents=True)
    (source / "__init__.py").write_text("original\n")
    (tmp_path / "pyproject.toml").write_text('[project]\nversion="0.11.0"\n')
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    monkeypatch.setattr(gate, "PACKAGE", source)
    package = (
        {}
        if defect == "missing"
        else {"__init__.py": b"changed" if defect == "changed" else b"original\n"}
    )
    if defect == "unexpected":
        package["extra.py"] = b"unexpected"
    version = "0.10.0" if defect == "version" else "0.11.0"
    metadata = f"Name: koval-backtrader\nVersion: {version}\n".encode()
    if kind == "wheel":
        artifact = tmp_path / "koval_backtrader-0.11.0-py3-none-any.whl"
        with zipfile.ZipFile(artifact, "w") as archive:
            for name, content in package.items():
                archive.writestr("koval_backtrader/" + name, content)
            archive.writestr("koval_backtrader-0.11.0.dist-info/METADATA", metadata)
    else:
        artifact = tmp_path / "koval_backtrader-0.11.0.tar.gz"
        with tarfile.open(artifact, "w:gz") as archive:
            entries = {"src/koval_backtrader/" + k: v for k, v in package.items()}
            for name, content in (entries | {"PKG-INFO": metadata}).items():
                member = tarfile.TarInfo("koval_backtrader-0.11.0/" + name)
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
    problems = gate.compare(artifact)
    if defect is None:
        assert problems == []
    else:
        assert any(defect in problem for problem in problems), problems
