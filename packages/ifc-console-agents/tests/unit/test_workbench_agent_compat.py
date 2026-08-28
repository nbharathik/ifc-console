"""One-release compatibility coverage for Workbench.ask()."""

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


def test_ask_runs_a_tool_and_returns_the_answer(wb: Workbench, monkeypatch):
    rounds = iter(
        [
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
        ]
    )

    def fake_stream(provider, **kwargs):
        yield from next(rounds)

    monkeypatch.setattr("ifc_console_agents.chat.agent.stream", fake_stream)
    result = wb.ask("how many walls?", model="test-model", api_key="sk-test")
    assert result["text"] == "The model has three walls."
    assert result["tool_calls"][0]["name"] == "query_elements"
    assert result["tool_calls"][0]["ok"] is True
    assert result["usage"] == {"in": 150, "out": 10}
    assert [turn["role"] for turn in result["turns"]] == ["user", "assistant"]


def test_ask_reports_events_as_they_stream(wb: Workbench, monkeypatch):
    def fake_stream(provider, **kwargs):
        yield {"type": "content", "text": "hello"}

    monkeypatch.setattr("ifc_console_agents.chat.agent.stream", fake_stream)
    seen = []
    wb.ask("hi", model="m", api_key="k", on_event=seen.append)
    assert seen[0]["type"] == "content"


def test_ask_without_a_key_says_so(wb: Workbench, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(Exception) as excinfo:
        wb.ask("hi", model="m")
    assert "API key" in str(excinfo.value)
