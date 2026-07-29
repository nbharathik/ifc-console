"""Brand assets for ifc-console: the terminal banner and the SVG kit.

One home for the identity. The console and CLI import the terminal pieces
(palette, the uppercase wordmark, the block banner); running
``python -m ifc_console.branding`` writes the SVG kit and text banner into
``docs/assets/brand`` for the README and the docs site.

The logo is the uppercase wordmark **IFC CONSOLE** drawn as custom monoline
letterforms: squared geometric capitals with a uniform stroke, square
terminals, and a tight corner radius, hand-built as SVG paths so no font is
needed and it renders identically everywhere. The two words are split by a
wide letterspace rather than a hyphen, and by tone: **IFC** muted, **CONSOLE**
strong. The colors are quiet neutrals in the opencode style, two tones of warm
gray, no accent color. That keeps the logo clear of the app-wide mode colors
(green ask, red edit).

Square icons come in two forms. The **IC** monogram, built from the same
letterforms, is the app icon and the docs header. The **>_** prompt mark, built
from the same stroke system, is the favicon: two shapes stay legible at 16px
where two letters do not, and it reads as "terminal" at a glance.

The terminal keeps its own medium: ``logo.txt`` renders the wordmark in a
5-row block font, which is the right look for a README or a release note. The
console itself prints no banner at all: the log belongs to the session, so the
name lives in the top-right brand row and nowhere else.
"""

from __future__ import annotations

from pathlib import Path

__all__ = [
    "CATEGORICAL",
    "TAGLINE",
    "WORDMARK",
    "PALETTE",
    "categorical_color",
    "render_block",
    "block_lines",
    "top_right_markup",
    "write_assets",
]

# --------------------------------------------------------------------- palette
# Quiet warm neutrals, opencode style: a light gray, a mid gray, and a near
# black. Two tones per theme, no accent color. That is the whole system.
GRAY_LIGHT = "#CFCECD"  # strong tone on dark backgrounds
GRAY_MID = "#656363"    # muted tone on either theme
GRAY_DARK = "#211E1E"   # strong tone on light backgrounds; dark tiles
BG_DARK = "#161514"     # warm near-black surface (social card, previews)
PAPER = "#F5F8FB"       # near-white

PALETTE = {
    "gray_light": GRAY_LIGHT,
    "gray_mid": GRAY_MID,
    "gray_dark": GRAY_DARK,
    "bg_dark": BG_DARK,
    "paper": PAPER,
}

# ------------------------------------------------- data / review color system
# Okabe-Ito categorical palette: distinguishable under the common color-vision
# deficiencies. Used wherever elements are grouped by value (viewer color
# themes and their legends); meaning never rides on color alone, every legend
# entry carries a label and count.
CATEGORICAL = (
    "#0072B2",  # blue
    "#E69F00",  # orange
    "#009E73",  # bluish green
    "#CC79A7",  # reddish purple
    "#56B4E9",  # sky blue
    "#D55E00",  # vermillion
    "#F0E442",  # yellow
    "#999999",  # neutral gray
)


def categorical_color(index: int) -> str:
    """A stable palette color for group ``index`` (wraps past the end)."""
    return CATEGORICAL[index % len(CATEGORICAL)]

WORDMARK = "IFC CONSOLE"  # the logo, always upper case, split by a space
TAGLINE = "a terminal interface to connect IFC files to LLMs"

# ------------------------------------------------------- terminal block font
# A 5-row uppercase block font, only the glyphs the brand needs. Rows are
# padded to a fixed width per glyph and joined with a one-column gap, so the
# banner always lines up in any monospace terminal.
_BLOCK_HEIGHT = 5
_GLYPHS: dict[str, list[str]] = {
    "I": ["###", " # ", " # ", " # ", "###"],
    "F": ["####", "#   ", "### ", "#   ", "#   "],
    "C": ["####", "#   ", "#   ", "#   ", "####"],
    "O": ["####", "#  #", "#  #", "#  #", "####"],
    "N": ["#  #", "## #", "# ##", "#  #", "#  #"],
    "S": ["####", "#   ", "####", "   #", "####"],
    "L": ["#   ", "#   ", "#   ", "#   ", "####"],
    "E": ["####", "#   ", "### ", "#   ", "####"],
    "-": ["   ", "   ", "###", "   ", "   "],
    " ": ["   ", "   ", "   ", "   ", "   "],
}
_FILL = "█"  # the "#" cells become solid blocks in the terminal banner


def block_lines(text: str = WORDMARK) -> list[str]:
    """The block wordmark for ``text`` as a list of equal-height rows.

    The block font is uppercase, so ``ifc-console`` renders as IFC-CONSOLE.
    """
    glyphs = [_GLYPHS.get(ch, _GLYPHS[" "]) for ch in text.upper()]
    return [" ".join(g[r].replace("#", _FILL) for g in glyphs) for r in range(_BLOCK_HEIGHT)]


def render_block(text: str = WORDMARK) -> str:
    """The block wordmark for ``text`` as one multi-line string (no color)."""
    return "\n".join(block_lines(text))


# ------------------------------------------------------------- console markup
# Rich markup strings for the Textual console. Static text only, so they carry
# no user or model controlled values and need no escaping. The terminal brand
# is neutral (bold + dim), matching the grayscale logo.
def top_right_markup() -> str:
    """The wordmark for the console's top-right brand row."""
    return f"[b]{WORDMARK}[/b]"


# --------------------------------------------------- vector letterforms (svg)
# The wordmark drawn as squared monoline capitals: cap height 40 units, stroke
# 7, square terminals, miter joins, corner radius 6. Each glyph is a set of
# centerline paths plus its ink width; glyphs are placed left to right with a
# fixed gap. Hand-built paths mean the logo needs no font anywhere. The space
# is a glyph too: its width plus the gap on either side is the word split, so
# the lockup needs no hyphen.
_CAP = 40.0
_STROKE = 7.0
_LETTER_GAP = 9.0
# Prefix on a path that must not take the group's square terminals: a square
# cap on a diagonal cuts at an angle and leaves a nub outside the stem.
_BUTT = "butt:"
_GLYPH_PATHS: dict[str, tuple[float, tuple[str, ...]]] = {
    "I": (7.0, ("M3.5 3.5V36.5",)),
    "F": (27.5, ("M3.5 36.5V3.5H24", "M3.5 19.5H19")),
    "C": (27.5, ("M24 3.5H9.5A6 6 0 0 0 3.5 9.5V30.5A6 6 0 0 0 9.5 36.5H24",)),
    "O": (
        28.0,
        (
            "M9.5 3.5A6 6 0 0 0 3.5 9.5V30.5A6 6 0 0 0 9.5 36.5H18.5"
            "A6 6 0 0 0 24.5 30.5V9.5A6 6 0 0 0 18.5 3.5Z",
        ),
    ),
    # N is three subpaths, not one polyline: joining a stem to the diagonal
    # makes an acute miter that spikes far past the cap line. The diagonal's
    # butt ends sit inside the stems, so the corners stay square.
    "N": (28.0, ("M3.5 36.5V3.5", "M24.5 36.5V3.5", _BUTT + "M3.5 3.5L24.5 36.5")),
    "S": (
        27.5,
        (
            "M24 9.5A6 6 0 0 0 18 3.5H9.5A6 6 0 0 0 3.5 9.5V14A6 6 0 0 0 9.5 20"
            "H18A6 6 0 0 1 24 26V30.5A6 6 0 0 1 18 36.5H9.5A6 6 0 0 1 3.5 30.5",
        ),
    ),
    "L": (27.5, ("M3.5 3.5V36.5H24",)),
    "E": (27.5, ("M24 3.5H3.5V36.5H24", "M3.5 19.5H19")),
    "-": (14.0, ("M0 20H14",)),
    " ": (13.0, ()),
    # the prompt mark, same stroke system: a chevron and a rule on the baseline
    ">": (25.0, ("M4 4L19.5 20L4 36",)),
    "_": (18.0, ("M3.5 36.5H14.5",)),
}


def _ink_width(text: str) -> float:
    """Total ink width of ``text`` in glyph units, gaps included."""
    widths = [_GLYPH_PATHS[ch][0] for ch in text]
    return sum(widths) + _LETTER_GAP * (len(widths) - 1)


def _path(d: str) -> str:
    """One centerline path, honouring the ``_BUTT`` terminal override."""
    if d.startswith(_BUTT):
        return f'<path d="{d[len(_BUTT):]}" stroke-linecap="butt"/>'
    return f'<path d="{d}"/>'


def _letters(text: str, colors: list[str]) -> str:
    """The glyphs of ``text`` as stroke paths, one color entry per glyph."""
    parts = []
    gx = 0.0
    for ch, color in zip(text, colors, strict=True):
        width, paths = _GLYPH_PATHS[ch]
        body = "".join(_path(d) for d in paths)
        if body:  # the space carries no paths
            parts.append(f'<g transform="translate({gx:.1f} 0)" stroke="{color}">{body}</g>')
        gx += width + _LETTER_GAP
    return "".join(parts)


def _stroke_group(x: float, y: float, scale: float, inner: str) -> str:
    return (
        f'<g transform="translate({x:.1f} {y:.1f}) scale({scale:.3f})" fill="none" '
        f'stroke-width="{_STROKE}" stroke-linecap="square" stroke-linejoin="miter">'
        f"{inner}</g>"
    )


def _vector_wordmark(theme: str, x: float, y: float, scale: float) -> str:
    """The two-tone wordmark (IFC muted, CONSOLE strong) at (x, y)."""
    muted, strong = _theme(theme)
    split = WORDMARK.index(" ")
    colors = [muted if i < split else strong for i in range(len(WORDMARK))]
    return _stroke_group(x, y, scale, _letters(WORDMARK, colors))


def _vector_ic(x: float, y: float, scale: float, color: str) -> str:
    """The IC monogram letters at (x, y), one color."""
    return _stroke_group(x, y, scale, _letters("IC", [color, color]))


def _vector_prompt(x: float, y: float, scale: float, color: str) -> str:
    """The >_ prompt mark at (x, y), one color."""
    return _stroke_group(x, y, scale, _letters(">_", [color, color]))


def _theme(theme: str) -> tuple[str, str]:
    """(muted, strong) tone pair for a theme."""
    if theme == "light":
        return GRAY_MID, GRAY_DARK
    return GRAY_MID, GRAY_LIGHT


def _svg(
    width: float,
    height: float,
    body: str,
    *,
    title: str = "ifc-console",
    bg: str | None = None,
) -> str:
    rect = f'<rect width="{width:.0f}" height="{height:.0f}" fill="{bg}"/>' if bg else ""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" height="{height:.0f}" '
        f'viewBox="0 0 {width:.0f} {height:.0f}" role="img" aria-label="{title}">'
        f"<title>{title}</title>{rect}{body}</svg>\n"
    )


_TAG_FONT = (
    "'DejaVu Sans Mono','JetBrains Mono','SFMono-Regular',ui-monospace,"
    "Menlo,Consolas,monospace"
)


# --------------------------------------------------------------- svg builders
def svg_wordmark(theme: str = "dark") -> str:
    """The wordmark alone."""
    s, pad = 1.2, 12.0
    w = _ink_width(WORDMARK) * s + pad * 2
    h = _CAP * s + pad * 2
    return _svg(w, h, _vector_wordmark(theme, pad, pad, s))


def svg_horizontal(theme: str = "dark") -> str:
    """The wordmark at README-banner size, the primary logo for the README.

    Intrinsically wide (like opencode's README logo) so GitHub renders it
    large without a width attribute; being SVG it stays crisp at any size.
    """
    s, padx, pady = 2.6, 28.0, 30.0
    w = _ink_width(WORDMARK) * s + padx * 2
    h = _CAP * s + pady * 2
    return _svg(w, h, _vector_wordmark(theme, padx, pady, s))


def svg_stacked(theme: str = "dark") -> str:
    """The IC badge above the wordmark, for narrow columns and avatars."""
    w, h = 360.0, 200.0
    badge = (
        f'<rect x="{(w - 88) / 2:.0f}" y="18" width="88" height="88" rx="14" fill="{GRAY_LIGHT}"/>'
    )
    ic_s = 1.15
    ic = _vector_ic((w - _ink_width("IC") * ic_s) / 2, 18 + (88 - _CAP * ic_s) / 2, ic_s, GRAY_DARK)
    wm_s = 0.83
    wm = _vector_wordmark(theme, (w - _ink_width(WORDMARK) * wm_s) / 2, 148, wm_s)
    return _svg(w, h, badge + ic + wm)


def svg_mark(color: str = GRAY_MID) -> str:
    """The IC monogram alone, transparent, in a gray legible on either theme."""
    s = 1.7
    x = (120 - _ink_width("IC") * s) / 2
    y = (120 - _CAP * s) / 2
    return _svg(120, 120, _vector_ic(x, y, s, color), title="ifc-console mark")


def svg_mark_white() -> str:
    """A light-gray IC monogram for colored surfaces (the docs header)."""
    s = 1.7
    x = (120 - _ink_width("IC") * s) / 2
    y = (120 - _CAP * s) / 2
    return _svg(120, 120, _vector_ic(x, y, s, GRAY_LIGHT), title="ifc-console")


def svg_prompt() -> str:
    """The >_ prompt mark alone, transparent, in a gray legible on either theme."""
    s = 1.7
    x = (120 - _ink_width(">_") * s) / 2
    y = (120 - _CAP * s) / 2
    return _svg(120, 120, _vector_prompt(x, y, s, GRAY_MID), title="ifc-console prompt mark")


def svg_favicon() -> str:
    """The >_ prompt mark on a dark tile: two shapes, still legible at 16px."""
    tile = f'<rect width="64" height="64" rx="10" fill="{GRAY_DARK}"/>'
    s = 0.85
    x = (64 - _ink_width(">_") * s) / 2
    y = (64 - _CAP * s) / 2
    return _svg(64, 64, tile + _vector_prompt(x, y, s, GRAY_LIGHT), title="ifc-console")


def svg_monogram() -> str:
    """The IC badge: a light tile with dark letters, an app icon."""
    badge = f'<rect width="64" height="64" rx="10" fill="{GRAY_LIGHT}"/>'
    s = 0.85
    x = (64 - _ink_width("IC") * s) / 2
    y = (64 - _CAP * s) / 2
    return _svg(64, 64, badge + _vector_ic(x, y, s, GRAY_DARK), title="ifc-console")


def svg_social() -> str:
    """A 1280x640 preview for the GitHub social card and docs og:image."""
    w, h = 1280, 640
    grid = ['<g stroke="#2A2827" stroke-width="1" stroke-opacity="0.5">']
    grid += [f'<line x1="{gx}" y1="0" x2="{gx}" y2="{h}"/>' for gx in range(0, w + 1, 48)]
    grid += [f'<line x1="0" y1="{gy}" x2="{w}" y2="{gy}"/>' for gy in range(0, h + 1, 48)]
    grid.append("</g>")
    s = 2.8
    x0 = (w - _ink_width(WORDMARK) * s) / 2
    word = _vector_wordmark("dark", x0, 196, s)
    tag = (
        f'<text x="{x0:.0f}" y="400" font-family="{_TAG_FONT}" font-size="32" '
        f'fill="{GRAY_LIGHT}" fill-opacity="0.9">A terminal interface to connect '
        f"IFC files to LLMs.</text>"
    )
    prompt = (
        f'<text x="{x0:.0f}" y="452" font-family="{_TAG_FONT}" font-size="25" '
        f'fill="{GRAY_LIGHT}">&gt;<tspan fill="#7C7A78"> chat with your model from '
        f"any MCP client</tspan></text>"
    )
    body = "".join(grid) + word + tag + prompt
    return _svg(w, h, body, title="ifc-console", bg=BG_DARK)


# --------------------------------------------------------------------- writer
def _assets() -> dict[str, str]:
    return {
        "mark.svg": svg_mark(),
        "mark-white.svg": svg_mark_white(),
        "prompt.svg": svg_prompt(),
        "favicon.svg": svg_favicon(),
        "monogram.svg": svg_monogram(),
        "wordmark-dark.svg": svg_wordmark("dark"),
        "wordmark-light.svg": svg_wordmark("light"),
        "horizontal-dark.svg": svg_horizontal("dark"),
        "horizontal-light.svg": svg_horizontal("light"),
        "stacked-dark.svg": svg_stacked("dark"),
        "stacked-light.svg": svg_stacked("light"),
        "social-preview.svg": svg_social(),
        "logo.txt": render_block(WORDMARK) + "\n\n" + TAGLINE + "\n",
    }


def write_assets(out_dir: str | Path) -> list[Path]:
    """Write the full logo kit into ``out_dir`` and return the paths written."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for name, content in _assets().items():
        path = out / name
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m ifc_console.branding",
        description="Generate the ifc-console logo kit (SVG marks and a text banner).",
    )
    parser.add_argument(
        "--out", default="docs/assets/brand", help="Output directory (default: docs/assets/brand)."
    )
    args = parser.parse_args(argv)
    written = write_assets(args.out)
    print(f"wrote {len(written)} files to {args.out}:")
    for path in written:
        print(f"  {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
