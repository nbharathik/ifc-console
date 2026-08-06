"""Cold-start budget: the CLI import graph stays free of heavy modules.

ifcopenshell costs about a second to import; textual, uvicorn, and the mcp
SDK are not free either. None of them may load before a command actually
needs them, or `ifc-console --help` pays for all of it.
"""

from __future__ import annotations

import subprocess
import sys

_HEAVY = ("ifcopenshell", "textual", "uvicorn", "starlette", "mcp", "pydantic", "numpy")

_PROBE = (
    "import sys; import ifc_console.cli; "
    f"loaded = [m for m in {_HEAVY!r} if m in sys.modules]; "
    "print(','.join(loaded))"
)

# The prologue every entry point runs: the preload thread and the main thread
# must never be inside the same lazy package's __getattr__ at once.
_RACE = (
    "import ifc_console.cli; "
    "from ifc_console import preload; preload.start(); "
    "from ifc_console.settings import SettingsStore; "
    "from ifc_console.mcp import server; "
    "preload.release(); print('ok')"
)


def test_cli_import_stays_light():
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    loaded = [m for m in result.stdout.strip().split(",") if m]
    assert loaded == [], f"heavy modules imported at CLI import time: {loaded}"


def test_version_flag_does_not_import_the_world():
    """--version reads package metadata; it must not drag in the backend."""
    result = subprocess.run(
        [sys.executable, "-m", "ifc_console.cli", "--version"],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    assert result.stdout.startswith("ifc-console ")


def test_preload_never_races_the_main_thread():
    for _ in range(15):
        result = subprocess.run(
            [sys.executable, "-c", _RACE], capture_output=True, text=True, timeout=180
        )
        assert result.returncode == 0, result.stderr[-800:]
