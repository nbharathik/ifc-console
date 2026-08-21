"""The extension store: discover, install, and track agents and plugins.

Store v1 is a static catalog, no server: a schema-validated catalog.json on
GitHub, with a bundled seed as the offline fallback. Each installed agent
lives in its own environment (uv tool), so the console records what it
installed in ~/.ifc-console/extensions.json instead of importing anything.
Operation plugins keep their existing same-environment, deny-by-default path.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ifc_console.core.results import ToolError

CATALOG_URL = (
    "https://raw.githubusercontent.com/nbharathik/ifc-console-extensions/main/catalog.json"
)
_FETCH_TIMEOUT_S = 10
_RECORD_VERSION = 1
_NAME = re.compile(r"^[a-z][a-z0-9-]*$")


class ExtensionEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=_NAME.pattern, max_length=64)
    kind: Literal["agent", "plugin"]
    description: str = Field(max_length=300)
    package: str = Field(max_length=300)
    command: str | None = None
    requires_core: str | None = None
    homepage: str | None = None
    maintainer: str | None = None


class Catalog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    extensions: list[ExtensionEntry] = Field(default_factory=list)


# The offline seed: what ships in this repository. The published catalog at
# CATALOG_URL supersedes it whenever it is reachable.
SEED_CATALOG = Catalog(
    version=1,
    extensions=[
        ExtensionEntry(
            name="measure",
            kind="agent",
            description=(
                "Document-grounded measurement agent: company recipes, explicit "
                "measurement methods, viewer verification, cited reports."
            ),
            package="ifc-agent-measure",
            command="ifc-measure",
            requires_core=">=0.1.4,<0.3",
            homepage="https://github.com/nbharathik/ifc-console",
            maintainer="ifc-console",
        ),
        ExtensionEntry(
            name="company-checks",
            kind="plugin",
            description=(
                "Example operation plugin: a company validation rule that appears "
                "in every ifc-console interface."
            ),
            package="examples/plugins/company_checks",
            homepage="https://github.com/nbharathik/ifc-console",
            maintainer="ifc-console",
        ),
    ],
)


def fetch_catalog(url: str | None = None) -> tuple[Catalog, str]:
    """(catalog, source) from the published URL, a file path, or the seed."""
    target = url or CATALOG_URL
    if not target.startswith(("http://", "https://")):
        path = Path(target)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return Catalog.model_validate(payload), str(path)
        except (OSError, ValueError, ValidationError) as exc:
            raise ToolError(
                "INVALID_INPUT",
                f"catalog file {target} is unusable: {exc}",
                "Point --catalog at a catalog.json matching the published schema.",
            ) from exc
    try:
        from urllib.request import urlopen

        with urlopen(target, timeout=_FETCH_TIMEOUT_S) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8"))
        return Catalog.model_validate(payload), target
    except Exception:
        return SEED_CATALOG, "bundled seed (the published catalog was unreachable)"


class InstallRecord:
    """What `extensions install` put on this machine, by extension name."""

    def __init__(self, home: Path) -> None:
        self.path = home / "extensions.json"

    def load(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        installed = data.get("installed")
        return dict(installed) if isinstance(installed, dict) else {}

    def save(self, installed: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {"version": _RECORD_VERSION, "installed": installed},
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

    def add(self, name: str, entry: dict[str, Any]) -> None:
        installed = self.load()
        installed[name] = entry
        self.save(installed)

    def remove(self, name: str) -> dict[str, Any] | None:
        installed = self.load()
        entry = installed.pop(name, None)
        if entry is not None:
            self.save(installed)
        return entry


def resolve_entry(catalog: Catalog, name_or_spec: str) -> ExtensionEntry:
    """A catalog entry by name, or a synthetic one for a direct requirement."""
    for entry in catalog.extensions:
        if entry.name == name_or_spec:
            return entry
    if name_or_spec.startswith("git+") or "/" in name_or_spec or "==" in name_or_spec:
        stem = name_or_spec.rsplit("/", 1)[-1].removesuffix(".git").split("==")[0]
        short = stem.removeprefix("ifc-agent-").removeprefix("ifc-console-") or stem
        return ExtensionEntry(
            name=re.sub(r"[^a-z0-9-]", "-", short.lower()).strip("-") or "extension",
            kind="agent",
            description="installed directly by requirement",
            package=name_or_spec,
        )
    known = ", ".join(entry.name for entry in catalog.extensions) or "(none)"
    raise ToolError(
        "NOT_FOUND",
        f"no extension named {name_or_spec!r} in the catalog",
        f"Known: {known}. A git URL or pip requirement installs directly.",
    )


def _run_uv_tool(args: list[str]) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            ["uv", "tool", *args],
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    except FileNotFoundError:
        raise ToolError(
            "EXTRA_NOT_INSTALLED",
            "installing extensions needs the uv tool on PATH.",
            "Install uv (https://docs.astral.sh/uv/), or install the package "
            "yourself with pipx or pip and run it directly.",
        ) from None
    except subprocess.TimeoutExpired:
        return False, "uv tool timed out after 600 seconds"
    output = (completed.stdout or "") + (completed.stderr or "")
    return completed.returncode == 0, output.strip()


def core_version_note(entry: ExtensionEntry) -> str | None:
    """A warning when the extension pins a core range this console is outside.

    Informational only: agent extensions run in their own environment with
    their own core, so a mismatch never blocks the install.
    """
    if not entry.requires_core:
        return None
    import ifc_console

    try:
        from packaging.specifiers import SpecifierSet
        from packaging.version import Version

        if Version(ifc_console.__version__) not in SpecifierSet(entry.requires_core):
            return (
                f"{entry.name} declares ifc-console{entry.requires_core}; this "
                f"console is {ifc_console.__version__}. Standalone runs are "
                "unaffected; attach mode depends on the envelope contract."
            )
    except Exception:
        return None
    return None


def install(
    home: Path, name_or_spec: str, *, catalog_url: str | None = None
) -> dict[str, Any]:
    """Install one extension into its own environment and record it."""
    catalog, source = fetch_catalog(catalog_url)
    entry = resolve_entry(catalog, name_or_spec)
    if entry.kind == "plugin":
        raise ToolError(
            "INVALID_INPUT",
            f"{entry.name} is an operation plugin, not a standalone agent.",
            "Install it into the console environment with pip and allow it via "
            "plugins.allow; see the plugins documentation.",
        )
    ok, output = _run_uv_tool(["install", entry.package])
    if not ok:
        raise ToolError(
            "EXEC_ERROR",
            f"uv tool install {entry.package} failed",
            output.splitlines()[-1] if output else "Run the command manually to see why.",
        )
    record = {
        "package": entry.package,
        "kind": entry.kind,
        "command": entry.command,
        "description": entry.description,
        "source": source,
        "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    InstallRecord(home).add(entry.name, record)
    note = core_version_note(entry)
    return {"name": entry.name, **record, **({"note": note} if note else {})}


def uninstall(home: Path, name: str) -> dict[str, Any]:
    record_store = InstallRecord(home)
    installed = record_store.load()
    entry = installed.get(name)
    if entry is None:
        known = ", ".join(sorted(installed)) or "(none)"
        raise ToolError(
            "NOT_FOUND",
            f"{name!r} is not in the install record",
            f"Installed: {known}. `uv tool list` shows tools installed outside it.",
        )
    ok, output = _run_uv_tool(["uninstall", str(entry.get("package"))])
    if not ok:
        raise ToolError(
            "EXEC_ERROR",
            f"uv tool uninstall {entry.get('package')} failed",
            output.splitlines()[-1] if output else "Run the command manually to see why.",
        )
    record_store.remove(name)
    return {"name": name, **entry}


def search(query: str, *, catalog_url: str | None = None) -> tuple[list[ExtensionEntry], str]:
    catalog, source = fetch_catalog(catalog_url)
    needle = query.casefold()
    hits = [
        entry
        for entry in catalog.extensions
        if needle in entry.name.casefold() or needle in entry.description.casefold()
    ]
    return hits, source


__all__ = [
    "CATALOG_URL",
    "Catalog",
    "ExtensionEntry",
    "InstallRecord",
    "SEED_CATALOG",
    "core_version_note",
    "fetch_catalog",
    "install",
    "resolve_entry",
    "search",
    "uninstall",
]
