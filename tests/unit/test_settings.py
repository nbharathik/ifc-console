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
    assert store.settings.files.allow_ai_save is False


def test_user_file_overrides_default(tmp_path: Path) -> None:
    home = tmp_path / "h"
    _write(home / "settings.json", {"server": {"port": 9001}})
    store = SettingsStore(home=home, project_dir=tmp_path, env={})
    assert store.settings.server.port == 9001
    assert store.provenance["server.port"] == "user"


def test_env_overrides_user(tmp_path: Path) -> None:
    home = tmp_path / "h"
    _write(home / "settings.json", {"server": {"port": 9001}})
    store = SettingsStore(home=home, project_dir=tmp_path, env={"IFC_CONSOLE_SERVER_PORT": "9002"})
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


def test_include_project_false_ignores_project_layers(tmp_path: Path) -> None:
    # The bridge runs with cwd inside arbitrary repos: a cloned project must
    # not be able to redirect the machine token to another port.
    _write(tmp_path / ".ifc-console" / "settings.json", {"server": {"port": 31337}})
    store = SettingsStore(home=tmp_path / "h", project_dir=tmp_path, env={}, include_project=False)
    assert store.settings.server.port == 8383
    assert store.provenance["server.port"] == "default"


def test_project_file_may_set_safe_key(tmp_path: Path) -> None:
    _write(tmp_path / ".ifc-console" / "settings.json", {"tui": {"theme": "light"}})
    store = SettingsStore(home=tmp_path / "h", project_dir=tmp_path, env={})
    assert store.settings.tui.theme == "light"
    assert store.provenance["tui.theme"] == "project"


def test_project_file_cannot_expand_exposure_logging_or_resource_budgets(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / ".ifc-console" / "settings.json",
        {
            "exec": {"timeout_seconds": 3600, "output_char_limit": 9_000_000},
            "viewer": {"enabled_default": True, "max_model_mb": 999_999},
            "logging": {"level": "debug"},
        },
    )
    store = SettingsStore(home=tmp_path / "h", project_dir=tmp_path, env={})
    assert store.settings.exec.timeout_seconds == 30
    assert store.settings.exec.output_char_limit == 40_000
    assert store.settings.viewer.enabled_default is False
    assert store.settings.viewer.max_model_mb == 200
    assert store.settings.logging.level == "info"
    for key in (
        "exec.timeout_seconds",
        "exec.output_char_limit",
        "viewer.enabled_default",
        "viewer.max_model_mb",
        "logging.level",
    ):
        assert any(key in warning for warning in store.warnings)


def test_project_file_may_not_widen_allowed_dirs(tmp_path: Path) -> None:
    _write(tmp_path / ".ifc-console" / "settings.json", {"files": {"allowed_dirs": ["/etc"]}})
    store = SettingsStore(home=tmp_path / "h", project_dir=tmp_path, env={})
    assert store.settings.files.allowed_dirs == []  # rejected
    assert any("allowed_dirs" in w for w in store.warnings)


def test_project_file_may_not_enable_system_access(tmp_path: Path) -> None:
    _write(tmp_path / ".ifc-console" / "settings.json", {"exec": {"allow_system_access": True}})
    store = SettingsStore(home=tmp_path / "h", project_dir=tmp_path, env={})
    assert store.settings.exec.allow_system_access is False


def test_project_file_may_not_enable_ai_saving(tmp_path: Path) -> None:
    _write(tmp_path / ".ifc-console" / "settings.json", {"files": {"allow_ai_save": True}})
    store = SettingsStore(home=tmp_path / "h", project_dir=tmp_path, env={})
    assert store.settings.files.allow_ai_save is False
    assert store.provenance["files.allow_ai_save"] == "default"
    assert any("files.allow_ai_save" in warning for warning in store.warnings)


def test_project_file_may_not_enable_edit_mode(tmp_path: Path) -> None:
    _write(tmp_path / ".ifc-console" / "settings.json", {"mode": {"default": "edit"}})
    store = SettingsStore(home=tmp_path / "h", project_dir=tmp_path, env={})
    assert store.settings.mode.default == "ask"
    assert store.provenance["mode.default"] == "default"
    assert any("mode.default" in warning for warning in store.warnings)


def test_project_files_cannot_redirect_the_server_port(tmp_path: Path) -> None:
    _write(tmp_path / ".ifc-console" / "settings.json", {"server": {"port": 9100}})
    _write(tmp_path / ".ifc-console" / "settings.local.json", {"server": {"port": 9200}})
    store = SettingsStore(home=tmp_path / "h", project_dir=tmp_path, env={})
    assert store.settings.server.port == 8383
    assert store.provenance["server.port"] == "default"
    assert sum("server.port" in warning for warning in store.warnings) == 2


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


def test_output_limit_cannot_exceed_the_sandbox_protocol_budget(tmp_path: Path) -> None:
    _write(
        tmp_path / "h" / "settings.json",
        {"exec": {"output_char_limit": 1_000_001}},
    )
    store = SettingsStore(home=tmp_path / "h", project_dir=tmp_path, env={})
    assert store.settings.exec.output_char_limit == 40_000
    assert any("invalid settings" in warning for warning in store.warnings)


def test_string_settings_keep_boolean_looking_words(tmp_path: Path) -> None:
    """`off`, `on`, `no` are values, not booleans, when the field is a string."""
    home = tmp_path / "h"
    store = SettingsStore(home=home, project_dir=tmp_path, env={})
    store.ensure_dirs()
    store.set_user("sandbox.mode", "off")
    assert store.settings.sandbox.mode == "off"
    reread = SettingsStore(home=home, project_dir=tmp_path, env={})
    assert reread.settings.sandbox.mode == "off"


def test_env_layer_coerces_per_field_type(tmp_path: Path) -> None:
    store = SettingsStore(
        home=tmp_path / "h",
        project_dir=tmp_path,
        env={
            "IFC_CONSOLE_SANDBOX_MODE": "off",
            "IFC_CONSOLE_SANDBOX_WARM_ON_LOAD": "on",
            "IFC_CONSOLE_SANDBOX_MEMORY_MB": "4096",
        },
    )
    assert store.settings.sandbox.mode == "off"
    assert store.settings.sandbox.warm_on_load is True
    assert store.settings.sandbox.memory_mb == 4096


def test_project_files_cannot_weaken_the_sandbox(tmp_path: Path) -> None:
    _write(tmp_path / ".ifc-console" / "settings.json", {"sandbox": {"mode": "off"}})
    store = SettingsStore(home=tmp_path / "h", project_dir=tmp_path, env={})
    assert store.settings.sandbox.mode == "auto"
    assert any("sandbox.mode" in w for w in store.warnings)


def test_project_files_cannot_enable_plugins(tmp_path: Path) -> None:
    _write(
        tmp_path / ".ifc-console" / "settings.json",
        {"plugins": {"enabled": True, "allow": ["untrusted"]}},
    )
    store = SettingsStore(home=tmp_path / "h", project_dir=tmp_path, env={})
    assert store.settings.plugins.enabled is False
    assert store.settings.plugins.allow == []
    assert any("plugins.enabled" in warning for warning in store.warnings)


def test_oversized_project_settings_are_ignored_without_unbounded_reads(tmp_path: Path) -> None:
    path = tmp_path / ".ifc-console" / "settings.json"
    path.parent.mkdir()
    path.write_text('{"tui":{"theme":"light"},"padding":"' + "x" * 1_048_576 + '"}')
    store = SettingsStore(home=tmp_path / "h", project_dir=tmp_path, env={})
    assert store.settings.tui.theme == "blue"
    assert any("byte limit" in warning for warning in store.warnings)


def test_deeply_nested_project_settings_are_ignored(tmp_path: Path) -> None:
    nested: dict = {"value": "light"}
    for index in range(40):
        nested = {f"level_{index}": nested}
    _write(tmp_path / ".ifc-console" / "settings.json", nested)
    store = SettingsStore(home=tmp_path / "h", project_dir=tmp_path, env={})
    assert store.settings.tui.theme == "blue"
    assert any("nested too deeply" in warning for warning in store.warnings)


def test_unknown_key_warns(tmp_path: Path) -> None:
    _write(tmp_path / "h" / "settings.json", {"nope": {"x": 1}})
    store = SettingsStore(home=tmp_path / "h", project_dir=tmp_path, env={})
    assert any("nope" in w for w in store.warnings)
