"""A tiny PDF writer for the demo project and tests: pages of text and boxes.

Only what those need is here: the base-14 Helvetica font, positioned text lines,
and stroked rectangles. Coordinates are points from the top-left of the page.
"""

from __future__ import annotations

from dataclasses import dataclass, field


def _escape(text: str) -> str:
    return text.encode("cp1252", "replace").decode("cp1252").replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


@dataclass
class Page:
    width: float = 595.0
    height: float = 842.0
    _ops: list[str] = field(default_factory=list)

    def text(self, x: float, y: float, value: str, size: float = 11.0) -> Page:
        self._ops.append(
            f"BT /F1 {size:g} Tf {x:g} {self.height - y:g} Td ({_escape(value)}) Tj ET"
        )
        return self

    def rect(
        self, x0: float, y0: float, x1: float, y1: float, *, gray: float = 0.1, width: float = 1.0
    ) -> Page:
        self._ops.append(
            f"{gray:g} G {width:g} w {x0:g} {self.height - y1:g} {x1 - x0:g} {y1 - y0:g} re S"
        )
        return self

    def stream(self) -> bytes:
        return "\n".join(self._ops).encode("cp1252", "replace")


def build(pages: list[Page]) -> bytes:
    """Serialize pages into a PDF document."""
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog = add(b"")
    tree = add(b"")
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    kids: list[int] = []
    for page in pages:
        content = page.stream()
        stream = add(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
        kids.append(
            add(
                (
                    f"<< /Type /Page /Parent {tree} 0 R /MediaBox [0 0 {page.width:g} {page.height:g}] "
                    f"/Resources << /Font << /F1 {font} 0 R >> >> /Contents {stream} 0 R >>"
                ).encode("ascii")
            )
        )
    objects[catalog - 1] = f"<< /Type /Catalog /Pages {tree} 0 R >>".encode("ascii")
    refs = " ".join(f"{kid} 0 R" for kid in kids)
    objects[tree - 1] = f"<< /Type /Pages /Kids [{refs}] /Count {len(kids)} >>".encode("ascii")

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii")
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    ).encode("ascii")
    return bytes(out)


__all__ = ["Page", "build"]
