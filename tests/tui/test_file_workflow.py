"""File navigation recovery, scope previews, and mixed workspace outcomes."""

from __future__ import annotations

import shutil

import pytest
from textual.widgets import Input, Static

from ifc_console.tui import commands
from ifc_console.tui.app import IfcConsoleApp
from ifc_console.tui.console import ConsoleScreen
from ifc_console.tui.launcher import FilePickerModal
from ifc_console.tui.workspace import WorkspaceChoice, WorkspaceModal
from tests.tui.test_commands import FakeConsole

pytestmark = pytest.mark.asyncio


async def test_file_directory_does_not_parse_or_request_discard(core, tmp_path, monkeypatch):
    directory = tmp_path / "models with spaces"
    directory.mkdir()
    console = FakeConsole(core)
    core.session.dirty = True
    before = list(core.allowed_dirs)

    async def unexpected(*args, **kwargs):
        pytest.fail("directory input must not parse a model or request discarded edits")

    monkeypatch.setattr(core, "open_model", unexpected)
    monkeypatch.setattr(console, "confirm", unexpected)
    await commands.dispatch(console, f'/file "{directory}"')

    assert f'/workspace "{directory}"' in console.text
    assert core.allowed_dirs == before
    assert core.session.dirty


@pytest.mark.parametrize("modal_type", [FilePickerModal, WorkspaceModal])
async def test_empty_filter_stays_open_and_clear_recovers(core, work_model, modal_type):
    app = IfcConsoleApp(core, autostart=False)
    async with app.run_test() as pilot:
        panel = modal_type(core)
        results = []
        app.push_screen(panel, callback=results.append)
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        field = panel.query_one("#filter", Input)
        field.value = "no-such-model-xyz"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen is panel
        assert results == []
        assert panel._visible == []

        await pilot.press("ctrl+l")
        await pilot.pause()
        assert field.value == ""
        assert panel._visible
        await pilot.press("escape")
        await pilot.pause()
        assert results == [None]
        assert not core.session.loaded


async def test_picker_accepts_a_quoted_direct_path_outside_discovery(core, tmp_path, work_model):
    nested = tmp_path / "one" / "two" / "three"
    nested.mkdir(parents=True)
    target = nested / "model with spaces.ifc"
    shutil.copy2(work_model, target)
    app = IfcConsoleApp(core, autostart=False)
    async with app.run_test() as pilot:
        panel = FilePickerModal(core)
        results = []
        app.push_screen(panel, callback=results.append)
        await pilot.pause()
        assert target not in panel._visible
        panel.query_one("#filter", Input).value = f'"{target}"'
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert results == [target.resolve()]


async def test_workspace_preview_and_cancel_preserve_live_state(core, tmp_path, work_model):
    await core.open_model(work_model)
    core.session.dirty = True
    core.viewer.selection = ["selected-global-id"]
    core.workspace.primary_root = tmp_path
    core.workspace.scan()
    before_index = list(core.workspace.entries)
    before_scan = core.workspace.scanned_at
    before_dirs = list(core.allowed_dirs)
    before_session = core.session
    preview = tmp_path / "different project"
    preview.mkdir()
    shutil.copy2(work_model, preview / "other.ifc")
    app = IfcConsoleApp(core, autostart=False)
    async with app.run_test() as pilot:
        screen = app.screen
        worker = app.run_worker(screen.open_workspace_panel(root=preview))
        await pilot.pause()
        panel = app.screen
        assert isinstance(panel, WorkspaceModal)
        for _ in range(20):
            if panel._entries:
                break
            await pilot.pause(0.05)
        await pilot.pause()
        assert [entry.path.name for entry in panel._entries] == ["other.ifc"]
        assert str(preview) in str(panel.query_one("#roots", Static).render())
        assert core.allowed_dirs == before_dirs
        assert core.workspace.primary_root == tmp_path
        assert core.workspace.entries == before_index
        assert core.workspace.scanned_at == before_scan
        await pilot.press("escape")
        assert await worker.wait() is False
        assert core.allowed_dirs == before_dirs
        assert core.workspace.primary_root == tmp_path
        assert core.workspace.entries == before_index
        assert core.session is before_session and core.session.dirty
        assert core.viewer.selection == ["selected-global-id"]


async def test_workspace_accept_commits_scope_then_attaches_without_replacing(
    core, tmp_path, work_model
):
    await core.open_model(work_model)
    original = core.session
    original.dirty = True
    target_root = tmp_path / "accepted project"
    target_root.mkdir()
    target = target_root / "other.ifc"
    shutil.copy2(work_model, target)

    class AcceptedConsole(FakeConsole):
        async def push_screen_wait(self, panel):
            assert target_root not in core.allowed_dirs
            assert panel.root == target_root
            return WorkspaceChoice(models=[target])

    console = AcceptedConsole(core)
    assert await ConsoleScreen.open_workspace_panel(console, root=target_root)
    assert core.workspace.primary_root == target_root
    assert target_root in core.allowed_dirs
    assert core.session is original and original.dirty
    assert core.models.require("other").read_only
    assert "0 loaded · 1 attached · 0 skipped · 0 failed" in console.text


async def test_mixed_selection_reports_outcomes_and_document_path_status(
    core, tmp_path, work_model
):
    console = FakeConsole(core)
    second = tmp_path / "second.ifc"
    shutil.copy2(work_model, second)
    broken = tmp_path / "broken.ifc"
    broken.write_text("invalid IFC", encoding="utf-8")
    document = tmp_path / "catalogue.pdf"
    document.write_bytes(b"%PDF-1.4\n")
    await commands.apply_workspace_choice(
        console,
        WorkspaceChoice(
            models=[broken, work_model, work_model, second],
            files=[document, document],
        ),
    )
    assert core.session.path == work_model
    assert core.models.require("second").read_only
    assert "1 loaded · 2 attached · 2 skipped · 1 failed" in console.text
    assert "not indexed or analysed" in console.text
    assert "Codex conversation" in console.text
    assert "or add it to Agent Content" in console.text


async def test_workspace_original_path_does_not_duplicate_or_replace_dirty_copy(core, work_model):
    await core.open_model(work_model)
    copy = await core.enter_edit_mode(by="test")
    assert copy is not None
    core.session.dirty = True
    original_session = core.session
    console = FakeConsole(core)

    await commands.apply_workspace_choice(console, WorkspaceChoice(models=[work_model]))

    assert core.session is original_session and core.session.dirty
    assert core.session.path == copy.path
    assert len(core.models.sessions) == 1
    assert "0 loaded · 0 attached · 1 skipped · 0 failed" in console.text
    panel = WorkspaceModal(core)
    original_entry = next(entry for entry in panel._scan() if entry.path == work_model)
    assert "active IFC" in panel._row(original_entry)


async def test_workspace_roles_and_optional_consumers_are_explicit(
    core, tmp_path, work_model, monkeypatch
):
    await core.open_model(work_model)
    attached = tmp_path / "reference.ifc"
    shutil.copy2(work_model, attached)
    await core.open_model(attached, attach=True)
    ids = tmp_path / "checks.ids"
    ids.write_text("<ids/>", encoding="utf-8")
    document = tmp_path / "catalogue.pdf"
    document.write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr("ifc_console.tui.workspace.importlib.util.find_spec", lambda name: None)
    app = IfcConsoleApp(core, autostart=False)
    async with app.run_test() as pilot:
        panel = WorkspaceModal(core)
        app.push_screen(panel)
        await pilot.pause()
        await panel.workers.wait_for_complete()
        await pilot.pause()
        rows = " ".join(panel._row(entry) for entry in panel._visible)
        assert "active IFC" in rows and "attached IFC" in rows
        assert "validate_ids" in rows and "requires validation extra" in rows
        assert "catalogue.pdf" not in rows
        await pilot.press("ctrl+u")
        rows = " ".join(panel._row(entry) for entry in panel._visible)
        assert "document path only" in rows
        assert "attach in Codex or Agent Content" in rows
