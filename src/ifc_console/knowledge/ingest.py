"""Turn project documents into knowledge records.

Markdown splits per heading section, plain text packs paragraphs, PDFs index
per page, and every PDF page remains available as renderable visual evidence.
Scanned pages carry no searchable text but can still be inspected by a vision
model. Images are registered so search can find and cite them, but their
pixels are not indexed. Document text is data, never instructions: chunks
that look like instructions are flagged at ingest time.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ifc_console.core.results import ToolError
from ifc_console.knowledge.records import Record

MARKDOWN_SUFFIXES = (".md", ".markdown")
TEXT_SUFFIXES = (".txt",)
PDF_SUFFIXES = (".pdf",)
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg")
SUPPORTED_SUFFIXES = MARKDOWN_SUFFIXES + TEXT_SUFFIXES + PDF_SUFFIXES + IMAGE_SUFFIXES

# One chunk should fit a retrieval result, not a whole manual.
_MAX_CHUNK = 4000
_SUMMARY_CHARS = 240

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


def _summary(text: str) -> str:
    flat = " ".join(text.split())
    return flat[:_SUMMARY_CHARS]


def _flags(text: str) -> dict[str, Any]:
    from ifc_console.policy.untrusted import scan

    excerpts = scan(text)
    return {"instruction_like": True} if excerpts else {}


def _split_big(section: str | None, text: str) -> list[tuple[str | None, str]]:
    if len(text) <= _MAX_CHUNK:
        return [(section, text)]
    parts: list[tuple[str | None, str]] = []
    paragraphs = re.split(r"\n\s*\n", text)
    bucket: list[str] = []
    size = 0
    for paragraph in paragraphs:
        if size + len(paragraph) > _MAX_CHUNK and bucket:
            parts.append((section, "\n\n".join(bucket)))
            bucket, size = [], 0
        bucket.append(paragraph)
        size += len(paragraph)
    if bucket:
        parts.append((section, "\n\n".join(bucket)))
    if len(parts) > 1:
        parts = [
            (f"{section or 'text'} ({i + 1})" if len(parts) > 1 else section, text)
            for i, (section, text) in enumerate(parts)
        ]
    return parts


def chunk_markdown(text: str) -> tuple[str | None, list[tuple[str | None, str]]]:
    """(document title, [(section, chunk text), ...]) split at headings."""
    title: str | None = None
    sections: list[tuple[str | None, list[str]]] = [(None, [])]
    for line in text.splitlines():
        match = _HEADING.match(line)
        if match:
            heading = match.group(2).strip()
            if title is None and len(match.group(1)) == 1:
                title = heading
            sections.append((heading, []))
        else:
            sections[-1][1].append(line)
    chunks: list[tuple[str | None, str]] = []
    for section, lines in sections:
        body = "\n".join(lines).strip()
        if not body and not section:
            continue
        text_block = body if body else (section or "")
        chunks.extend(_split_big(section, text_block))
    return title, chunks


def chunk_text(text: str) -> list[tuple[str | None, str]]:
    body = text.strip()
    if not body:
        return []
    return _split_big(None, body)


def chunk_pdf(path: Path) -> list[tuple[int, str]]:
    """(page number, page text) for every page that carries text."""
    try:
        from pypdf import PdfReader
    except ImportError:
        from ifc_console.agents.environment import missing_dependency_hint

        raise ToolError(
            "EXTRA_NOT_INSTALLED",
            "PDF ingestion needs the pypdf package.",
            missing_dependency_hint("pypdf"),
        ) from None
    try:
        reader = PdfReader(str(path))
    except Exception as exc:
        raise ToolError(
            "INVALID_INPUT",
            f"{path.name} could not be read as a PDF: {exc}",
            "Check the file; encrypted PDFs must be decrypted first.",
        ) from exc
    pages: list[tuple[int, str]] = []
    for number, page in enumerate(reader.pages, start=1):
        try:
            text = (page.extract_text() or "").strip()
        except Exception:
            text = ""
        if text:
            pages.append((number, text))
    return pages


def file_records(path: Path, *, base: Path) -> tuple[list[Record], dict[str, Any]]:
    """Records for one document, plus a per-file ingest report entry."""
    from ifc_console.automation.files import sha256_file

    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ToolError(
            "INVALID_INPUT",
            f"{path.name} is not an ingestable document",
            f"Supported: {', '.join(SUPPORTED_SUFFIXES)}",
        )
    try:
        rel = str(path.resolve().relative_to(base.resolve()))
    except ValueError:
        rel = str(path.resolve())
    rel = rel.replace("\\", "/")
    sha = sha256_file(path)
    stem = path.stem
    title: str | None = None
    entry: dict[str, Any] = {"path": rel, "sha256": sha}

    def record(index: str, section: str | None, body: str, page: int | None, media: str) -> Record:
        name = f"{title or stem}" + (f" - {section}" if section else "")
        meta: dict[str, Any] = {
            "path": rel,
            "media": media,
            "sha256": sha,
            "aliases": [stem, path.name],
            **_flags(body),
        }
        if page is not None:
            meta["page"] = page
        if section:
            meta["section"] = section
        return Record(
            kind="doc",
            key=f"doc:{rel}#{index}",
            name=name,
            summary=_summary(body) or f"{media} document",
            body=body,
            meta=meta,
        )

    records: list[Record] = []
    if suffix in IMAGE_SUFFIXES:
        entry["media"] = "image"
        records.append(
            Record(
                kind="doc",
                key=f"doc:{rel}#1",
                name=stem,
                summary="image document; referenced but not text-indexed",
                meta={"path": rel, "media": "image", "sha256": sha, "aliases": [path.name]},
            )
        )
    elif suffix in PDF_SUFFIXES:
        entry["media"] = "pdf"
        pages = chunk_pdf(path)
        try:
            from pypdf import PdfReader

            entry["pages"] = len(PdfReader(str(path)).pages)
        except Exception:
            # chunk_pdf already produced the precise invalid-PDF error. This
            # fallback only protects metadata collection from a second read.
            entry["pages"] = max((number for number, _ in pages), default=0)
        entry["text_pages"] = len(pages)
        if not pages:
            entry["no_text"] = True
        text_by_page = dict(pages)
        visual_only = 0
        for number in range(1, int(entry["pages"]) + 1):
            text = text_by_page.get(number)
            if text:
                records.append(record(f"p{number}", None, text, number, "pdf"))
                continue
            visual_only += 1
            records.append(
                record(
                    f"p{number}",
                    None,
                    f"Visual-only PDF page {number} in {path.name}; render this page to inspect it.",
                    number,
                    "pdf",
                )
            )
        entry["visual_only_pages"] = visual_only
    else:
        media = "markdown" if suffix in MARKDOWN_SUFFIXES else "text"
        entry["media"] = media
        text = path.read_text(encoding="utf-8", errors="replace")
        if media == "markdown":
            title, chunks = chunk_markdown(text)
        else:
            chunks = chunk_text(text)
        if not chunks:
            entry["no_text"] = True
        for i, (section, body) in enumerate(chunks, start=1):
            records.append(record(str(i), section, body, None, media))

    entry["records"] = len(records)
    flagged = sum(1 for r in records if r.meta.get("instruction_like"))
    if flagged:
        entry["instruction_like"] = flagged
    return records, entry


__all__ = ["SUPPORTED_SUFFIXES", "chunk_markdown", "chunk_pdf", "chunk_text", "file_records"]
