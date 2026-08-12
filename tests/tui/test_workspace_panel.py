"""The workspace panel and its slash commands.

The default single-file path must stay exactly as it was; everything here is
the opt-in second mode.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from ifc_console.app import AppCore
from ifc_console.settings import SettingsStore
from ifc_console.tui import commands
from ifc_console.tui.app import IfcConsoleApp
from ifc_console.tui.workspace import WorkspaceModal
from tests.tui.test_commands import FakeConsole

pytestmark = pytest.mark.asyncio

IDS_FIXTURE = Path(__file__).parents[1] / "fixtures" / "wall_firerating.ids"


@pytest.fixture
def console(core) -> FakeConsole:
    core.start_audit()
    return FakeConsole(core)


@pytest.fixture
def project(tmp_path: Path, minimal_ifc4_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    shutil.copy2(minimal_ifc4_path, root / "tower-architecture.ifc")
    shutil.copy2(minimal_ifc4_path, root / "tower-structure.ifc")
    shutil.copy2(IDS_FIXTURE, root / "employer-requirements.ids")
    (root / "notes.md").write_text("not a bim file", encoding="utf-8")
    return root


# ----------------------------------------------------------------- commands
async def test_attach_without_a_model_opens_it_as_the_active_one(
    console: FakeConsole, project: Path
) -> None:
    console.core.add_allowed_dir(project)
    await commands.dispatch(console, f"/attach {project / 'tower-architecture.ifc'}")
    assert console.core.session.loaded
    assert console.core.models.active_id == "tower-architecture"
    assert len(console.core.models.sessions) == 1


async def test_attach_then_models_then_use_then_detach(
    console: FakeConsole, project: Path
) -> None:
    console.core.add_allowed_dir(project)
    await commands.dispatch(console, f"/attach {project / 'tower-architecture.ifc'}")
    await commands.dispatch(console, f"/attach {project / 'tower-structure.ifc'}")
    await commands.dispatch(console, f"/attach {project / 'employer-requirements.ids'}")

    core = console.core
    assert set(core.models.sessions) == {"tower-architecture", "tower-structure"}
    assert core.models.require("tower-structure").read_only is True
    assert "employer-requirements" in core.models.attachments

    console.lines.clear()
    await commands.dispatch(console, "/models")
    text = console.text
    assert "tower-architecture" in text and "active" in text
    assert "read-only" in text
    assert "validate_ids" in text  # the IDS says which tool reads it

    await commands.dispatch(console, "/use tower-structure")
    assert core.models.active_id == "tower-structure"
    assert core.session.read_only is False

    await commands.dispatch(console, "/detach tower-architecture")
    await commands.dispatch(console, "/detach employer-requirements")
    assert set(core.models.sessions) == {"tower-structure"}
    assert core.models.attachments == {}


async def test_use_and_detach_complete_loaded_ids(console: FakeConsole, project: Path) -> None:
    from ifc_console.tui import completion

    console.core.add_allowed_dir(project)
    await commands.dispatch(console, f"/attach {project / 'tower-architecture.ifc'}")
    await commands.dispatch(console, f"/attach {project / 'tower-structure.ifc'}")
    await commands.dispatch(console, f"/attach {project / 'employer-requirements.ids'}")

    detach = [c.insert for c in completion.complete("/detach ", console.core).candidates]
    assert set(detach) == {"tower-architecture", "tower-structure", "employer-requirements"}
    # only models can be made active
    use = [c.insert for c in completion.complete("/use ", console.core).candidates]
    assert set(use) == {"tower-architecture", "tower-structure"}


async def test_use_and_detach_report_unknown_ids(console: FakeConsole, work_model: Path) -> None:
    await commands.dispatch(console, str(work_model))
    await commands.dispatch(console, "/use ghost")
    # the message and the hint both land, so the user knows what to do next.
    # The hint is the console wording, not the MCP tool name the LLM is told.
    assert "no resident model" in console.text
    assert "/models" in console.text
    assert "list_models" not in console.text
    console.lines.clear()
    await commands.dispatch(console, "/detach ghost")
    assert "no resident model" in console.text


async def test_workspace_command_sets_the_root_and_opens_the_panel(
    console: FakeConsole, project: Path
) -> None:
    await commands.dispatch(console, f"/workspace {project}")
    assert project.resolve() in console.core.allowed_dirs
    assert console.panel_opened == 1


async def test_workspace_command_rejects_a_file(console: FakeConsole, work_model: Path) -> None:
    await commands.dispatch(console, f"/workspace {work_model}")
    assert "is not a directory" in console.text


async def test_apply_choice_makes_the_first_model_active(
    console: FakeConsole, project: Path
) -> None:
    from ifc_console.tui.workspace import WorkspaceChoice

    console.core.add_allowed_dir(project)
    choice = WorkspaceChoice(
        models=[project / "tower-architecture.ifc", project / "tower-structure.ifc"],
        files=[project / "employer-requirements.ids"],
    )
    await commands.apply_workspace_choice(console, choice)
    core = console.core
    assert core.models.active_id == "tower-architecture"
    assert core.models.require("tower-structure").read_only is True
    assert "employer-requirements" in core.models.attachments


async def test_apply_choice_promotes_the_first_model_that_loads(
    console: FakeConsole, project: Path
) -> None:
    from ifc_console.tui.workspace import WorkspaceChoice

    broken = project / "broken.ifc"
    broken.write_text("not an IFC file", encoding="utf-8")
    valid = project / "tower-architecture.ifc"
    console.core.add_allowed_dir(project)

    await commands.apply_workspace_choice(
        console, WorkspaceChoice(models=[broken, valid], files=[])
    )
    assert console.core.session.path == valid.resolve()
    assert console.core.session.read_only is False


# -------------------------------------------------------------------- panel
def _app(tmp_path: Path, project: Path, monkeypatch) -> IfcConsoleApp:
    """An app whose only allowed root is the throwaway project folder."""
    monkeypatch.chdir(tmp_path)
    store = SettingsStore(home=tmp_path / "home", project_dir=tmp_path, env={})
    core = AppCore(store)
    core.add_allowed_dir(project)
    return IfcConsoleApp(core, autostart=False)


async def test_panel_lists_supported_kinds_only_until_ctrl_u(
    tmp_path: Path, project: Path, monkeypatch
) -> None:
    app = _app(tmp_path, project, monkeypatch)
    async with app.run_test() as pilot:
        panel = WorkspaceModal(app.core)
        app.push_screen(panel)
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        rows = panel.query_one("#files")
        assert rows.option_count == 3  # two models plus the IDS, not notes.md
        labels = " ".join(str(rows.get_option_at_index(i).prompt) for i in range(3))
        assert "IFC" in labels and "IDS" in labels

        await pilot.press("ctrl+u")
        assert panel._show_all is True


async def test_panel_filter_narrows_and_tab_checks(tmp_path: Path, project: Path, monkeypatch) -> None:
    app = _app(tmp_path, project, monkeypatch)
    async with app.run_test() as pilot:
        panel = WorkspaceModal(app.core)
        app.push_screen(panel)
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        rows = panel.query_one("#files")

        panel.query_one("#filter").value = "ids"
        await pilot.pause()
        assert rows.option_count == 1

        await pilot.press("tab")
        await pilot.pause()
        assert {p.name for p in panel._checked} == {"employer-requirements.ids"}


async def test_panel_check_all_and_clear_buttons(
    tmp_path: Path, project: Path, monkeypatch
) -> None:
    app = _app(tmp_path, project, monkeypatch)
    async with app.run_test() as pilot:
        panel = WorkspaceModal(app.core)
        app.push_screen(panel)
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

        await pilot.click("#select-all")
        await pilot.pause()
        assert len(panel._checked) == 3

        await pilot.click("#select-none")
        await pilot.pause()
        assert panel._checked == set()


async def test_panel_tab_checks_and_enter_opens(
    tmp_path: Path, project: Path, monkeypatch
) -> None:
    """Tab builds the selection; Enter opens it."""
    app = _app(tmp_path, project, monkeypatch)
    async with app.run_test() as pilot:
        panel = WorkspaceModal(app.core)
        result: list = []
        app.push_screen(panel, callback=result.append)
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        rows = panel.query_one("#files")

        await pilot.press("tab")
        await pilot.pause()
        assert len(panel._checked) == 1
        assert rows.highlighted == 1  # checking steps down
        assert not result  # still open, still picking

        await pilot.press("tab")
        await pilot.pause()
        assert len(panel._checked) == 2
        assert not result

        await pilot.press("up", "tab")  # back onto the first row: uncheck
        await pilot.pause()
        assert len(panel._checked) == 1

        await pilot.press("enter")
        await pilot.pause()
        assert len(result) == 1
        assert len(result[0].models) + len(result[0].files) == 1


async def test_panel_applies_everything_checked(tmp_path: Path, project: Path, monkeypatch) -> None:
    app = _app(tmp_path, project, monkeypatch)
    async with app.run_test() as pilot:
        panel = WorkspaceModal(app.core)
        result: list = []
        app.push_screen(panel, callback=result.append)
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

        await pilot.press("ctrl+a")  # check everything visible
        await pilot.pause()
        panel.action_apply()
        await pilot.pause()

        choice = result[0]
        assert len(choice.models) == 2
        assert [p.name for p in choice.models] == [
            "tower-architecture.ifc",
            "tower-structure.ifc",
        ]
        assert [p.name for p in choice.files] == ["employer-requirements.ids"]


async def test_panel_with_nothing_checked_behaves_like_the_single_picker(
    tmp_path: Path, project: Path, monkeypatch
) -> None:
    app = _app(tmp_path, project, monkeypatch)
    async with app.run_test() as pilot:
        panel = WorkspaceModal(app.core)
        result: list = []
        app.push_screen(panel, callback=result.append)
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

        await pilot.press("enter")  # nothing checked: the highlighted row wins
        await pilot.pause()
        choice = result[0]
        assert len(choice.models) + len(choice.files) == 1
