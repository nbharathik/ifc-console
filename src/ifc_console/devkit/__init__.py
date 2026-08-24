"""Developer harness: run and exercise the browser panel without an LLM key.

Nothing here is imported by the product paths. ``ifc-console dev`` builds a
throwaway demo project, turns on the rehearsal provider, and either boots one
browser tab or runs the headless feature checklist.
"""

from __future__ import annotations

__all__ = ["REHEARSAL_ID", "enable_rehearsal_provider", "rehearsal_enabled"]


def __getattr__(name: str):
    if name in __all__:
        from ifc_console.devkit import rehearsal

        return getattr(rehearsal, name)
    raise AttributeError(name)
