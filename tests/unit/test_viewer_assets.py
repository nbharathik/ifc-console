"""Static checks on the viewer bundle.

There is no browser in the test environment, so these stand in for the one
class of bug that would otherwise only show up as a blank panel: markup and
script drifting apart.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[2] / "src" / "ifc_console" / "viewer" / "static"


@pytest.fixture(scope="module")
def html() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def script() -> str:
    return (STATIC / "app.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def styles() -> str:
    return (STATIC / "app.css").read_text(encoding="utf-8")


def test_every_element_the_script_looks_up_exists(html: str, script: str) -> None:
    ids = set(re.findall(r'id="([^"]+)"', html))
    looked_up = set(re.findall(r'\$\("([^"]+)"\)', script))
    assert not looked_up - ids, f"app.js reads ids that index.html does not define: {looked_up - ids}"


def test_panel_ids_passed_as_strings_exist(html: str, script: str) -> None:
    """initSidePanel and POPOVERS name their elements in string literals."""
    ids = set(re.findall(r'id="([^"]+)"', html))
    for name in ("tree-panel", "props-panel", "split-tree", "split-props",
                 "settings-panel", "help-panel", "tools-panel"):
        assert name in ids


def test_search_and_saved_view_controls_are_present(html: str) -> None:
    for name in ("search-input", "search-clear", "search-results",
                 "saved-views", "view-name", "tool-save-view"):
        assert f'id="{name}"' in html, f"{name} is missing from index.html"


def test_no_style_attributes_or_inline_scripts(html: str) -> None:
    """The CSP forbids both; a violation only shows up in a browser console."""
    assert not re.search(r"<script(?![^>]*\bsrc=)", html)
    assert not re.search(r"<style[\s>]", html)


def test_stylesheet_defines_every_custom_property_it_uses(styles: str) -> None:
    used = set(re.findall(r"var\((--[a-z-]+)", styles))
    defined = set(re.findall(r"^\s*(--[a-z-]+):", styles, re.MULTILINE))
    assert not used - defined, f"undefined CSS variables: {sorted(used - defined)}"
