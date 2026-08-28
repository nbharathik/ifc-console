"""The core wheel remains complete when no agent product can be imported."""

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
                if fullname == "ifc_console_agents" or fullname.startswith("ifc_console_agents."):
                    raise ModuleNotFoundError(
                        f"blocked optional module {{fullname}}", name="ifc_console_agents"
                    )
                return None

        sys.meta_path.insert(0, NoAgents())

        import ifc_console
        namespace = {{}}
        exec("from ifc_console import *", namespace)
        assert "Workbench" in namespace
        assert "Agent" not in namespace
        from ifc_console.app import AppCore
        from ifc_console.application.operations import build_operations
        from ifc_console.settings import SettingsStore
        from ifc_console.viewer import assets

        assert assets.available()
        assert ifc_console.Workbench
        try:
            getattr(ifc_console, "Agent")
        except AttributeError as exc:
            assert "ifc-console-agents" in str(exc)
        else:
            raise AssertionError("optional Agent unexpectedly resolved")

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            core = AppCore(SettingsStore(home=root / "home", project_dir=root, env={{}}))
            try:
                assert not core.extensions.available("agents")
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
