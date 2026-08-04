"""The plugin seam: koval-engine must find this engine on its own.

Everything else in this suite tests the adapter's behaviour. This file tests
the one thing a user hits first — that installing the package is sufficient,
with no environment variable, no import, and no configuration. If this fails,
`koval backtest` fails for everyone even though every other test is green.
"""

from __future__ import annotations

import inspect
import tomllib
from importlib import metadata
from pathlib import Path

import pytest
from koval.engine.backtest_engine import BacktestEngineProtocol, load_backtest_engine

from koval_backtrader.backtest_runner import BacktraderBacktestEngine

ROOT = Path(__file__).resolve().parent.parent
ENTRY_POINT_GROUP = "koval.backtest_engines"
ENTRY_POINT_NAME = "backtrader"


@pytest.fixture(autouse=True)
def _no_engine_override(monkeypatch):
    """The env override must not be what makes these tests pass."""
    monkeypatch.delenv("KOVAL_BACKTEST_ENGINE", raising=False)


def test_installed_package_registers_the_backtrader_entry_point():
    names = {entry.name for entry in metadata.entry_points(group=ENTRY_POINT_GROUP)}

    assert ENTRY_POINT_NAME in names, (
        f"group {ENTRY_POINT_GROUP!r} has {sorted(names)}; reinstall the package if this is stale"
    )


def test_engine_resolves_this_adapter_by_default():
    engine = load_backtest_engine()

    assert isinstance(engine, BacktraderBacktestEngine)


def test_engine_resolves_this_adapter_when_asked_for_by_name():
    engine = load_backtest_engine(ENTRY_POINT_NAME)

    assert isinstance(engine, BacktraderBacktestEngine)


def test_resolved_engine_satisfies_the_published_protocol():
    """`BacktestEngineProtocol` is not runtime-checkable, so match it structurally.

    Widening the engine's protocol just to make an `isinstance` call work here
    would change published MIT API to suit a GPL test. The signature is the
    contract; check that instead.
    """
    engine = load_backtest_engine()
    expected = inspect.signature(BacktestEngineProtocol.run)
    actual = inspect.signature(type(engine).run)

    assert list(actual.parameters) == list(expected.parameters)
    assert actual.return_annotation == expected.return_annotation


def test_declared_entry_point_matches_the_installed_one():
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "entry-points"
    ][ENTRY_POINT_GROUP][ENTRY_POINT_NAME]
    installed = next(
        entry
        for entry in metadata.entry_points(group=ENTRY_POINT_GROUP)
        if entry.name == ENTRY_POINT_NAME
    )

    assert declared == installed.value


def test_module_path_override_also_resolves_this_engine(monkeypatch):
    """`KOVAL_BACKTEST_ENGINE` takes a module path and calls `create_engine`."""
    monkeypatch.setenv("KOVAL_BACKTEST_ENGINE", "koval_backtrader.backtest_runner")

    assert isinstance(load_backtest_engine(), BacktraderBacktestEngine)
