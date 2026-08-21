"""File kinds the workspace understands.

Extension first, then a cheap header sniff, because mislabelled files are
common in delivered BIM data. Nothing here parses a whole file: detection
reads at most SNIFF_BYTES, and describe() reads a bounded head.
"""

from __future__ import annotations

import contextlib
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SNIFF_BYTES = 4096
_DESCRIBE_BYTES = 512_000

_SCHEMA_RE = re.compile(r"FILE_SCHEMA\s*\(\s*\(\s*'([^']+)'")
_IDS_TITLE_RE = re.compile(
    r"<(?:\w+:)?title[^>]*>([^<]{1,200})</(?:\w+:)?title>", re.IGNORECASE
)
_IDS_SPEC_RE = re.compile(r"<(?:\w+:)?specification\b", re.IGNORECASE)


@dataclass(frozen=True)
class FileKind:
    name: str
    label: str  # badge shown in the console panel
    suffixes: tuple[str, ...]
    opens_as: str  # model | spec | issues | table
    consumed_by: str | None  # the tool that reads it, named in hints
    extra: str | None  # pip extra needed, None when bundled


KINDS: tuple[FileKind, ...] = (
    FileKind("ifc", "IFC", (".ifc", ".ifczip", ".ifcxml"), "model", "open_ifc_file", None),
    FileKind("ids", "IDS", (".ids",), "spec", "validate_ids", "validation"),
    FileKind("bcf", "BCF", (".bcf", ".bcfzip"), "issues", None, "bcf"),
    FileKind("csv", "CSV", (".csv",), "table", None, None),
    FileKind("ifcjson", "JSON", (".ifcjson",), "model", None, None),
    # documents feed the project knowledge corpus via `ifc-console knowledge ingest`
    FileKind("pdf", "PDF", (".pdf",), "document", None, "pdf"),
    FileKind("md", "MD", (".md", ".markdown"), "document", None, None),
    FileKind("txt", "TXT", (".txt",), "document", None, None),
    FileKind("image", "IMG", (".png", ".jpg", ".jpeg"), "document", None, None),
)

_BY_NAME = {k.name: k for k in KINDS}
_BY_SUFFIX = {suffix: k for k in KINDS for suffix in k.suffixes}

SUPPORTED_SUFFIXES = tuple(sorted(_BY_SUFFIX))

# Suffixes worth opening to identify. Anything else is decided by extension
# alone, so a workspace scan does not read the head of every file in the tree.
SNIFFABLE_SUFFIXES = frozenset(
    {*SUPPORTED_SUFFIXES, "", ".txt", ".dat", ".xml", ".json", ".zip", ".step", ".stp",
     ".spf", ".p21"}
)


def kind(name: str) -> FileKind | None:
    return _BY_NAME.get(name)


def _head(path: Path, size: int = SNIFF_BYTES) -> bytes:
    try:
        with path.open("rb") as fh:
            return fh.read(size)
    except OSError:
        return b""


def _zip_kind(path: Path) -> str | None:
    """Classify a zip by its member names without extracting anything."""
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()[:200]
    except (OSError, zipfile.BadZipFile):
        return None
    lowered = [n.lower() for n in names]
    if any(n.endswith((".ifc", ".ifcxml")) for n in lowered):
        return "ifc"
    if any(n.endswith(("markup.bcf", "bcf.version", ".bcfp")) for n in lowered):
        return "bcf"
    return None


def _sniff(head: bytes, path: Path) -> str | None:
    if head[:12] == b"ISO-10303-21":
        return "ifc"
    if head[:4] == b"PK\x03\x04":
        return _zip_kind(path)
    stripped = head.lstrip()[:4096]
    if stripped[:1] == b"<":
        low = stripped.lower()
        if b"<ids" in low or b"information delivery specification" in low:
            return "ids"
        if b"ifcxml" in low or b"ifc4" in low or b"ifc2x3" in low:
            return "ifc"
    if stripped[:1] == b"{" and (b'"file_schema"' in stripped or b'"IFC' in stripped):
        return "ifcjson"
    return None


def detect_kind(path: Path) -> str | None:
    """The file's kind, or None when nothing recognizes it.

    The sniff wins over the extension: a .ifc holding XML is IFC either way,
    but a .txt holding an SPF header is still an IFC model.
    """
    suffix = path.suffix.lower()
    by_suffix = _BY_SUFFIX.get(suffix)
    if suffix not in SNIFFABLE_SUFFIXES:
        # A suffix we neither support nor mistrust: no read, no kind.
        return by_suffix.name if by_suffix else None
    sniffed = _sniff(_head(path), path)
    if sniffed is not None:
        return sniffed
    if by_suffix is not None and by_suffix.name in ("csv", "ids", "bcf"):
        # text formats with no reliable magic: trust the extension
        return by_suffix.name
    return by_suffix.name if by_suffix else None


def peek_schema(path: Path) -> str | None:
    """The IFC schema from the SPF header, without opening the model."""
    head = _head(path).decode("ascii", errors="ignore")
    match = _SCHEMA_RE.search(head)
    return match.group(1) if match else None


def _describe_ifc(path: Path) -> dict[str, Any]:
    detail: dict[str, Any] = {}
    schema = peek_schema(path)
    if schema:
        detail["schema"] = schema
    elif path.suffix.lower() in (".ifczip", ".ifcxml"):
        detail["schema"] = None
        detail["note"] = "schema readable only after opening this container"
    return detail


def _describe_ids(path: Path) -> dict[str, Any]:
    text = _head(path, _DESCRIBE_BYTES).decode("utf-8", errors="ignore")
    detail: dict[str, Any] = {"specifications": len(_IDS_SPEC_RE.findall(text))}
    match = _IDS_TITLE_RE.search(text)
    if match:
        detail["title"] = match.group(1).strip()
    return detail


def _describe_bcf(path: Path) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
    except (OSError, zipfile.BadZipFile):
        return {}
    topics = {n.split("/", 1)[0] for n in names if "/" in n and n.lower().endswith("markup.bcf")}
    return {"topics": len(topics)}


def _describe_csv(path: Path) -> dict[str, Any]:
    head = _head(path).decode("utf-8", errors="ignore")
    first = head.splitlines()[0] if head else ""
    if not first:
        return {}
    columns = [c.strip() for c in first.split(",")][:20]
    return {"columns": columns}


_DESCRIBERS = {
    "ifc": _describe_ifc,
    "ids": _describe_ids,
    "bcf": _describe_bcf,
    "csv": _describe_csv,
}


def describe_file(path: Path, kind_name: str | None = None) -> dict[str, Any]:
    """Cheap, bounded metadata for one file. Never loads a model."""
    name = kind_name or detect_kind(path)
    if name is None:
        return {}
    describer = _DESCRIBERS.get(name)
    if describer is None:
        return {}
    with contextlib.suppress(Exception):
        return describer(path)
    return {}
