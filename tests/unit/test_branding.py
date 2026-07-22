"""Brand assets: the block wordmark, console markup, and the SVG kit."""

from __future__ import annotations

import xml.dom.minidom as minidom
from pathlib import Path

from ifc_console import branding


def test_block_wordmark_rows_are_aligned() -> None:
    rows = branding.block_lines("ifc-console")
    assert len(rows) == 5
    assert len({len(r) for r in rows}) == 1  # every row is the same width
    assert "█" in rows[0]


def test_render_block_joins_the_rows() -> None:
    assert branding.render_block("ifc-console") == "\n".join(branding.block_lines("ifc-console"))


def test_console_markup_is_produced() -> None:
    # the corner brand row is the console's only wordmark; there is no banner
    assert "IFC CONSOLE" in branding.top_right_markup()


def test_write_assets_emits_valid_svgs(tmp_path: Path) -> None:
    written = branding.write_assets(tmp_path)
    names = {p.name for p in written}
    assert {
        "mark.svg",
        "favicon.svg",
        "monogram.svg",
        "horizontal-dark.svg",
        "horizontal-light.svg",
        "social-preview.svg",
        "logo.txt",
    } <= names
    for path in written:
        if path.suffix == ".svg":
            minidom.parse(str(path))  # raises if the SVG is malformed
