"""The first-party agent packs that ship inside IFC Console.

They appear in ``/agent`` and the browser panel without installation,
discovery, or an allow-list step. External agent extensions are intentionally
outside the current product surface.
"""

from __future__ import annotations


def builtin_packs() -> tuple:
    """The bundled packs, imported lazily so startup stays light."""
    from ifc_console.agents.builtin.docs import PACK as docs_pack
    from ifc_console.agents.builtin.measure import PACK as measure_pack

    return (measure_pack, docs_pack)


__all__ = ["builtin_packs"]
