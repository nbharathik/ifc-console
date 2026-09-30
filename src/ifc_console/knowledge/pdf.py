"""PDF text and page rendering through pypdfium2, the PDF backend of ``[agents]``."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path
from typing import Any

from ifc_console.core.results import ToolError


def _pdfium() -> Any:
    try:
        import pypdfium2
    except ImportError:
        from ifc_console.knowledge.dependencies import missing_document_dependency

        raise ToolError(
            "EXTRA_NOT_INSTALLED",
            "PDF support needs the pypdfium2 package.",
            missing_document_dependency("pypdfium2"),
        ) from None
    return pypdfium2


def open_pdf(path: Path) -> Any:
    """Open a PDF, turning unreadable or encrypted files into a clear error."""
    pdfium = _pdfium()
    try:
        return pdfium.PdfDocument(str(path))
    except Exception as exc:
        raise ToolError(
            "INVALID_INPUT",
            f"{path.name} could not be read as a PDF: {exc}",
            "Check the file; encrypted PDFs must be decrypted first.",
        ) from exc


def page_count(path: Path) -> int:
    document = open_pdf(path)
    try:
        return len(document)
    finally:
        document.close()


def page_texts(path: Path) -> list[tuple[int, str]]:
    """(page number, page text) for every page that carries text."""
    document = open_pdf(path)
    pages: list[tuple[int, str]] = []
    try:
        for number in range(len(document)):
            page = document[number]
            try:
                textpage = page.get_textpage()
                try:
                    text = textpage.get_text_bounded().replace("\r\n", "\n").strip()
                finally:
                    textpage.close()
            except Exception:
                text = ""
            finally:
                page.close()
            if text:
                pages.append((number + 1, text))
    finally:
        document.close()
    return pages


def png_bytes(rgb: bytes, width: int, height: int) -> bytes:
    """Encode 8-bit RGB pixels as a PNG without an imaging library."""
    stride = width * 3
    raw = b"".join(b"\x00" + rgb[row * stride : (row + 1) * stride] for row in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")


def render_page(
    path: Path, page: int, *, max_size: int, format: str, quality: int
) -> tuple[bytes, str, int, int]:
    """One page as image bytes: (data, format, width, height).

    JPEG needs Pillow; without it the page comes back as PNG and the returned
    format says so.
    """
    document = open_pdf(path)
    try:
        if page > len(document):
            raise ToolError(
                "INVALID_INPUT",
                f"page {page} is outside the 1-{len(document)} page range.",
                "Use the page count returned by list_project_documents.",
            )
        pdf_page = document[page - 1]
        try:
            width_pt, height_pt = pdf_page.get_size()
            scale = min(max_size / max(width_pt, height_pt), 4.0)
            bitmap = pdf_page.render(scale=scale, rev_byteorder=True)
            pixels = bitmap.to_numpy()[:, :, :3]
        finally:
            pdf_page.close()
    finally:
        document.close()
    height, width = int(pixels.shape[0]), int(pixels.shape[1])
    if format == "jpeg":
        try:
            import io

            from PIL import Image

            buffer = io.BytesIO()
            Image.fromarray(pixels).save(buffer, "JPEG", quality=quality)
            return buffer.getvalue(), "jpeg", width, height
        except ImportError:
            pass
    return png_bytes(pixels.tobytes(), width, height), "png", width, height


__all__ = ["open_pdf", "page_count", "page_texts", "png_bytes", "render_page"]
