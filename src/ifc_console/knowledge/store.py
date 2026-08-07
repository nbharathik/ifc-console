"""SQLite storage and search for the knowledge index.

`sqlite3` is in the standard library on every platform we support, and FTS5 is
compiled into the CPython builds we care about, so the index costs no new
dependency, no download, and no network. Builds where FTS5 is missing fall back
to a LIKE scan over the same table.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ifc_console.knowledge.records import Record, expand_terms

SCHEMA_VERSION = 1

_DDL = """
CREATE TABLE record (
    id      INTEGER PRIMARY KEY,
    key     TEXT UNIQUE NOT NULL,
    kind    TEXT NOT NULL,
    schema  TEXT NOT NULL DEFAULT '',
    name    TEXT NOT NULL,
    terms   TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    body    TEXT NOT NULL DEFAULT '',
    meta    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX record_kind ON record(kind, schema);
CREATE INDEX record_name ON record(name COLLATE NOCASE);
CREATE TABLE kb_meta (key TEXT PRIMARY KEY, value TEXT);
"""

_FTS_DDL = """
CREATE VIRTUAL TABLE record_fts USING fts5(
    name, terms, summary, body,
    content='record', content_rowid='id', tokenize='unicode61'
);
"""

# Column weights for bm25: a hit on the name beats a hit deep in the body.
_RANK = "bm25(record_fts, 8.0, 5.0, 2.0, 1.0)"

_WORD = re.compile(r"[A-Za-z0-9_]+")
_NON_ALNUM = re.compile(r"[^a-z0-9]")
_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "does", "for", "from",
    "get", "give", "have", "how", "i", "if", "in", "into", "is", "it", "me", "my", "of",
    "on", "or", "should", "show", "that", "the", "their", "them", "this", "to", "use",
    "using", "what", "when", "where", "which", "who", "why", "with", "you", "your",
})
# How people say it, mapped to how the schema spells it.
_SYNONYMS = {
    "guid": "globalid",
    "level": "storey",
    "floor": "storey",
    "psets": "pset",
    "qtos": "qto",
    "quantities": "quantity",
}
_PHRASES = {
    ("property", "set"): "pset",
    ("quantity", "set"): "qto",
    ("quantity", "take", "off"): "qto",
}


def fts5_available() -> bool:
    try:
        with sqlite3.connect(":memory:") as conn:
            conn.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        return True
    except sqlite3.Error:
        return False


def query_tokens(text: str) -> list[str]:
    """Words worth searching for, with identifier names split into parts."""
    out: list[str] = []
    for raw in _WORD.findall(text or ""):
        for word in expand_terms(raw).split():
            if len(word) > 1 and word not in _STOPWORDS:
                out.append(_SYNONYMS.get(word, word))
    for phrase, replacement in _PHRASES.items():
        for i in range(len(out) - len(phrase) + 1):
            if tuple(out[i : i + len(phrase)]) == phrase:
                out.append(replacement)
    seen: dict[str, None] = {}
    for word in out:
        seen.setdefault(word, None)
    return list(seen)


def name_boost(name: str, terms: str, tokens: list[str]) -> float:
    """Deterministic bonus so an exact name match outranks a lucky body hit."""
    flat = _NON_ALNUM.sub("", name.lower())
    joined = "".join(tokens)
    bonus = 0.0
    if flat == joined:
        bonus += 12.0
    elif flat.startswith(joined) or flat.endswith(joined):
        bonus += 8.0
    elif joined and joined in flat:
        bonus += 5.0
    words = set(terms.lower().split())
    covered = sum(1 for token in tokens if token in words)
    return bonus + 3.0 * covered / max(1, len(tokens))


def _match_expression(tokens: list[str], *, operator: str, prefix: bool) -> str:
    parts = []
    for token in tokens:
        star = "*" if prefix and len(token) >= 4 else ""
        parts.append(f'"{token}"{star}')
    return f" {operator} ".join(parts)


def build(path: Path, records: Iterable[Record], meta: dict[str, Any]) -> dict[str, Any]:
    """Write a fresh index, then move it into place in one step."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".building")
    os.close(fd)
    tmp = Path(tmp_name)
    counts: dict[str, int] = {}
    has_fts = fts5_available()
    try:
        conn = sqlite3.connect(str(tmp))
        try:
            conn.executescript(_DDL)
            if has_fts:
                conn.executescript(_FTS_DDL)
            rows = []
            for record in records:
                counts[record.kind] = counts.get(record.kind, 0) + 1
                rows.append(
                    (
                        record.key,
                        record.kind,
                        record.schema,
                        record.name,
                        record.terms,
                        record.summary,
                        record.body,
                        json.dumps(record.meta, default=str),
                    )
                )
                if len(rows) >= 2000:
                    _insert(conn, rows)
                    rows.clear()
            if rows:
                _insert(conn, rows)
            if has_fts:
                conn.execute("INSERT INTO record_fts(record_fts) VALUES('rebuild')")
            info = {
                **meta,
                "schema_version": SCHEMA_VERSION,
                "fts": has_fts,
                "counts": counts,
                "total": sum(counts.values()),
            }
            conn.executemany(
                "INSERT INTO kb_meta(key, value) VALUES (?, ?)",
                [(k, json.dumps(v, default=str)) for k, v in info.items()],
            )
            conn.commit()
            conn.execute("VACUUM")
        finally:
            conn.close()
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    info["size_bytes"] = path.stat().st_size
    return info


def _insert(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO record(key, kind, schema, name, terms, summary, body, meta) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )


class Store:
    """Read-only view over one built index."""

    def __init__(self, path: Path) -> None:
        self.path = path
        # Read-only, and shared across threads under the caller's lock.
        self._conn = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro", uri=True, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self.meta = {
            row["key"]: json.loads(row["value"])
            for row in self._conn.execute("SELECT key, value FROM kb_meta")
        }
        self.fts = bool(self.meta.get("fts"))

    def close(self) -> None:
        self._conn.close()

    def get(self, key: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM record WHERE key = ?", (key,)).fetchone()
        return self._row(row, body=True) if row else None

    def by_name(self, name: str, *, kind: str | None = None, schema: str | None = None):
        sql = "SELECT * FROM record WHERE name = ? COLLATE NOCASE"
        args: list[Any] = [name]
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        if schema:
            sql += " AND schema = ?"
            args.append(schema)
        return [self._row(row, body=True) for row in self._conn.execute(sql, args)]

    def search(
        self,
        query: str,
        *,
        kind: str | tuple[str, ...] | None = None,
        schema: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        tokens = query_tokens(query)
        if not tokens:
            return []
        kinds = (kind,) if isinstance(kind, str) else kind
        if not self.fts:
            return self._like_search(tokens, kinds, schema, limit)
        # Every word (precision) unioned with any word (recall), then reranked
        # so a name that matches what was typed wins over a lucky body hit.
        pool: dict[str, dict[str, Any]] = {}
        for operator, prefix in (("AND", False), ("OR", True)):
            for row in self._fts_search(
                _match_expression(tokens, operator=operator, prefix=prefix),
                kinds,
                schema,
                limit * 4,
                tokens,
            ):
                pool.setdefault(row["key"], row)
            if len(pool) >= limit * 4:
                break
        rows = sorted(pool.values(), key=lambda r: -r["score"])
        return rows[:limit]

    def _fts_search(
        self, match: str, kinds, schema, limit: int, tokens: list[str]
    ) -> list[dict[str, Any]]:
        sql = [
            "SELECT r.*, ",
            f"{_RANK} AS score, ",
            "snippet(record_fts, 3, '', '', ' … ', 24) AS snippet ",
            "FROM record_fts JOIN record r ON r.id = record_fts.rowid ",
            "WHERE record_fts MATCH ?",
        ]
        args: list[Any] = [match]
        if kinds:
            sql.append(f" AND r.kind IN ({','.join('?' * len(kinds))})")
            args.extend(kinds)
        if schema:
            sql.append(" AND (r.schema = ? OR r.schema = '')")
            args.append(schema)
        sql.append(" ORDER BY score LIMIT ?")
        args.append(limit)
        try:
            rows = self._conn.execute("".join(sql), args).fetchall()
        except sqlite3.OperationalError:
            return []
        return [
            self._row(
                row,
                snippet=row["snippet"],
                score=-row["score"] + name_boost(row["name"], row["terms"], tokens),
            )
            for row in rows
        ]

    def _like_search(self, tokens, kinds, schema, limit: int) -> list[dict[str, Any]]:
        clauses = " AND ".join(["(name LIKE ? OR terms LIKE ? OR body LIKE ?)"] * len(tokens))
        args: list[Any] = []
        for token in tokens:
            args.extend([f"%{token}%"] * 3)
        sql = f"SELECT * FROM record WHERE {clauses}"
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            args.extend(kinds)
        if schema:
            sql += " AND (schema = ? OR schema = '')"
            args.append(schema)
        sql += " ORDER BY length(name) LIMIT ?"
        args.append(limit)
        rows = self._conn.execute(sql, args).fetchall()
        return [self._row(row) for row in rows]

    def _row(
        self,
        row: sqlite3.Row,
        *,
        body: bool = False,
        snippet: str | None = None,
        score: float | None = None,
    ) -> dict[str, Any]:
        out: dict[str, Any] = {
            "key": row["key"],
            "kind": row["kind"],
            "name": row["name"],
            "summary": row["summary"],
        }
        if row["schema"]:
            out["schema"] = row["schema"]
        meta = json.loads(row["meta"] or "{}")
        meta.pop("aliases", None)
        out["meta"] = {k: v for k, v in meta.items() if v not in (None, [], {})}
        if snippet:
            out["snippet"] = snippet
        if score is not None:
            out["score"] = round(score, 3)
        if body:
            out["body"] = row["body"]
        return out
