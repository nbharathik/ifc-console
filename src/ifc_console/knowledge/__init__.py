"""The offline knowledge base: IFC schema docs, property sets, and the
IfcOpenShell API, searchable without a network call.

Everything indexed here already ships inside the installed ifcopenshell wheel.
The index is built once into the user directory and reused; it is keyed by the
ifcopenshell version, so upgrading the library rebuilds it.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from ifc_console.knowledge.records import KINDS, Record
from ifc_console.knowledge.store import SCHEMA_VERSION, Store, build

log = logging.getLogger("ifc-console.knowledge")

# The corpus surface applications may build on: Record is the one shape every
# corpus produces, build() writes an index from any iterable of them, Store
# reads one back, and ProjectKnowledge is the per-project reference wiring.
__all__ = ["KINDS", "KnowledgeBase", "ProjectKnowledge", "Record", "Store", "build"]


def __getattr__(name: str):
    if name == "ProjectKnowledge":
        from ifc_console.knowledge.project import ProjectKnowledge

        return ProjectKnowledge
    raise AttributeError(name)


def _ifcopenshell_version() -> str:
    try:
        import ifcopenshell

        return str(ifcopenshell.version)
    except Exception:
        return "unknown"


def _iter_records(schemas: tuple[str, ...]):
    from ifc_console.knowledge import corpora, recipes

    for schema in schemas:
        try:
            yield from corpora.entities(schema)
            yield from corpora.property_sets(schema)
            yield from corpora.types(schema)
        except Exception:
            log.warning("knowledge: skipped schema %s", schema, exc_info=True)
    try:
        yield from corpora.api_functions()
    except Exception:
        log.warning("knowledge: skipped the ifcopenshell API corpus", exc_info=True)
    yield from recipes.records()


# Which schema represents a name that exists in several: IFC4 is what most
# files in the wild use.
_SCHEMA_RANK = {"IFC4": 3.0, "IFC4X3": 2.0, "IFC2X3": 1.0}


def _rank(row: dict[str, Any]) -> float:
    return row.get("score", 0.0) + _SCHEMA_RANK.get(row.get("schema", ""), 0.0) * 0.01


def _collapse_schemas(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """One hit per name when no schema was asked for, listing where it exists."""
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault((row["kind"], row["name"]), []).append(row)
    out: list[dict[str, Any]] = []
    for group in grouped.values():
        best = max(group, key=_rank)
        others = [
            row["schema"]
            for row in group
            if row.get("schema") and row.get("schema") != best.get("schema")
        ]
        if others:
            best["also_in"] = sorted(set(others))
        out.append(best)
    return sorted(out, key=lambda r: -r.get("score", 0))[:limit]


class KnowledgeBase:
    """Owns one index file: builds it on demand, then answers searches."""

    def __init__(self, home: Path, *, schemas: tuple[str, ...] | None = None) -> None:
        from ifc_console.knowledge.corpora import SCHEMAS

        self.home = home
        self.schemas = schemas or SCHEMAS
        self._store: Store | None = None
        self._lock = threading.Lock()
        self._building = False
        self.last_error: str | None = None

    @property
    def path(self) -> Path:
        version = _ifcopenshell_version().replace(" ", "")
        covered = "-".join(sorted(s.upper().removeprefix("IFC") for s in self.schemas)).lower()
        return (
            self.home
            / "knowledge"
            / f"kb-v{SCHEMA_VERSION}-ios{version}-{covered or 'none'}.sqlite"
        )

    @property
    def ready(self) -> bool:
        return self.path.exists()

    @property
    def building(self) -> bool:
        return self._building

    def build(self, *, force: bool = False) -> dict[str, Any]:
        """Build the index. Safe to call from a worker thread."""
        if self.ready and not force:
            return {"built": False, "path": str(self.path), **self.stats()}
        # Windows cannot replace a file that is still open for reading.
        self._close()
        self._building = True
        try:
            info = build(
                self.path,
                _iter_records(self.schemas),
                {
                    "ifcopenshell": _ifcopenshell_version(),
                    "schemas": list(self.schemas),
                },
            )
            self.last_error = None
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self._building = False
            self._close()
        self._prune_old()
        return {"built": True, "path": str(self.path), **info}

    def _prune_old(self) -> None:
        directory = self.path.parent
        for old in directory.glob("kb-*.sqlite"):
            if old != self.path:
                old.unlink(missing_ok=True)

    def _close(self) -> None:
        with self._lock:
            if self._store is not None:
                self._store.close()
                self._store = None

    def close(self) -> None:
        self._close()

    def _open(self) -> Store | None:
        with self._lock:
            if self._store is None:
                if not self.ready:
                    return None
                try:
                    self._store = Store(self.path)
                except Exception as exc:
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    return None
            return self._store

    def search(
        self,
        query: str,
        *,
        kind: str | tuple[str, ...] | None = None,
        schema: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        store = self._open()
        if store is None:
            return []
        with self._lock:
            rows = store.search(
                query, kind=kind, schema=schema, limit=limit if schema else limit * 3
            )
        return rows[:limit] if schema else _collapse_schemas(rows, limit)

    def get(self, key: str) -> dict[str, Any] | None:
        store = self._open()
        if store is None:
            return None
        with self._lock:
            return store.get(key)

    def lookup(
        self, name: str, *, kind: str | None = None, schema: str | None = None
    ) -> list[dict[str, Any]]:
        """Exact name lookup, which is what a tool argument usually gives us."""
        store = self._open()
        if store is None:
            return []
        with self._lock:
            return store.by_name(name, kind=kind, schema=schema)

    def stats(self) -> dict[str, Any]:
        store = self._open()
        if store is None:
            return {"ready": False, "building": self._building, "error": self.last_error}
        return {
            "ready": True,
            "building": self._building,
            "path": str(self.path),
            "search": "fts5" if store.fts else "like",
            **{k: v for k, v in store.meta.items() if k in ("counts", "total", "ifcopenshell")},
        }
