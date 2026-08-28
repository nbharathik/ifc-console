"""Agent capabilities and precise repair hints for the active interpreter.

The credential store is part of the base agents distribution. PDF extraction
and page rendering are optional document capabilities so applications that do
not ingest manuals keep a smaller environment.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

# (id, label, import name, distribution, required, consequence)
_CAPABILITIES: tuple[tuple[str, str, str, str, bool, str], ...] = (
    (
        "pdf_text",
        "PDF text",
        "pypdf",
        "pypdf",
        False,
        "PDF uploads cannot be indexed or searched",
    ),
    (
        "pdf_pages",
        "PDF page images",
        "pymupdf",
        "PyMuPDF",
        False,
        "drawings and scanned pages cannot be shown to a vision model",
    ),
    (
        "credential_store",
        "Credential store",
        "keyring",
        "keyring",
        True,
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
    required: bool
    consequence: str

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "distribution": self.distribution,
            "present": self.present,
            "version": self.version,
            "required": self.required,
            "consequence": self.consequence,
            "install": None if self.required else "ifc-console-agents[documents]",
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
    """Repair a stale base agents installation in this interpreter."""
    kind = install_kind()
    if kind == "uv-tool":
        return "uv tool upgrade ifc-console --reinstall"
    if kind == "venv":
        return f'"{sys.executable}" -m pip install --upgrade ifc-console-agents'
    return f'"{sys.executable}" -m pip install --user --upgrade ifc-console-agents'


def documents_install_command() -> str:
    """Install optional agent document support in this interpreter."""
    if install_kind() == "uv-tool":
        return 'uv tool install ifc-console --with "ifc-console-agents[documents]" --force'
    user = " --user" if install_kind() == "system" else ""
    return (
        f'"{sys.executable}" -m pip install{user} --upgrade '
        '"ifc-console-agents[documents]"'
    )


def capabilities() -> list[Capability]:
    """Report base requirements and optional agent document capabilities."""
    found: list[Capability] = []
    for identifier, label, module, distribution, required, consequence in _CAPABILITIES:
        present = _probe(module)
        found.append(
            Capability(
                id=identifier,
                label=label,
                module=module,
                distribution=distribution,
                present=present,
                version=_version(distribution) if present else "",
                required=required,
                consequence=consequence,
            )
        )
    return found


def report() -> dict[str, object]:
    """A JSON-safe capability report for the panel and the CLI."""
    found = capabilities()
    missing_required = [item.label for item in found if item.required and not item.present]
    missing_optional = [item.label for item in found if not item.required and not item.present]
    missing = [*missing_required, *missing_optional]
    if missing_required:
        hint = (
            f"{', '.join(missing_required)} missing from the base ifc-console-agents "
            f"installation. Repair it with: {repair_command()}"
        )
    elif missing_optional:
        hint = (
            f"Optional document support is unavailable ({', '.join(missing_optional)}). "
            "Install ifc-console-agents[documents] to enable PDF ingestion and page images."
        )
    else:
        hint = "All agent capabilities are installed."
    return {
        "capabilities": [item.as_dict() for item in found],
        "missing": missing,
        "missing_required": missing_required,
        "missing_optional": missing_optional,
        "ok": not missing_required,
        "python": sys.executable,
        "install_kind": install_kind(),
        "repair": repair_command(),
        "documents_install": documents_install_command(),
        "hint": hint,
    }


def missing_dependency_hint(distribution: str) -> str:
    """The hint attached to a runtime error naming one missing distribution."""
    if distribution.casefold() in {"fitz", "pymupdf", "pypdf"}:
        return (
            f"Install optional PDF support with ifc-console-agents[documents]: "
            f"{documents_install_command()}."
        )
    return (
        f"{distribution} ships inside ifc-console-agents, so this console is "
        f"running from a stale environment ({sys.executable}). Reinstall or upgrade "
        f"ifc-console-agents there: {repair_command()}."
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
