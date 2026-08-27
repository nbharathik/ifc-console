"""Named UI themes shared by the console, viewer, and chat surfaces."""

from __future__ import annotations

THEME_LABELS = {
    "light": "Light",
    "dark": "Dark",
    "modern": "Modern Dark",
    "blue": "Default Blue",
}

THEME_IDS = tuple(THEME_LABELS)
RESOLVED_THEME_IDS = THEME_IDS
# ``auto`` remains readable for settings written by older releases, but it is
# no longer presented as a fifth theme. It resolves to the product default.
THEME_PATTERN = "^(light|dark|modern|blue|auto)$"


def resolve_theme(name: str) -> str:
    """Resolve a stored choice to the palette every rendered surface uses."""
    return name if name in RESOLVED_THEME_IDS else "blue"


def theme_label(name: str) -> str:
    """Return the human-facing name while remaining robust to old settings."""
    return THEME_LABELS.get(name, THEME_LABELS["blue"])
