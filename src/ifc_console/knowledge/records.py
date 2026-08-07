"""The one record shape every knowledge corpus produces."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Every kind that may appear in the index. Tools filter on these.
KINDS = ("entity", "pset", "property", "type", "api", "recipe")

_CAMEL = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")
_PREFIXES = ("Ifc", "Pset_", "Qto_")


def expand_terms(*names: str) -> str:
    """Searchable word forms for identifier-shaped names.

    `unicode61` tokenizes `IfcWallStandardCase` as one token, so a search for
    "wall" would miss it. Splitting the camel case and dropping the domain
    prefixes gives the tokenizer something a person would actually type.
    """
    out: list[str] = []
    for name in names:
        if not name:
            continue
        out.append(name)
        for prefix in _PREFIXES:
            if name.startswith(prefix) and len(name) > len(prefix):
                out.append(name[len(prefix) :])
        out.extend(_CAMEL.findall(name.replace("_", " ")))
    seen: dict[str, None] = {}
    for word in out:
        seen.setdefault(word.lower(), None)
    return " ".join(seen)


@dataclass(frozen=True)
class Record:
    kind: str
    key: str
    name: str
    summary: str
    body: str = ""
    schema: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def terms(self) -> str:
        return expand_terms(self.name, *self.meta.get("aliases", []))
