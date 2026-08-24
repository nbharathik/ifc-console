"""The first-party agents that ship inside IFC Console.

They appear in ``/agent`` and the browser panel without installation,
discovery, or an allow-list step. Every one of them is a preset: a role prompt
plus a set of capability blocks, built by the same code path as a user's own
agent. External agent extensions are intentionally outside the current product
surface.
"""

from __future__ import annotations


def builtin_packs() -> tuple:
    """The bundled packs, imported lazily so startup stays light."""
    from ifc_console.agents.presets import preset_packs

    return preset_packs()


__all__ = ["builtin_packs"]
