"""The shipped example must be the example that runs.

A README or an examples directory that has drifted from the code is worse than
none: it costs a new user their first half hour and teaches them not to trust
the docs. So the example is executed here rather than eyeballed.

`docs/` makes the stronger claim — its walkthroughs print specific numbers —
so those pages are held to it: the code block is executed and its output is
compared against the figure printed beside it.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
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


def _python_block(page: str, marker: str) -> str:
    """The one fenced Python block on `page` containing `marker`."""
    blocks = re.findall(r"```python\n(.*?)```", (DOCS / page).read_text(encoding="utf-8"), re.S)
    matching = [block for block in blocks if marker in block]

    assert len(matching) == 1, f"expected exactly one block in {page} containing {marker!r}"
    return matching[0]


def test_the_strategies_walkthrough_runs_and_prints_its_documented_output(tmp_path):
    """`docs/strategies.md` names the output of its hand-written strategy.

    That figure is the page's proof that the adapter can be driven directly,
    and it is only worth anything while it is still true.
    """
    page = (DOCS / "strategies.md").read_text(encoding="utf-8")
    documented = re.search(r"Output on this seed: `(?P<output>.+?)`", page)
    assert documented, "strategies.md must state the walkthrough's output"

    script = tmp_path / "walkthrough.py"
    script.write_text(_python_block("strategies.md", "class BreakoutStrategy"), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == documented.group("output")


def _shell_block_with_output(page: str, marker: str) -> tuple[str, str]:
    """A fenced shell block containing `marker`, with the output block below it."""
    match = re.search(
        r"```bash\n(?P<command>[^`]*?"
        + re.escape(marker)
        + r"[^`]*?)```\s*\n```\n(?P<output>[^`]*?)```",
        (DOCS / page).read_text(encoding="utf-8"),
        re.S,
    )

    assert match, f"expected a shell block in {page} containing {marker!r}, followed by its output"
    return match.group("command"), match.group("output")


def test_the_custom_engine_walkthrough_runs_and_prints_its_documented_metrics(tmp_path):
    """`docs/custom-engines.md` promises a competing engine can be dropped in
    with no packaging. Running its example is the only way that promise stays
    true through a change to the loader or the protocol."""
    console_script = Path(sys.executable).parent / "koval"
    assert console_script.is_file(), "koval-engine's console script must be on the same venv"

    engine = _python_block("custom-engines.md", "class BuyAndHoldEngine")
    (tmp_path / "my_engine.py").write_text(engine, encoding="utf-8")
    subprocess.run(
        [str(console_script), "examples", "--copy", "."],
        capture_output=True,
        text=True,
        check=True,
        cwd=tmp_path,
    )

    command, documented = _shell_block_with_output("custom-engines.md", "KOVAL_BACKTEST_ENGINE")
    tokens = shlex.split(" ".join(command.replace("\\\n", " ").split()))
    overrides = {}
    while "=" in tokens[0]:
        name, _, value = tokens[0].partition("=")
        overrides[name] = value
        tokens.pop(0)

    assert tokens[0] == "koval", f"expected a koval command, got {tokens[0]!r}"
    result = subprocess.run(
        [str(console_script), *tokens[1:]],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
        env={**os.environ, **overrides},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == documented.strip()
