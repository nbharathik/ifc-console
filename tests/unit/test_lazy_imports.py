"""Cold-start budget: the CLI import graph stays free of heavy modules.

ifcopenshell costs about a second to import; textual, uvicorn, and the mcp
SDK are not free either. None of them may load before a command actually
needs them, or `ifc-console --help` pays for all of it.
"""

from __future__ import annotations

import subprocess
import sys

_PROBE = (
    "import sys; import ifc_console.cli; "
    "loaded = [m for m in ('ifcopenshell', 'textual', 'uvicorn') if m in sys.modules]; "
    "print(','.join(loaded))"
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
