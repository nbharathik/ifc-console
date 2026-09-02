"""Run the agent browser panel's pure ES-module tests from pytest."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from ifc_console_agents import assets

UI_TESTS = Path(__file__).resolve().parents[1] / "ui"
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_panel_modules_pass_their_node_tests() -> None:
    files = sorted(str(path) for path in UI_TESTS.glob("*.test.mjs"))
    assert files, "no agent panel module tests found"
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [NODE, "--test", *files],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_every_panel_module_is_covered() -> None:
    static = assets.require_static_dir()
    pure = {
        "chat_ai_sdk.js",
        "chat_markdown.js",
        "chat_flow.js",
        "chat_history.js",
        "chat_memory.js",
        "chat_sidebar.js",
        "chat_studio.js",
        "chat_workspace.js",
        "workflows_model.js",
    }
    missing = [name for name in pure if not (static / name).is_file()]
    assert not missing, f"expected panel modules are gone: {missing}"
    covered = " ".join(
        path.read_text(encoding="utf-8") for path in UI_TESTS.glob("*.test.mjs")
    )
    untested = [name for name in sorted(pure) if name not in covered]
    assert not untested, f"panel modules without tests: {untested}"
