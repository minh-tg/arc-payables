"""The README For judges guide: real links, four ordered rows, legend-only states, no em dashes."""

from __future__ import annotations

import re
from pathlib import Path

README = Path(__file__).resolve().parents[1] / "README.md"
HEADING = "## For judges"
CRITERIA = ["Agentic Sophistication", "Traction", "Circle tool usage", "Innovation"]
LEGEND_WORDS = {"mock", "testnet", "saved evidence", "unverified"}
LEGEND_LINE = re.compile(r"^- \*\*(.+?)\*\* \((.+)\)$")
LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def section() -> str:
    lines = README.read_text(encoding="utf-8").splitlines()
    start = lines.index(HEADING)
    body = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        body.append(line)
    return "\n".join(body)


def table() -> list[list[str]]:
    rows = []
    for line in section().splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if all(cell and set(cell) <= {"-", ":"} for cell in cells):
            continue
        rows.append(cells)
    return rows


def test_the_heading_comes_before_every_other_section():
    headings = [line for line in README.read_text(encoding="utf-8").splitlines() if line.startswith("## ")]
    assert headings[0] == HEADING


def test_the_table_is_the_four_criteria_rows_in_order():
    rows = table()
    assert rows[0] == ["Criterion", "What to look at", "Command", "State"]
    assert len(rows) == 5, f"expected four rows, found {len(rows) - 1}"
    assert [row[0] for row in rows[1:]] == CRITERIA


def test_every_relative_link_target_exists():
    root = README.parent
    targets = LINK.findall(section())
    assert targets, "the section links to no file"
    for target in targets:
        path = target.split("#", 1)[0]
        if not path or "://" in path:
            continue
        assert (root / path).exists(), f"{target} does not exist"


def test_state_cells_hold_only_legend_words():
    legend = {match.group(1) for line in section().splitlines() if (match := LEGEND_LINE.match(line.strip()))}
    assert legend == LEGEND_WORDS
    for row in table()[1:]:
        states = [state.strip() for state in row[3].split(",")]
        assert states and all(states), f"{row[0]} has an empty state cell"
        for state in states:
            assert state in legend, f"{state!r} in {row[0]} is not a legend word"


def test_the_section_uses_no_em_dash():
    assert "\u2014" not in section()
