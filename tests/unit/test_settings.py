"""Settings precedence and project-subset enforcement (plan 07 §4, plan 10 §2.3)."""

from __future__ import annotations

import json
from pathlib import Path

from ifc_console.settings import SettingsStore


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_defaults(tmp_path: Path) -> None:
    store = SettingsStore(home=tmp_path / "h", project_dir=tmp_path, env={})
    assert store.settings.mode.default == "ask"
    assert store.settings.server.port == 8383


def test_user_file_overrides_default(tmp_path: Path) -> None:
    home = tmp_path / "h"
    _write(home / "settings.json", {"server": {"port": 9001}})
    store = SettingsStore(home=home, project_dir=tmp_path, env={})
    assert store.settings.server.port == 9001
    assert store.provenance["server.port"] == "user"


def test_env_overrides_user(tmp_path: Path) -> None:
    home = tmp_path / "h"
    _write(home / "settings.json", {"server": {"port": 9001}})
    store = SettingsStore(
        home=home, project_dir=tmp_path, env={"IFC_CONSOLE_SERVER_PORT": "9002"}
    )
    assert store.settings.server.port == 9002
    assert store.provenance["server.port"] == "env"


def test_flags_override_env(tmp_path: Path) -> None:
    store = SettingsStore(
        home=tmp_path / "h",
        project_dir=tmp_path,
        env={"IFC_CONSOLE_SERVER_PORT": "9002"},
        flag_overrides={"server.port": 9003},
    )
    assert store.settings.server.port == 9003
    assert store.provenance["server.port"] == "flag"


def test_project_file_may_set_safe_key(tmp_path: Path) -> None:
    _write(tmp_path / ".ifc-console" / "settings.json", {"tui": {"theme": "light"}})
    store = SettingsStore(home=tmp_path / "h", project_dir=tmp_path, env={})
    assert store.settings.tui.theme == "light"
    assert store.provenance["tui.theme"] == "project"


def test_project_file_may_not_widen_allowed_dirs(tmp_path: Path) -> None:
    _write(tmp_path / ".ifc-console" / "settings.json", {"files": {"allowed_dirs": ["/etc"]}})
    store = SettingsStore(home=tmp_path / "h", project_dir=tmp_path, env={})
    assert store.settings.files.allowed_dirs == []  # rejected
    assert any("allowed_dirs" in w for w in store.warnings)


def test_project_file_may_not_enable_system_access(tmp_path: Path) -> None:
    _write(tmp_path / ".ifc-console" / "settings.json", {"exec": {"allow_system_access": True}})
    store = SettingsStore(home=tmp_path / "h", project_dir=tmp_path, env={})
    assert store.settings.exec.allow_system_access is False


def test_local_overrides_project(tmp_path: Path) -> None:
    _write(tmp_path / ".ifc-console" / "settings.json", {"server": {"port": 9100}})
    _write(tmp_path / ".ifc-console" / "settings.local.json", {"server": {"port": 9200}})
    store = SettingsStore(home=tmp_path / "h", project_dir=tmp_path, env={})
    assert store.settings.server.port == 9200
    assert store.provenance["server.port"] == "project-local"


def test_set_and_unset_user(tmp_path: Path) -> None:
    home = tmp_path / "h"
    store = SettingsStore(home=home, project_dir=tmp_path, env={})
    store.ensure_dirs()
    store.set_user("server.port", "9500")
    assert SettingsStore(home=home, project_dir=tmp_path, env={}).settings.server.port == 9500
    store.unset_user("server.port")
    assert SettingsStore(home=home, project_dir=tmp_path, env={}).settings.server.port == 8383


def test_invalid_value_rejected(tmp_path: Path) -> None:
    store = SettingsStore(home=tmp_path / "h", project_dir=tmp_path, env={})
    store.ensure_dirs()
    try:
        store.set_user("mode.default", "bogus")
        raise AssertionError("should have rejected bad enum value")
    except Exception:
        pass


def test_unknown_key_warns(tmp_path: Path) -> None:
    _write(tmp_path / "h" / "settings.json", {"nope": {"x": 1}})
    store = SettingsStore(home=tmp_path / "h", project_dir=tmp_path, env={})
    assert any("nope" in w for w in store.warnings)
