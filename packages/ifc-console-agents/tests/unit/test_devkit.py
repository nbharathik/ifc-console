"""The dev harness: a demo project, an offline model, and no browser tabs."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from ifc_console_agents.chat.providers import PROVIDERS
from ifc_console_agents.devkit.checks import CheckRun, render, sse_events
from ifc_console_agents.devkit.rehearsal import (
    REHEARSAL,
    REHEARSAL_ID,
    enable_from_environment,
    rehearsal_stream,
)
from ifc_console_agents.devkit.scenario import MANUAL_NAME, MODEL_NAME, build_scenario


@pytest.fixture(autouse=True)
def _no_rehearsal_leak():
    """The rehearsal provider must never linger in a normal process."""
    yield
    PROVIDERS.pop(REHEARSAL_ID, None)


class TestRehearsalProvider:
    def test_it_is_absent_unless_asked_for(self, monkeypatch):
        monkeypatch.delenv("IFC_CONSOLE_DEV", raising=False)
        PROVIDERS.pop(REHEARSAL_ID, None)
        enable_from_environment()
        assert REHEARSAL_ID not in PROVIDERS

    def test_the_environment_flag_turns_it_on(self, monkeypatch):
        monkeypatch.setenv("IFC_CONSOLE_DEV", "1")
        PROVIDERS.pop(REHEARSAL_ID, None)
        enable_from_environment()
        assert PROVIDERS[REHEARSAL_ID] is REHEARSAL

    def test_it_needs_no_key_and_points_at_loopback(self):
        assert REHEARSAL.needs_key is False
        assert REHEARSAL.base_url.startswith("http://127.0.0.1")

    def _events(self, turns, tools, model="rehearsal-tools"):
        return list(
            rehearsal_stream(
                REHEARSAL,
                model=model,
                system="",
                turns=turns,
                tools=[{"name": name} for name in tools],
                options={},
            )
        )

    def test_the_first_round_scopes_the_model(self):
        events = self._events(
            [{"role": "user", "text": "what is here?"}],
            ["get_ifc_project_info", "query_elements"],
        )
        calls = [event for event in events if event["type"] == "tool_calls"][0]["calls"]
        assert [call["name"] for call in calls] == ["get_ifc_project_info", "query_elements"]
        assert json.loads(calls[1]["arguments"])["query"] == "IfcWall"

    def test_it_answers_when_it_holds_no_tools(self):
        events = self._events([{"role": "user", "text": "hello"}], [])
        assert any(event["type"] == "content" for event in events)
        assert not any(event["type"] == "tool_calls" for event in events)

    def test_the_synthetic_image_message_does_not_hide_the_real_request(self):
        """The agent loop injects '[image content from ...]' user turns; the
        request is still the sentence the person typed."""
        turns = [
            {"role": "user", "text": "measure and propose the thickness"},
            {"role": "tool", "name": "query_elements", "text": '{"global_id": "0KP4gnzpTEFgz0AowkG89h"}'},
            {"role": "tool", "name": "measure_elements", "text": "{}"},
            {"role": "tool", "name": "get_project_document_page", "text": "{}"},
            {"role": "user", "text": "[image content from get_project_document_page]"},
        ]
        events = self._events(
            turns,
            ["measure_elements", "get_project_document_page", "measure__propose_measured_value"],
        )
        calls = [event for event in events if event["type"] == "tool_calls"]
        assert calls, "the proposal round never ran"
        assert calls[0]["calls"][0]["name"] == "measure__propose_measured_value"

    def test_it_does_not_propose_unless_asked_to(self):
        turns = [
            {"role": "user", "text": "just measure the walls"},
            {"role": "tool", "name": "query_elements", "text": '{"global_id": "0KP4gnzpTEFgz0AowkG89h"}'},
            {"role": "tool", "name": "measure_elements", "text": "{}"},
        ]
        events = self._events(turns, ["measure_elements", "measure__propose_measured_value"])
        assert not any(event["type"] == "tool_calls" for event in events)

    def test_usage_uses_the_key_names_the_agent_loop_reads(self):
        events = self._events([{"role": "user", "text": "hi"}], [])
        usage = [event for event in events if event["type"] == "usage"][0]
        assert set(usage) == {"type", "in", "out"}

    def test_cancellation_stops_the_stream(self):
        cancel = threading.Event()
        cancel.set()
        events = list(
            rehearsal_stream(
                REHEARSAL,
                model="rehearsal-tools",
                system="",
                turns=[{"role": "user", "text": "hi"}],
                tools=[],
                options={},
                cancel=cancel,
            )
        )
        assert len(events) <= 1


class TestScenario:
    def test_it_writes_a_model_a_manual_and_a_recipe(self, tmp_path: Path):
        scenario = build_scenario(tmp_path / "demo")
        assert scenario.model.name == MODEL_NAME
        assert scenario.model.stat().st_size > 0
        assert scenario.recipe is not None and scenario.recipe.is_file()
        assert any(path.name == MANUAL_NAME for path in scenario.references)

    def test_references_land_where_the_agents_look(self, tmp_path: Path):
        scenario = build_scenario(tmp_path / "demo")
        expected = scenario.project_dir / ".ifc-console" / "agents" / "references"
        for path in scenario.references:
            assert path.parent == expected

    def test_rebuilding_is_idempotent(self, tmp_path: Path):
        first = build_scenario(tmp_path / "demo")
        before = first.model.read_bytes()
        second = build_scenario(tmp_path / "demo")
        assert second.model.read_bytes() == before

    def test_the_demo_model_has_walls_to_measure(self, tmp_path: Path):
        import ifcopenshell

        scenario = build_scenario(tmp_path / "demo")
        ifc = ifcopenshell.open(str(scenario.model))
        walls = ifc.by_type("IfcWall")
        assert len(walls) == 3
        assert {wall.Name for wall in walls} == {
            "Interior Wall A",
            "Interior Wall B",
            "Exterior Wall C",
        }


class TestDevCommandSafety:
    def test_fresh_never_deletes_an_explicit_project(self, tmp_path: Path, capsys):
        from ifc_console.cli import main

        project = tmp_path / "real-project"
        project.mkdir()
        sentinel = project / "keep-me.txt"
        sentinel.write_text("important", encoding="utf-8")

        code = main(["dev", "--project", str(project), "--fresh", "--check"])

        assert code == 3
        assert sentinel.read_text(encoding="utf-8") == "important"
        assert "--fresh only resets the disposable temporary demo" in capsys.readouterr().err

    def test_fresh_stops_when_the_temporary_project_cannot_be_removed(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        import shutil
        import tempfile

        from ifc_console import cli

        from ifc_console_agents.devkit import serve

        project = tmp_path / "ifc-console-dev-project"
        project.mkdir()
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))

        def locked(_path):
            raise PermissionError("locked by another process")

        def must_not_build(*_args, **_kwargs):
            raise AssertionError("the harness started after an incomplete reset")

        monkeypatch.setattr(shutil, "rmtree", locked)
        monkeypatch.setattr(serve, "build_dev_core", must_not_build)

        assert cli.main(["dev", "--fresh", "--check"]) == 2
        assert "could not reset the temporary dev project" in capsys.readouterr().err


class TestCheckReporting:
    def test_a_run_fails_when_any_check_fails(self):
        run = CheckRun()
        run.add("a", True)
        assert run.passed
        run.add("b", False, "broke")
        assert not run.passed
        assert [check.name for check in run.failures] == ["b"]

    def test_a_skipped_check_does_not_fail_the_run(self):
        run = CheckRun()
        run.add("a", False, skipped=True)
        assert run.passed
        assert run.checks[0].status == "skip"

    def test_the_table_names_every_check_and_the_totals(self):
        run = CheckRun()
        run.add("first", True, "fine")
        run.add("second", False, "broke")
        text = render(run)
        assert "first" in text and "second" in text
        assert "1/2 checks passed, 1 failed" in text

    def test_sse_parsing_skips_malformed_frames(self):
        text = 'data: {"type":"a"}\n\ndata: nonsense\n\ndata: {"type":"b"}\n\n'
        assert [event["type"] for event in sse_events(text)] == ["a", "b"]
