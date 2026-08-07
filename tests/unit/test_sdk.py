"""The public SDK: scriptable BIM without a server or a terminal."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from ifc_console.sdk import IfcConsoleError, Workbench


@pytest.fixture
def wb(tmp_path: Path, minimal_ifc4_path: Path):
    model = tmp_path / "work.ifc"
    shutil.copy2(minimal_ifc4_path, model)
    with Workbench.open(model, home=tmp_path / "home") as workbench:
        yield workbench


def test_workbench_is_exported_from_the_package():
    import ifc_console

    assert ifc_console.Workbench is Workbench
    assert "Workbench" in dir(ifc_console)


def test_open_loads_the_model(wb: Workbench):
    assert wb.model["loaded"] is True
    assert wb.model["schema"] == "IFC4"
    assert wb.mode == "ask"


def test_query_returns_rows(wb: Workbench):
    walls = wb.query("IfcWall")
    assert len(walls) == 3
    assert {"global_id", "class", "name"} <= set(walls[0])


def test_project_info_and_tree(wb: Workbench):
    assert wb.info()["project"]["name"] == "Duplex"
    assert wb.tree()["tree"]["class"] == "IfcProject"


def test_psets_reach_the_property_values(wb: Workbench):
    wall = next(w for w in wb.query("IfcWall") if w["name"] == "Wall-1")
    result = wb.psets(wall["global_id"])
    psets = result["results"][0]["psets"]
    assert psets["Pset_WallCommon"]["FireRating"] == "F30"


def test_tool_errors_become_exceptions_with_a_code(wb: Workbench):
    with pytest.raises(IfcConsoleError) as excinfo:
        wb.query("IfcWall, ((broken")
    assert excinfo.value.code == "INVALID_QUERY"
    assert excinfo.value.hint


def test_call_returns_the_raw_envelope_instead_of_raising(wb: Workbench):
    envelope = wb.call("query_elements", query="NotAnIfcClass")
    assert set(envelope) >= {"ok", "meta"}
    assert envelope["meta"]["model"]


def test_tools_are_provider_neutral_schemas(wb: Workbench):
    tools = wb.tools()
    names = {tool["name"] for tool in tools}
    assert {"query_elements", "get_element", "execute_ifc_code"} <= names
    one = next(t for t in tools if t["name"] == "query_elements")
    assert one["input_schema"]["type"] == "object"
    assert "query" in one["input_schema"]["properties"]
    assert one["description"]


def test_every_advertised_tool_is_callable(wb: Workbench):
    """tools() and call() must describe the same surface."""
    advertised = {tool["name"] for tool in wb.tools()}
    assert advertised <= set(wb.core.tool_functions)


def test_ask_mode_blocks_mutating_code(wb: Workbench):
    with pytest.raises(IfcConsoleError) as excinfo:
        wb.run_code("ifc_api.root.create_entity(ifc, ifc_class='IfcWall')", "add a wall")
    assert excinfo.value.code == "ASK_MODE_BLOCKED"


def test_edit_mode_runs_the_api_and_saves(wb: Workbench, tmp_path: Path):
    wb.set_mode("edit")
    result = wb.run_code(
        "ifc_api.root.create_entity(ifc, ifc_class='IfcWall', name='SDK'); len(by_class('IfcWall'))",
        "add one wall",
    )
    assert result["result"] == "4"
    assert wb.model["dirty"] is True
    saved = wb.save(tmp_path / "out.ifc")
    assert Path(saved["path"]).exists()


def test_read_only_code_still_runs_in_ask_mode(wb: Workbench):
    result = wb.run_code("len(by_class('IfcWall'))")
    assert result["result"] == "3"


def test_schema_docs_work_without_the_knowledge_index(wb: Workbench):
    docs = wb.schema_docs(entity="IfcWall")
    assert docs["entity"] == "IfcWall"
    assert "Pset_WallCommon" in docs["applicable_psets"]


def test_settings_overrides_stay_in_memory(tmp_path: Path, minimal_ifc4_path: Path):
    home = tmp_path / "home"
    with Workbench.open(minimal_ifc4_path, home=home, settings={"knowledge.enabled": False}) as wb:
        assert wb.core.settings.knowledge.enabled is False
    written = (home / "settings.json").read_text(encoding="utf-8") if (home / "settings.json").exists() else ""
    assert '"enabled": false' not in written, "an override must not reach the user file"


def test_unknown_setting_is_refused(tmp_path: Path):
    with pytest.raises(IfcConsoleError):
        Workbench.open(None, home=tmp_path / "home", settings={"nope.nothing": 1})


def test_workbench_opens_without_a_model(tmp_path: Path):
    with Workbench.open(home=tmp_path / "home") as wb:
        assert wb.model["loaded"] is False
        with pytest.raises(IfcConsoleError) as excinfo:
            wb.query("IfcWall")
        assert excinfo.value.code == "NO_MODEL_LOADED"


def test_attached_models_are_listed(wb: Workbench, minimal_ifc4_path: Path):
    wb.attach(minimal_ifc4_path)
    listing = wb.models()
    assert len(listing["models"]) == 2


def test_close_is_idempotent(tmp_path: Path):
    wb = Workbench.open(home=tmp_path / "home")
    wb.close()
    with pytest.raises(RuntimeError):
        wb.status()


# ---------------------------------------------------------------- wb.ask(...)
def test_ask_runs_a_tool_and_returns_the_answer(wb: Workbench, monkeypatch):
    """The SDK drives the same loop the chat panel does, with any provider."""
    rounds = iter(
        [
            [
                {
                    "type": "tool_calls",
                    "calls": [
                        {"id": "c1", "name": "query_elements", "arguments": '{"query": "IfcWall"}'}
                    ],
                }
            ],
            [{"type": "content", "text": "The model has three walls."},
             {"type": "usage", "in": 120, "out": 8}],
        ]
    )

    def fake_stream(provider, **kwargs):
        yield from next(rounds)

    monkeypatch.setattr("ifc_console.chat.agent.stream", fake_stream)
    result = wb.ask("how many walls?", model="test-model", api_key="sk-test")
    assert result["text"] == "The model has three walls."
    assert result["tool_calls"][0]["name"] == "query_elements"
    assert result["tool_calls"][0]["ok"] is True
    assert result["usage"] == {"in": 120, "out": 8}
    assert [turn["role"] for turn in result["turns"]] == ["user", "assistant"]


def test_ask_reports_events_as_they_stream(wb: Workbench, monkeypatch):
    def fake_stream(provider, **kwargs):
        yield {"type": "content", "text": "hello"}

    monkeypatch.setattr("ifc_console.chat.agent.stream", fake_stream)
    seen = []
    wb.ask("hi", model="m", api_key="k", on_event=seen.append)
    assert seen[0]["type"] == "content"


def test_ask_without_a_key_says_so(wb: Workbench, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(Exception) as excinfo:
        wb.ask("hi", model="m")
    assert "API key" in str(excinfo.value)
