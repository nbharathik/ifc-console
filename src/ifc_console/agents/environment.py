"""What the bundled agents need from the environment, and how to repair it.

The agents ship with the package, so a missing PDF or keyring dependency is a
broken install, not an optional extra the user forgot. This module reports the
state and produces the exact repair command for the interpreter that is
actually running, because the usual failure is a console launched from a stale
environment rather than a wrong pyproject.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

# (id, label, import name, distribution, what stops working without it)
_REQUIRED: tuple[tuple[str, str, str, str, str], ...] = (
    ("pdf_text", "PDF text", "pypdf", "pypdf", "PDF uploads cannot be indexed or searched"),
    (
        "pdf_pages",
        "PDF page images",
        "pymupdf",
        "PyMuPDF",
        "drawings and scanned pages cannot be shown to a vision model",
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

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "distribution": self.distribution,
            "present": self.present,
            "version": self.version,
            "consequence": self.consequence,
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
    """The one command that fixes a stale environment, for this interpreter."""
    kind = install_kind()
    if kind == "uv-tool":
        return "uv tool upgrade ifc-console --reinstall"
    if kind == "venv":
        return f'"{sys.executable}" -m pip install --upgrade ifc-console'
    return f'"{sys.executable}" -m pip install --user --upgrade ifc-console'


def capabilities() -> list[Capability]:
    """Report every agent dependency that ships in the base package."""
    found: list[Capability] = []
    for identifier, label, module, distribution, consequence in _REQUIRED:
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
    return {
        "capabilities": [item.as_dict() for item in found],
        "missing": missing,
        "ok": not missing,
        "python": sys.executable,
        "install_kind": install_kind(),
        "repair": repair_command(),
        "hint": (
            "Everything the bundled agents need is installed."
            if not missing
            else (
                f"{', '.join(missing)} missing from this environment. These ship with "
                f"ifc-console, so the console is running from a stale install. Fix it with: "
                f"{repair_command()}"
            )
        ),
    }


def missing_dependency_hint(distribution: str) -> str:
    """The hint attached to a runtime error naming one missing distribution."""
    return (
        f"{distribution} ships inside ifc-console's base package, so this console is "
        f"running from a stale environment ({sys.executable}). Reinstall or upgrade "
        f"ifc-console there: {repair_command()}."
    )


__all__ = [
    "Capability",
    "capabilities",
    "install_kind",
    "missing_dependency_hint",
    "repair_command",
    "report",
]
