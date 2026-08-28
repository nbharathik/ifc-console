from __future__ import annotations

import sys

from ifc_console_agents import environment


def test_every_advertised_agent_capability_is_required() -> None:
    found = environment.capabilities()

    assert {item.id for item in found} == {
        "credential_store",
        "graph",
        "graph_checkpoints",
        "pdf_pages",
        "pdf_text",
    }
    assert all(item.required for item in found)
    assert all(item.as_dict()["install"] is None for item in found)


def test_dependency_repair_hints_never_name_feature_extras() -> None:
    hint = environment.missing_dependency_hint("pypdf")

    assert "ships inside ifc-console-agents" in hint
    assert "ifc-console-agents[" not in hint


def test_uv_tool_repair_installs_the_single_complete_agents_package(monkeypatch) -> None:
    monkeypatch.setattr(environment, "install_kind", lambda: "uv-tool")

    assert environment.repair_command() == (
        "uv tool install --with ifc-console-agents ifc-console --force"
    )


def test_uv_venv_without_pip_gets_a_working_repair_command(monkeypatch) -> None:
    monkeypatch.setattr(environment, "install_kind", lambda: "venv")
    monkeypatch.setattr(environment, "_probe", lambda module: module != "pip")

    assert environment.repair_command() == (
        f'uv pip install --python "{sys.executable}" --upgrade ifc-console-agents'
    )
