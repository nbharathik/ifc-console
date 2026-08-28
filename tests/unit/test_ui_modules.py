"""Run the core viewer's pure ES-module tests from pytest."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from ifc_console.viewer import assets

UI_TESTS = Path(__file__).resolve().parents[1] / "ui"
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_viewer_modules_pass_their_node_tests() -> None:
    files = sorted(str(path) for path in UI_TESTS.glob("*.test.mjs"))
    assert files, "no core viewer module tests found"
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [NODE, "--test", *files],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_measurement_module_is_covered() -> None:
    static = assets.require_static_dir()
    assert (static / "measure_math.js").is_file()
    covered = " ".join(
        path.read_text(encoding="utf-8") for path in UI_TESTS.glob("*.test.mjs")
    )
    assert "measure_math.js" in covered
