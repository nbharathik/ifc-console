"""Run the browser panel's unit tests from pytest.

The panel's pure logic (markdown rendering, the context-flow reducer, the
local conversation archive) lives in ES modules that Node can test without a
browser or an npm install. Keeping them in the pytest run means a Python-only
contributor still sees a UI regression.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

UI_TESTS = Path(__file__).resolve().parents[1] / "ui"
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_panel_modules_pass_their_node_tests() -> None:
    files = sorted(str(path) for path in UI_TESTS.glob("*.test.mjs"))
    assert files, "no panel tests found"
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [NODE, "--test", *files],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_every_panel_module_is_covered() -> None:
    """A new pure module without a test is a gap worth failing on."""
    static = (
        Path(__file__).resolve().parents[2]
        / "packages/ifc-console-viewer/src/ifc_console_viewer/static"
    )
    if not static.is_dir():
        pytest.skip("the viewer package is not in this checkout")
    pure = {
        "chat_markdown.js",
        "chat_flow.js",
        "chat_history.js",
        "chat_sidebar.js",
        "chat_workspace.js",
    }
    missing = [name for name in pure if not (static / name).is_file()]
    assert not missing, f"expected panel modules are gone: {missing}"
    covered = " ".join(path.read_text(encoding="utf-8") for path in UI_TESTS.glob("*.test.mjs"))
    untested = [name for name in sorted(pure) if name not in covered]
    assert not untested, f"panel modules without tests: {untested}"
