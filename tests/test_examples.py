"""The shipped example must be the example that runs.

A README or an examples directory that has drifted from the code is worse than
none: it costs a new user their first half hour and teaches them not to trust
the docs. So the example is executed here rather than eyeballed.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "run_backtest.py"
README = ROOT / "README.md"


def test_the_bundled_example_runs_and_reports_metrics():
    result = subprocess.run(
        [sys.executable, str(EXAMPLE)],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "total_trades" in result.stdout
    assert "closed_trades" in result.stdout


def test_the_example_does_not_import_this_package_by_name():
    """Consumer code talks to the engine; the adapter arrives via the entry point."""
    source = EXAMPLE.read_text(encoding="utf-8")

    assert "import koval_backtrader" not in source
    assert "from koval_backtrader" not in source


def _readme_koval_commands() -> list[str]:
    return re.findall(r"^\s*\$?\s*(koval .+)$", README.read_text(encoding="utf-8"), re.MULTILINE)


def test_readme_shows_a_backtest_quickstart():
    commands = _readme_koval_commands()

    assert any(command.startswith("koval examples") for command in commands)
    assert any(command.startswith("koval backtest") for command in commands)


def test_readme_quickstart_commands_run_as_written(tmp_path):
    """Run the README's commands verbatim, in order, in an empty directory.

    A quickstart is the first thing a new user tries and the first thing to
    rot. Executing it is the only way to know it still works.
    """
    console_script = Path(sys.executable).parent / "koval"
    assert console_script.is_file(), "koval-engine's console script must be on the same venv"

    for command in _readme_koval_commands():
        result = subprocess.run(
            [str(console_script), *shlex.split(command)[1:]],
            capture_output=True,
            text=True,
            check=False,
            cwd=tmp_path,
        )
        assert result.returncode == 0, f"{command}\n{result.stdout}{result.stderr}"
