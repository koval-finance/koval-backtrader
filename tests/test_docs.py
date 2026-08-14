"""Consistency guards for the human-facing documentation.

The README is the first thing a stranger reads and `docs/` is where it sends
them. A link that 404s there costs the reader their trust before they have
run anything, so link rot is a build failure rather than a habit. The README
links to repository files with absolute GitHub URLs — relative paths break
when PyPI renders it — which is exactly the kind of link nobody notices going
stale, so those are resolved back to local paths and checked too.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
BLOB_PREFIX = "https://github.com/koval-finance/koval-backtrader/blob/main/"

_LINK = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
_HEADING = re.compile(r"^#{1,6}\s+(.*)$", re.MULTILINE)


def _documents() -> list[Path]:
    return [ROOT / "README.md", *sorted(DOCS.glob("*.md"))]


def _anchors(path: Path) -> set[str]:
    """GitHub's heading slugs: lower-cased, punctuation dropped, spaces hyphenated."""
    slugs = set()
    for heading in _HEADING.findall(path.read_text(encoding="utf-8")):
        cleaned = re.sub(r"[^\w\s-]", "", heading.strip().lower())
        slugs.add(re.sub(r"\s+", "-", cleaned))
    return slugs


def test_every_relative_documentation_link_resolves():
    broken = []
    for document in _documents():
        for link in _LINK.findall(document.read_text(encoding="utf-8")):
            target = link.split("#", 1)[0]
            if not target or target.startswith(("http://", "https://", "mailto:")):
                continue
            if not (document.parent / target).resolve().is_file():
                broken.append(f"{document.relative_to(ROOT)} -> {link}")
    assert broken == [], "broken links:\n" + "\n".join(broken)


def test_every_readme_link_into_this_repository_resolves():
    broken = []
    for link in _LINK.findall((ROOT / "README.md").read_text(encoding="utf-8")):
        if not link.startswith(BLOB_PREFIX):
            continue
        target = link[len(BLOB_PREFIX) :].split("#", 1)[0]
        if not (ROOT / target).is_file():
            broken.append(f"README.md -> {link}")
    assert broken == [], "README links to files that do not exist:\n" + "\n".join(broken)


def test_every_section_link_points_at_a_real_heading():
    """A renamed heading breaks every deep link into it, silently."""
    broken = []
    for document in _documents():
        for link in _LINK.findall(document.read_text(encoding="utf-8")):
            if "#" not in link or link.startswith(("http://", "https://", "mailto:")):
                continue
            target, anchor = link.split("#", 1)
            path = (document.parent / target).resolve() if target else document
            if path.suffix != ".md" or not path.is_file():
                continue
            if anchor not in _anchors(path):
                broken.append(f"{document.relative_to(ROOT)} -> {link}")
    assert broken == [], "links to missing headings:\n" + "\n".join(broken)


def test_every_docs_page_is_listed_in_the_index():
    index = (DOCS / "README.md").read_text(encoding="utf-8")
    pages = sorted(path.name for path in DOCS.glob("*.md") if path.name != "README.md")
    missing = [name for name in pages if name not in index]
    assert missing == [], f"docs/README.md must link to: {missing}"


def test_the_readme_sends_readers_to_the_documentation():
    """A docs tree nobody is pointed at may as well not exist."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    pages = {path.name for path in DOCS.glob("*.md") if path.name != "README.md"}
    unreferenced = [name for name in sorted(pages) if f"docs/{name}" not in readme]
    assert unreferenced == [], f"README.md must link to: {unreferenced}"


def test_the_module_line_counts_in_architecture_md_are_current():
    """A number nobody recomputes is a number that is already wrong.

    `docs/architecture.md` sizes each module so a reader knows what they are
    about to open. Every edit to `src/` invalidates one of those figures.
    """
    listing = re.findall(
        r"^├── (\S+\.py)\s+(\d+) lines|^└── (\S+\.py)\s+(\d+) lines",
        (DOCS / "architecture.md").read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    documented = {(a or c): int(b or d) for a, b, c, d in listing}

    assert len(documented) == 4, f"expected four modules in the listing, found {documented}"
    stale = []
    for name, claimed in sorted(documented.items()):
        actual = len((ROOT / "src" / "koval_backtrader" / name).read_text().splitlines())
        if actual != claimed:
            stale.append(f"{name}: documented {claimed}, actual {actual}")
    assert stale == [], "docs/architecture.md line counts are stale:\n" + "\n".join(stale)


def test_the_anchor_slugger_matches_github_rules():
    """Guards the guard: a slugger that returns nothing would pass everything."""
    assert _anchors(DOCS / "results.md") >= {
        "metrics",
        "ids-are-per-run",
        "reading-trade_closed",
    }
