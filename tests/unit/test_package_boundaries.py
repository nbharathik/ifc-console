"""The console and viewer keep working when the agents extension cannot load."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path


def test_core_import_cli_viewer_and_operations_work_without_agents(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[2] / "src"
    script = textwrap.dedent(
        f"""
        import importlib.abc
        import pathlib
        import sys
        import tempfile

        sys.path.insert(0, {str(source)!r})

        class NoAgents(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "ifc_console.agents" or fullname.startswith("ifc_console.agents."):
                    raise ModuleNotFoundError(
                        f"blocked module {{fullname}}", name="ifc_console.agents"
                    )
                return None

        sys.meta_path.insert(0, NoAgents())

        import ifc_console
        from ifc_console.app import AppCore
        from ifc_console.application.operations import build_operations
        from ifc_console.settings import SettingsStore
        from ifc_console.viewer import assets

        assert assets.available()
        assert ifc_console.Workbench

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            core = AppCore(SettingsStore(home=root / "home", project_dir=root, env={{}}))
            try:
                assert not core.extensions.available("agents")
                assert [r.status for r in core.extensions.records] == ["error"]
                build_operations(core)
                assert "list_agent_skills" not in core.operations
                assert "open_viewer" in core.operations
            finally:
                core.shutdown()
        """
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_importing_the_package_and_cli_never_loads_the_agents(tmp_path: Path) -> None:
    script = (
        "import sys; import ifc_console; import ifc_console.cli; "
        "assert not any(m.startswith('ifc_console.agents') for m in sys.modules), "
        "sorted(m for m in sys.modules if m.startswith('ifc_console.agents'))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
