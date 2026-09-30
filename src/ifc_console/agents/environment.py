"""What the optional ``[agents]`` extra adds, and how to add it to this interpreter.

The Agent workspace, providers, workflows, and skills run on the base install.
The extra supplies the pieces that talk to the outside: agent engines over ACP,
PDF text and page rendering, and the operating-system credential store.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_EXTRA = '"ifc-console[agents]"'

# (id, label, import name, distribution, consequence)
_CAPABILITIES: tuple[tuple[str, str, str, str, str], ...] = (
    (
        "engines",
        "Agent engines",
        "acp",
        "agent-client-protocol",
        "external agent engines cannot be started",
    ),
    (
        "pdf",
        "PDF text and pages",
        "pypdfium2",
        "pypdfium2",
        "PDF uploads cannot be indexed and drawings cannot be shown to a vision model",
    ),
    (
        "credential_store",
        "Credential store",
        "keyring",
        "keyring",
        "API keys cannot be saved in the operating-system store",
    ),
)


@dataclass(frozen=True)
class Capability:
    id: str
    label: str
    module: str
    distribution: str
    present: bool
    version: str
    consequence: str
    required: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "distribution": self.distribution,
            "present": self.present,
            "version": self.version,
            "required": self.required,
            "consequence": self.consequence,
            "install": None,
        }


def _version(distribution: str) -> str:
    try:
        from importlib.metadata import version

        return version(distribution)
    except Exception:
        return ""


def _probe(module: str) -> bool:
    from importlib.util import find_spec

    try:
        return find_spec(module) is not None
    except Exception:
        return False


def install_kind() -> Literal["uv-tool", "venv", "system"]:
    """How this interpreter was installed, since the repair differs."""
    prefix = Path(sys.prefix).resolve()
    if any(part.lower() == "tools" for part in prefix.parts) and "uv" in {
        part.lower() for part in prefix.parts
    }:
        return "uv-tool"
    if sys.prefix != sys.base_prefix or os.environ.get("VIRTUAL_ENV"):
        return "venv"
    return "system"


def repair_command() -> str:
    """Add the agents extra to this interpreter."""
    kind = install_kind()
    if kind == "uv-tool":
        return f"uv tool install {_EXTRA} --force"
    if kind == "venv":
        if not _probe("pip"):
            return f'uv pip install --python "{sys.executable}" --upgrade {_EXTRA}'
        return f'"{sys.executable}" -m pip install --upgrade {_EXTRA}'
    return f'"{sys.executable}" -m pip install --user --upgrade {_EXTRA}'


def documents_install_command() -> str:
    """The command the panel shows to add PDF support."""
    return repair_command()


def capabilities() -> list[Capability]:
    """Report what the agents extra provides in this interpreter."""
    found: list[Capability] = []
    for identifier, label, module, distribution, consequence in _CAPABILITIES:
        present = _probe(module)
        found.append(
            Capability(
                id=identifier,
                label=label,
                module=module,
                distribution=distribution,
                present=present,
                version=_version(distribution) if present else "",
                consequence=consequence,
            )
        )
    return found


def report() -> dict[str, object]:
    """A JSON-safe capability report for the panel and the CLI."""
    found = capabilities()
    missing = [item.label for item in found if not item.present]
    if missing:
        hint = f"Optional agent capabilities are missing ({', '.join(missing)}). Add them with: {repair_command()}"
    else:
        hint = "All agent capabilities are installed."
    return {
        "capabilities": [item.as_dict() for item in found],
        "missing": missing,
        "missing_required": [],
        "missing_optional": missing,
        "ok": True,
        "python": sys.executable,
        "install_kind": install_kind(),
        "repair": repair_command(),
        "documents_install": documents_install_command(),
        "hint": hint,
    }


def missing_dependency_hint(distribution: str) -> str:
    """The hint attached to a runtime error naming one missing distribution."""
    return (
        f"{distribution} is part of the optional agents extra. "
        f"Add it to this interpreter ({sys.executable}) with: {repair_command()}"
    )


__all__ = [
    "Capability",
    "capabilities",
    "documents_install_command",
    "install_kind",
    "missing_dependency_hint",
    "repair_command",
    "report",
]
