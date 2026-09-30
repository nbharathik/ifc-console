"""Workbench.ask() keeps its one-shot shape on top of the bundled Agent."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from ifc_console.sdk import Workbench


@pytest.fixture
def wb(tmp_path: Path, minimal_ifc4_path: Path):
    model = tmp_path / "work.ifc"
    shutil.copy2(minimal_ifc4_path, model)
    with Workbench.open(model, home=tmp_path / "home") as workbench:
        yield workbench


def scripted(*rounds, seen=None):
    """A provider that replays one scripted event list per model round."""
    remaining = iter(rounds)

    async def astream(provider, **kwargs):
        if seen is not None:
            seen.append(kwargs)
        for event in next(remaining):
            yield event

    return astream


def test_ask_runs_a_tool_and_returns_the_answer(wb: Workbench, monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(
        "ifc_console.agents.providers.astream",
        scripted(
            [
                {"type": "usage", "in": 30, "out": 2},
                {
                    "type": "tool_calls",
                    "calls": [
                        {
                            "id": "c1",
                            "name": "query_elements",
                            "arguments": '{"query": "IfcWall"}',
                        }
                    ],
                },
            ],
            [
                {"type": "content", "text": "The model has three walls."},
                {"type": "usage", "in": 120, "out": 8},
            ],
            seen=seen,
        ),
    )
    result = wb.ask("how many walls?", model="test-model", api_key="sk-test")
    assert result["text"] == "The model has three walls."
    assert result["tool_calls"][0]["name"] == "query_elements"
    assert result["tool_calls"][0]["ok"] is True
    assert result["usage"] == {"in": 150, "out": 10}
    assert [turn["role"] for turn in result["turns"]] == ["user", "assistant"]
    names = {tool["name"] for tool in seen[0]["tools"]}
    assert {"query_elements", "get_element"} <= names


def test_ask_reports_events_as_they_stream(wb: Workbench, monkeypatch):
    monkeypatch.setattr(
        "ifc_console.agents.providers.astream",
        scripted([{"type": "content", "text": "hello"}]),
    )
    seen = []
    wb.ask("hi", model="m", api_key="k", on_event=seen.append)
    assert seen[0] == {"type": "content", "text": "hello"}


def test_ask_can_run_without_tools_and_keeps_earlier_turns(wb: Workbench, monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(
        "ifc_console.agents.providers.astream",
        scripted([{"type": "content", "text": "ok"}], seen=seen),
    )
    history = [{"role": "user", "text": "hi"}, {"role": "assistant", "text": "hello"}]
    result = wb.ask("and now?", model="m", api_key="k", tools=False, history=history)
    assert seen[0]["tools"] == []
    assert [turn["text"] for turn in seen[0]["turns"]][:2] == ["hi", "hello"]
    assert [turn["role"] for turn in result["turns"]] == ["user", "assistant", "user", "assistant"]


def test_ask_without_a_key_says_so(wb: Workbench, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(Exception) as excinfo:
        wb.ask("hi", model="m")
    assert "API key" in str(excinfo.value)


def test_ask_refuses_an_unknown_provider(wb: Workbench):
    with pytest.raises(Exception) as excinfo:
        wb.ask("hi", provider="nope", model="m")
    assert "unknown provider" in str(excinfo.value)
