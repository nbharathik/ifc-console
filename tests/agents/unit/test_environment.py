from __future__ import annotations

import sys

from ifc_console.agents import environment


def test_agent_capabilities_come_from_one_optional_extra() -> None:
    found = environment.capabilities()

    assert {item.id for item in found} == {"credential_store", "engines", "pdf"}
    assert not any(item.required for item in found)
    assert all(item.as_dict()["install"] is None for item in found)
    assert environment.report()["ok"] is True


def test_dependency_repair_hints_name_the_single_agents_extra() -> None:
    hint = environment.missing_dependency_hint("pypdfium2")

    assert 'ifc-console[agents]' in hint
    assert "ifc-console-agents" not in hint


def test_uv_tool_repair_reinstalls_the_agents_extra(monkeypatch) -> None:
    monkeypatch.setattr(environment, "install_kind", lambda: "uv-tool")

    assert environment.repair_command() == 'uv tool install "ifc-console[agents]" --force'


def test_uv_venv_without_pip_gets_a_working_repair_command(monkeypatch) -> None:
    monkeypatch.setattr(environment, "install_kind", lambda: "venv")
    monkeypatch.setattr(environment, "_probe", lambda module: module != "pip")

    assert environment.repair_command() == (
        f'uv pip install --python "{sys.executable}" --upgrade "ifc-console[agents]"'
    )
