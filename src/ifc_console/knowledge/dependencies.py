"""Install guidance for optional project-document capabilities."""

from __future__ import annotations

import sys


def missing_document_dependency(distribution: str) -> str:
    """Return a repair command for a missing document parser or renderer."""
    return (
        f"{distribution} is part of optional document support. Install it for this "
        f'interpreter with: "{sys.executable}" -m pip install "ifc-console[documents]".'
    )


__all__ = ["missing_document_dependency"]
