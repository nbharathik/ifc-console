"""Settings schema, on-disk layout, and precedence merge.

Precedence: defaults < user file < project file < project local file < env < flags.
Project files may only set the "safe subset" (PROJECT_SAFE_KEYS): a cloned repo
must not be able to widen allowed_dirs or enable system access.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

ENV_PREFIX = "IFC_CONSOLE_"


def user_dir() -> Path:
    """~/.ifc-console, overridable via IFC_CONSOLE_HOME (mainly for tests)."""
    override = os.environ.get("IFC_CONSOLE_HOME")
    return Path(override) if override else Path.home() / ".ifc-console"


class ModeSettings(BaseModel):
    default: str = Field(default="ask", pattern="^(ask|edit)$")


class ServerSettings(BaseModel):
    port: int = Field(default=8383, ge=1024, le=65535)
    # One token per machine, stored in ~/.ifc-console/token, so clients are
    # configured once and keep working across restarts (like wiring up a
    # Blender addon once). Set false for a fresh token every run.
    persistent_token: bool = True
    token_in_config_snippets: bool = True


class ExecSettings(BaseModel):
    timeout_seconds: float = Field(default=30.0, gt=0)
    output_char_limit: int = Field(default=40_000, ge=1_000)
    allow_system_access: bool = False
    system_modules_extra: list[str] = Field(default_factory=list)


class FilesSettings(BaseModel):
    allowed_dirs: list[str] = Field(default_factory=list)
    backup_retention: int = Field(default=20, ge=1)
    follow_symlinks: bool = False
    # Refuse to open files above this budget instead of risking an OOM crash;
    # 0 disables the guard.
    max_open_mb: int = Field(default=4096, ge=0)


class ViewerSettings(BaseModel):
    enabled_default: bool = False
    max_model_mb: int = Field(default=200, ge=1)


class RecentsSettings(BaseModel):
    max: int = Field(default=20, ge=1)


class SessionsSettings(BaseModel):
    retention: int = Field(default=50, ge=1)


class LoggingSettings(BaseModel):
    level: str = Field(default="info", pattern="^(debug|info|warning|error)$")
    file_enabled: bool = True


class TuiSettings(BaseModel):
    theme: str = Field(default="dark", pattern="^(dark|light|auto)$")


class Settings(BaseModel):
    settings_version: int = 1
    mode: ModeSettings = Field(default_factory=ModeSettings)
    server: ServerSettings = Field(default_factory=ServerSettings)
    exec: ExecSettings = Field(default_factory=ExecSettings)
    files: FilesSettings = Field(default_factory=FilesSettings)
    viewer: ViewerSettings = Field(default_factory=ViewerSettings)
    recents: RecentsSettings = Field(default_factory=RecentsSettings)
    sessions: SessionsSettings = Field(default_factory=SessionsSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    tui: TuiSettings = Field(default_factory=TuiSettings)


# Keys project-level files may set: nothing here can widen file access,
# enable system access, or weaken authentication.
PROJECT_SAFE_KEYS = {
    "mode.default",
    "server.port",
    "exec.timeout_seconds",
    "exec.output_char_limit",
    "viewer.enabled_default",
    "viewer.max_model_mb",
    "logging.level",
    "tui.theme",
}

_META_KEYS = {"$schema", "_readme", "settings_version"}


def _flatten(d: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, f"{key}."))
        else:
            out[key] = v
    return out


def _set_dot(d: dict[str, Any], key: str, value: Any) -> None:
    parts = key.split(".")
    cur = d
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
        if not isinstance(cur, dict):
            raise ValueError(f"cannot set {key!r}: {p!r} is not a section")
    cur[parts[-1]] = value


def _get_dot(d: dict[str, Any], key: str) -> Any:
    cur: Any = d
    for p in key.split("."):
        if not isinstance(cur, dict) or p not in cur:
            raise KeyError(key)
        cur = cur[p]
    return cur


def _parse_env_value(raw: str) -> Any:
    low = raw.strip().lower()
    if low in ("true", "1", "yes", "on"):
        return True
    if low in ("false", "0", "no", "off"):
        return False
    try:
        return json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return raw


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


class SettingsStore:
    """Merged settings with per-key provenance."""

    def __init__(
        self,
        home: Path | None = None,
        project_dir: Path | None = None,
        env: dict[str, str] | None = None,
        flag_overrides: dict[str, Any] | None = None,
    ) -> None:
        self.home = home or user_dir()
        self.project_dir = project_dir or Path.cwd()
        self._env = dict(os.environ) if env is None else env
        self._flags = dict(flag_overrides or {})
        self.settings = Settings()
        self.provenance: dict[str, str] = {}
        self.warnings: list[str] = []
        self.reload()

    # -- paths ------------------------------------------------------------
    @property
    def user_file(self) -> Path:
        return self.home / "settings.json"

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def sessions_dir(self) -> Path:
        return self.home / "sessions"

    @property
    def backups_dir(self) -> Path:
        return self.home / "backups"

    @property
    def recents_file(self) -> Path:
        return self.home / "recents.json"

    @property
    def token_file(self) -> Path:
        return self.home / "token"

    # -- server token -------------------------------------------------------
    def load_server_token(self) -> str:
        """The machine's persistent bearer token; created on first use.

        Keeping it stable across runs means MCP clients are configured once.
        Rotate with `ifc-console token rotate` if it ever leaks.
        """
        try:
            token = self.token_file.read_text(encoding="utf-8").strip()
            if len(token) >= 16 and all(c in "0123456789abcdef" for c in token):
                return token
        except OSError:
            pass
        return self.rotate_server_token()

    def rotate_server_token(self) -> str:
        token = secrets.token_hex(16)
        self.home.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text(token + "\n", encoding="utf-8")
        # Owner-only on POSIX; Windows user-profile ACLs already scope it.
        with contextlib.suppress(OSError):
            os.chmod(self.token_file, 0o600)
        return token

    def ensure_dirs(self) -> None:
        for d in (self.home, self.logs_dir, self.sessions_dir, self.backups_dir):
            d.mkdir(parents=True, exist_ok=True)
        if not self.user_file.exists():
            payload = {
                "_readme": "ifc-console user settings; `ifc-console settings list` shows all keys",
                "settings_version": 1,
            }
            _atomic_write_json(self.user_file, payload)

    # -- merge ------------------------------------------------------------
    def _read_layer(self, path: Path, label: str) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self.warnings.append(f"{label}: unreadable settings file {path}: {exc}")
            return {}
        if not isinstance(raw, dict):
            self.warnings.append(f"{label}: {path} is not a JSON object; ignored")
            return {}
        for k in _META_KEYS:
            raw.pop(k, None)
        return raw

    def reload(self) -> None:
        self.warnings = []
        merged = Settings().model_dump()
        provenance = {k: "default" for k in _flatten(merged)}
        known = set(provenance)

        def apply(flat: dict[str, Any], label: str, safe_only: bool) -> None:
            for key, value in flat.items():
                if key not in known:
                    self.warnings.append(f"{label}: unknown setting {key!r} ignored")
                    continue
                if safe_only and key not in PROJECT_SAFE_KEYS:
                    self.warnings.append(
                        f"{label}: {key!r} may not be set from project files; ignored"
                    )
                    continue
                _set_dot(merged, key, value)
                provenance[key] = label

        apply(_flatten(self._read_layer(self.user_file, "user")), "user", safe_only=False)
        proj = self.project_dir / ".ifc-console" / "settings.json"
        proj_local = self.project_dir / ".ifc-console" / "settings.local.json"
        apply(_flatten(self._read_layer(proj, "project")), "project", safe_only=True)
        apply(
            _flatten(self._read_layer(proj_local, "project-local")),
            "project-local",
            safe_only=True,
        )

        env_flat = {}
        for key in known:
            env_name = ENV_PREFIX + key.replace(".", "_").upper()
            if env_name in self._env:
                env_flat[key] = _parse_env_value(self._env[env_name])
        apply(env_flat, "env", safe_only=False)
        apply(dict(self._flags), "flag", safe_only=False)

        try:
            self.settings = Settings.model_validate(merged)
        except ValidationError as exc:
            self.warnings.append(f"invalid settings ({exc.error_count()} errors); using defaults")
            self.settings = Settings()
            provenance = {k: "default" for k in known}
        self.provenance = provenance

    # -- user-file editing (`ifc-console settings set/unset`) -----------------
    def set_user(self, key: str, raw_value: str) -> Any:
        known = set(_flatten(Settings().model_dump()))
        if key not in known:
            raise KeyError(f"unknown setting {key!r}")
        value = _parse_env_value(raw_value)
        current = self._read_layer(self.user_file, "user")
        _set_dot(current, key, value)
        base = Settings().model_dump()
        for k, v in _flatten(current).items():
            if k in known:
                _set_dot(base, k, v)
        Settings.model_validate(base)  # raises on bad value before writing
        current["settings_version"] = 1
        _atomic_write_json(self.user_file, current)
        self.reload()
        return value

    def unset_user(self, key: str) -> None:
        current = self._read_layer(self.user_file, "user")
        parts = key.split(".")
        cur: Any = current
        for p in parts[:-1]:
            if not isinstance(cur, dict) or p not in cur:
                return
            cur = cur[p]
        if isinstance(cur, dict):
            cur.pop(parts[-1], None)
        current["settings_version"] = 1
        _atomic_write_json(self.user_file, current)
        self.reload()

    def get(self, key: str) -> Any:
        return _get_dot(self.settings.model_dump(), key)

    def flat(self) -> dict[str, Any]:
        return _flatten(self.settings.model_dump())
