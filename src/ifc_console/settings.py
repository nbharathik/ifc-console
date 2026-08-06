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
import time
from collections.abc import Iterator
from contextlib import contextmanager
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


class SandboxSettings(BaseModel):
    """Where generated code runs. See docs/sandbox.md."""

    # auto: sandbox whenever it can, fall back to in-process guards otherwise.
    # strict: refuse the run instead of falling back. off: never sandbox.
    mode: str = Field(default="auto", pattern="^(auto|strict|off)$")
    memory_mb: int = Field(default=2048, ge=256)
    # Models above this are not copied into the sandbox; a second copy of a
    # very large file costs more than the isolation is worth.
    max_model_mb: int = Field(default=512, ge=0)
    startup_timeout: float = Field(default=120.0, gt=0)
    load_timeout: float = Field(default=600.0, gt=0)
    # Start the worker when a model loads, so the first code run is not the
    # one that pays for it. Off by default: it holds a second copy of the
    # model, which is not worth it for sessions that never run code.
    warm_on_load: bool = False


class FilesSettings(BaseModel):
    allowed_dirs: list[str] = Field(default_factory=list)
    backup_retention: int = Field(default=20, ge=1)
    follow_symlinks: bool = False
    # Refuse to open files above this budget instead of risking an OOM crash;
    # 0 disables the guard.
    max_open_mb: int = Field(default=4096, ge=0)


class WorkspaceSettings(BaseModel):
    """The optional multi-file mode. One active model stays the default."""

    enabled: bool = True  # indexing only; nothing is loaded until asked
    # Models held in memory at once, active included. 1 restores strict
    # single-model behaviour.
    max_resident: int = Field(default=3, ge=1, le=16)
    max_total_mb: int = Field(default=6144, ge=0)
    scan_depth: int = Field(default=3, ge=1, le=10)
    scan_cap: int = Field(default=10_000, ge=100)


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
    sandbox: SandboxSettings = Field(default_factory=SandboxSettings)
    files: FilesSettings = Field(default_factory=FilesSettings)
    workspace: WorkspaceSettings = Field(default_factory=WorkspaceSettings)
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


def _coerce(current: Any, raw: str) -> Any:
    """Parse a text value for the field it is going into.

    Without the target type, `sandbox.mode off` would parse as the boolean
    False and fail validation. String-valued settings take the text as typed.
    """
    if isinstance(current, str):
        return raw.strip()
    return _parse_env_value(raw)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _valid_token(path: Path) -> str | None:
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if len(token) >= 16 and all(c in "0123456789abcdef" for c in token):
        return token
    return None


@contextmanager
def _token_lock(path: Path) -> Iterator[None]:
    lock_path = path.with_name(path.name + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + 5
    fd: int | None = None
    while fd is None:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        # Windows reports a concurrent unlink as EACCES (delete pending),
        # not EEXIST; both mean the lock is (or just was) held, so retry.
        except (FileExistsError, PermissionError):
            with contextlib.suppress(OSError):
                if time.time() - lock_path.stat().st_mtime > 30:
                    # rename first so two waiters cannot both judge the lock
                    # stale and the loser unlink the winner's fresh lock
                    stale = lock_path.with_name(lock_path.name + ".stale")
                    os.replace(lock_path, stale)
                    stale.unlink()
                    continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting for {lock_path}") from None
            time.sleep(0.01)
    try:
        yield
    finally:
        os.close(fd)
        with contextlib.suppress(OSError):
            lock_path.unlink()


class SettingsStore:
    """Merged settings with per-key provenance."""

    def __init__(
        self,
        home: Path | None = None,
        project_dir: Path | None = None,
        env: dict[str, str] | None = None,
        flag_overrides: dict[str, Any] | None = None,
        include_project: bool = True,
    ) -> None:
        self.home = home or user_dir()
        self.project_dir = project_dir or Path.cwd()
        self._env = dict(os.environ) if env is None else env
        self._flags = dict(flag_overrides or {})
        self.include_project = include_project
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
        with _token_lock(self.token_file):
            token = _valid_token(self.token_file)
            if token is not None:
                return token
            return self._write_server_token()

    def rotate_server_token(self) -> str:
        with _token_lock(self.token_file):
            return self._write_server_token()

    def _write_server_token(self) -> str:
        token = secrets.token_hex(16)
        self.home.mkdir(parents=True, exist_ok=True)
        tmp = self.token_file.with_name(
            f".{self.token_file.name}.{secrets.token_hex(6)}.tmp"
        )
        try:
            # owner-only from the first byte, not only after the chmod below
            tmp_fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                fh.write(token + "\n")
            os.replace(tmp, self.token_file)
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink()
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
        if self.include_project:
            proj = self.project_dir / ".ifc-console" / "settings.json"
            proj_local = self.project_dir / ".ifc-console" / "settings.local.json"
            apply(_flatten(self._read_layer(proj, "project")), "project", safe_only=True)
            apply(
                _flatten(self._read_layer(proj_local, "project-local")),
                "project-local",
                safe_only=True,
            )

        flat_defaults = _flatten(Settings().model_dump())
        env_flat = {}
        for key in known:
            env_name = ENV_PREFIX + key.replace(".", "_").upper()
            if env_name in self._env:
                env_flat[key] = _coerce(flat_defaults.get(key), self._env[env_name])
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
        defaults = _flatten(Settings().model_dump())
        known = set(defaults)
        if key not in known:
            raise KeyError(f"unknown setting {key!r}")
        value = _coerce(defaults[key], raw_value)
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
