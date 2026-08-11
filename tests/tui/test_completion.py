"""Completion menu logic (pure; no Textual needed)."""

from __future__ import annotations

import shutil
from pathlib import Path

from ifc_console.app import AppCore
from ifc_console.settings import SettingsStore
from ifc_console.tui import commands, completion
from ifc_console.tui.launcher import discover_ifc_files


def inserts(state: completion.MenuState) -> list[str]:
    return [c.insert for c in state.candidates]


# ------------------------------------------------------------- command names
def test_slash_alone_lists_every_command(core) -> None:
    state = completion.complete("/", core)
    assert inserts(state) == sorted(f"/{name}" for name in commands.REGISTRY)


def test_prefix_filters_and_exact_name_ranks_first(core) -> None:
    state = completion.complete("/mo", core)
    assert set(inserts(state)) == {"/mode", "/models"}
    # "models" starts with "mode" too; the exactly-typed name must win the top
    state = completion.complete("/mode", core)
    assert inserts(state) == ["/mode", "/models"]


def test_command_flags_decide_enter_behavior(core) -> None:
    by_insert = {c.insert: c for c in completion.complete("/", core).candidates}
    # no argument worth completing: Enter runs it
    assert by_insert["/help"].terminal and not by_insert["/help"].advance
    assert by_insert["/status"].terminal
    # argument values exist: Enter advances to them instead of running
    assert not by_insert["/mode"].terminal and by_insert["/mode"].advance
    assert not by_insert["/use"].terminal
    # required freeform argument: advance, never auto-run
    assert not by_insert["/port"].terminal and by_insert["/port"].advance


def test_unknown_and_ambiguous_names_offer_nothing(core) -> None:
    assert completion.complete("/nope ", core).empty
    assert completion.complete("/mo x", core).empty  # /mode or /models? unclear
    assert completion.complete("hello", core).empty
    assert completion.complete("", core).empty


# ------------------------------------------------------------ argument menus
def test_mode_values_in_escalation_order(core) -> None:
    state = completion.complete("/mode ", core)
    assert inserts(state) == ["ask", "edit"]
    assert all(c.terminal for c in state.candidates)
    assert "current" in state.candidates[0].annotation  # default mode is ask


def test_mode_prefix_narrows_and_applies(core) -> None:
    state = completion.complete("/mode e", core)
    assert inserts(state) == ["edit"]
    assert state.apply(state.candidates[0]) == "/mode edit"


def test_unique_command_prefix_reaches_argument_menu(core) -> None:
    # dispatch() resolves unique prefixes, so completion does too
    state = completion.complete("/con cl", core)
    assert inserts(state) == ["claude-code", "claude-desktop"]


def test_viewer_offers_bare_open_plus_flags(core) -> None:
    state = completion.complete("/viewer ", core)
    assert [c.shown for c in state.candidates] == ["(open)", "off", "url"]
    bare = state.candidates[0]
    assert bare.terminal and state.apply(bare) == "/viewer "
    state = completion.complete("/viewer o", core)
    assert inserts(state) == ["off"]  # the bare row only shows for an empty token


def test_copy_targets(core) -> None:
    state = completion.complete("/copy ", core)
    assert inserts(state) == [
        "claude-code",
        "claude-desktop",
        "cursor",
        "vscode",
        "codex",
        "url",
        "viewer",
        "token",
    ]


def test_open_lists_discovered_files(core, minimal_ifc4_path: Path, tmp_path: Path) -> None:
    shutil.copy2(minimal_ifc4_path, tmp_path / "alpha.ifc")
    (tmp_path / "sub").mkdir()
    shutil.copy2(minimal_ifc4_path, tmp_path / "sub" / "beta.ifc")

    def files():
        return discover_ifc_files(core)

    state = completion.complete("/open ", core, files)
    shown = inserts(state)
    assert "alpha.ifc" in shown and str(Path("sub") / "beta.ifc") in shown
    assert all(c.terminal for c in state.candidates)

    state = completion.complete("/open bet", core, files)
    assert inserts(state) == [str(Path("sub") / "beta.ifc")]
    assert state.apply(state.candidates[0]) == f"/file {Path('sub') / 'beta.ifc'}"


def test_file_discovery_is_scoped_to_launch_directory(
    tmp_path: Path, minimal_ifc4_path: Path, monkeypatch
) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    nested = project / "models"
    nested.mkdir(parents=True)
    outside.mkdir()
    recent = project / "recent.ifc"
    other = nested / "other.ifc"
    outside_recent = outside / "outside.ifc"
    for target in (recent, other, outside_recent):
        shutil.copy2(minimal_ifc4_path, target)

    monkeypatch.chdir(project)
    scoped_core = AppCore(
        SettingsStore(home=tmp_path / "home", project_dir=project, env={}),
        extra_allowed_dirs=(outside,),
    )
    scoped_core.recents.touch(
        recent, size_bytes=recent.stat().st_size, schema="IFC4", mode="ask"
    )
    # This is newer in the global history and explicitly allowed for tool
    # access, but neither fact should make it appear in /file.
    scoped_core.recents.touch(
        outside_recent,
        size_bytes=outside_recent.stat().st_size,
        schema="IFC4",
        mode="ask",
    )

    entries = discover_ifc_files(scoped_core)

    assert [path for path, _detail in entries] == [recent.resolve(), other.resolve()]
    assert entries[0][1].startswith("recent ")
    assert "recent" not in entries[1][1]


def test_port_shows_only_a_hint(core) -> None:
    state = completion.complete("/port ", core)
    assert len(state.candidates) == 1 and state.candidates[0].disabled
    assert completion.complete("/port 9", core).empty


def test_settings_keys_then_values(core) -> None:
    state = completion.complete("/settings mode", core)
    keys = inserts(state)
    assert "mode.default" in keys
    key = next(c for c in state.candidates if c.insert == "mode.default")
    assert key.advance and not key.terminal
    assert "ask" in key.annotation  # shows the current value inline

    state = completion.complete("/settings mode.default ", core)
    assert inserts(state) == ["ask", "edit"]
    assert all(c.terminal for c in state.candidates)

    state = completion.complete("/settings server.persistent_token ", core)
    assert inserts(state) == ["true", "false"]
    assert state.candidates[0].annotation == "current"

    state = completion.complete("/settings viewer.max_model_mb ", core)
    assert len(state.candidates) == 1 and state.candidates[0].disabled
    assert completion.complete("/settings viewer.max_model_mb 1", core).empty
    assert completion.complete("/settings not.a.key x", core).empty


def test_apply_advances_with_trailing_space(core) -> None:
    state = completion.complete("/sett", core)
    assert state.apply(state.candidates[0]) == "/settings "
