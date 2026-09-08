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
TABLE_SUFFIXES = (".jsonl", ".csv")
SUPPORTED_SUFFIXES = (
    MARKDOWN_SUFFIXES + TEXT_SUFFIXES + PDF_SUFFIXES + IMAGE_SUFFIXES + TABLE_SUFFIXES
)

# One chunk should fit a retrieval result, not a whole manual.
_MAX_CHUNK = 4000
_SUMMARY_CHARS = 240
# A table becomes one record per row; a catalogue is hundreds, not millions.
_MAX_ROWS = 20_000
_NAME_FIELDS = ("designation", "name", "title", "id", "grade", "product", "system", "section")

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


def _coerce(value: str) -> Any:
    text = value.strip()
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+\.\d+", text):
        return float(text)
    if text.lower() in {"true", "false"}:
        return text.lower() == "true"
    return text


def chunk_table(path: Path) -> list[dict[str, Any]]:
    """One dict per row of a .jsonl (an object per line) or .csv (header row) file."""
    import csv
    import json

    rows: list[dict[str, Any]] = []
    suffix = path.suffix.lower()
    with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        if suffix == ".csv":
            for row in csv.DictReader(handle):
                rows.append({str(k): _coerce(v) for k, v in row.items() if k is not None})
                if len(rows) >= _MAX_ROWS:
                    break
        else:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
                if len(rows) >= _MAX_ROWS:
                    break
    return rows


_NUMBERISH = re.compile(r"^[-+]?\d+(?:[.,]\d+)?%?$|^[-–—]$|^[xX]$")
_FOOTNOTE_MARK = re.compile(r"\d\)$")
_MAX_PDF_ROW_NAME = 6
_DIGEST_CHARS = 12_000


def pdf_table_rows(text: str) -> list[dict[str, Any]]:
    """Table-like lines of one PDF page: a short name followed by numbers.

    A printed table row is a name and at least three numeric cells; the
    non-numeric lines just above a block of rows are kept as the header the
    reader would use to name the columns. Values stay positional (v0, v1,
    ...) because the layout, not the text, carries the column names.
    """
    rows: list[dict[str, Any]] = []
    recent: list[str] = []
    header = ""
    in_block = False
    previous: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        tokens = line.split()
        tail = 0
        for token in reversed(tokens):
            if _NUMBERISH.match(token):
                tail += 1
            else:
                break
        name_tokens = tokens[: len(tokens) - tail]
        is_row = (
            tail >= 3
            and 0 < len(name_tokens) <= _MAX_PDF_ROW_NAME
            and any(re.search(r"[A-Za-z]", token) for token in name_tokens)
        )
        if not is_row:
            in_block = False
            if tail == 0:
                recent.append(line)
                recent = recent[-3:]
            continue
        if not in_block:
            header = " | ".join(recent)
            in_block = True
            previous = []
        # "GU 6N Per S" followed by "Per D": a name that starts with a later
        # word of the previous name continues it, so it inherits the head
        if previous and name_tokens[0] != previous[0] and name_tokens[0] in previous[1:]:
            name_tokens = previous[: previous.index(name_tokens[0])] + name_tokens
        previous = name_tokens
        name = _FOOTNOTE_MARK.sub("", " ".join(name_tokens)).strip()
        values = [_coerce(token.replace(",", ".")) for token in tokens[len(name_tokens) :]]
        values = [
            None if isinstance(v, str) and v in {"-", "–", "—", "x", "X"} else v for v in values
        ]
        row: dict[str, Any] = {"name": name, "values": values, "header": header}
        for index, value in enumerate(values):
            row[f"v{index}"] = value
        rows.append(row)
    return rows


def pdf_digest(pages: list[tuple[int, str]], rows_by_page: dict[int, list[dict[str, Any]]]) -> str:
    """One outline of a PDF: per page, its first heading-like line and its table rows."""
    lines = []
    for number, text in pages:
        title = next(
            (
                line.strip()
                for line in text.splitlines()
                if line.strip() and re.search(r"[A-Za-z]{3}", line) and len(line.strip()) <= 90
            ),
            "",
        )
        rows = rows_by_page.get(number) or []
        names = [row["name"] for row in rows[:6]]
        entry = f"p{number}: {title}"
        if rows:
            entry += f" | {len(rows)} table rows: " + ", ".join(names)
            if len(rows) > len(names):
                entry += ", ..."
        lines.append(entry)
    digest = "\n".join(lines)
    return digest[:_DIGEST_CHARS]


def _flatten(row: dict[str, Any], prefix: str = "") -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    for key, value in row.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            out.extend(_flatten(value, f"{name}."))
        else:
            out.append((name, value))
    return out


def row_name(row: dict[str, Any]) -> str:
    for field_name in _NAME_FIELDS:
        value = row.get(field_name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for value in row.values():
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "row"


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
        from ifc_console.knowledge.dependencies import missing_document_dependency

        raise ToolError(
            "EXTRA_NOT_INSTALLED",
            "PDF ingestion needs the pypdf package.",
            missing_document_dependency("pypdf"),
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
        # Every table-like line becomes a row record, so a designation search
        # lands on the row and lookup_table_rows can rank rows by value; the
        # digest is the one-page map of the document an agent reads first.
        rows_by_page: dict[int, list[dict[str, Any]]] = {}
        table_rows = 0
        for number, text in pages:
            found = pdf_table_rows(text)
            if not found:
                continue
            rows_by_page[number] = found
            for row in found:
                if table_rows >= _MAX_ROWS:
                    break
                table_rows += 1
                row = {**row, "page": number}
                body = "\n".join(
                    f"{key}: {value}" for key, value in _flatten(row) if value not in (None, "")
                )
                records.append(
                    Record(
                        kind="row",
                        key=f"row:{rel}#p{number}-{table_rows}",
                        name=f"{stem}: {row['name']}",
                        summary=_summary(body) or f"table row on page {number} of {path.name}",
                        body=body,
                        meta={
                            "path": rel,
                            "media": "pdf",
                            "sha256": sha,
                            "table": stem,
                            "page": number,
                            "line": table_rows,
                            "row": row,
                            "aliases": [stem, path.name, row["name"]],
                            **_flags(body),
                        },
                    )
                )
        entry["rows"] = table_rows
        if pages:
            digest = pdf_digest(pages, rows_by_page)
            records.append(
                Record(
                    kind="doc",
                    key=f"doc:{rel}#digest",
                    name=f"{title or stem} - digest",
                    summary=_summary(digest) or "document outline",
                    body=digest,
                    meta={
                        "path": rel,
                        "media": "pdf",
                        "sha256": sha,
                        "section": "digest",
                        "aliases": [stem, path.name, "digest", "outline", "contents"],
                    },
                )
            )
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
    elif suffix in TABLE_SUFFIXES:
        # every row is its own record, so a designation search lands on the
        # row and the row rides along in meta for deterministic lookup
        entry["media"] = "table"
        rows = chunk_table(path)
        entry["rows"] = len(rows)
        if not rows:
            entry["no_text"] = True
        for number, row in enumerate(rows, start=1):
            name = row_name(row)
            body = "\n".join(
                f"{key}: {value}" for key, value in _flatten(row) if value not in (None, "")
            )
            records.append(
                Record(
                    kind="row",
                    key=f"row:{rel}#{number}",
                    name=f"{stem}: {name}",
                    summary=_summary(body) or f"row {number} of {path.name}",
                    body=body,
                    meta={
                        "path": rel,
                        "media": "table",
                        "sha256": sha,
                        "table": stem,
                        "line": number,
                        "row": row,
                        "aliases": [stem, path.name, name],
                        **_flags(body),
                    },
                )
            )
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


__all__ = [
    "SUPPORTED_SUFFIXES",
    "TABLE_SUFFIXES",
    "chunk_markdown",
    "chunk_pdf",
    "chunk_table",
    "chunk_text",
    "file_records",
    "pdf_digest",
    "pdf_table_rows",
    "row_name",
]
