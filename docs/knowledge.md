# Knowledge index

An offline reference the LLM can search before it writes code or a selector.
It exists because the most common failure mode is a plausible guess: a property
set that does not exist, an API function with the wrong name, a property on the
wrong entity.

Everything indexed here already ships inside the ifcopenshell package you
installed. Nothing is downloaded, nothing is sent anywhere, and the index works
with no network at all.

## What is in it

| kind | what it holds | count (IFC2X3 + IFC4 + IFC4X3) |
| ---- | ------------- | ------------------------------ |
| `entity` | IFC entity documentation, supertypes, attributes, predefined types | 2,305 |
| `pset` | property and quantity sets, with the entities they apply to | 1,592 |
| `property` | one record per property, for reverse lookup | 8,555 |
| `type` | defined types | 1,160 |
| `api` | every `ifcopenshell.api` function with its signature and docstring | 380 |
| `recipe` | verified snippets for `execute_ifc_code` | 25 |

About 14,000 records, roughly 23 MB on disk.

Recipes are not decoration: every one of them is executed against a fixture
model by the test suite, read-only recipes with mutation locked and editing
recipes against a throwaway copy. A recipe that does not run does not ship.

## Where it lives

`~/.ifc-console/knowledge/kb-v1-ios<version>-<schemas>.sqlite`, built once and
reused. The filename carries the ifcopenshell version and the indexed schemas,
so upgrading the library or changing `knowledge.schemas` builds a fresh index
and prunes the old one.

It builds itself in the background the first time you start the console, which
takes a few seconds. Searches before it lands report `KNOWLEDGE_NOT_READY` with
a hint to retry; `get_schema_docs` needs no index and always works.

## Using it

From your LLM client:

- `search_ifc_knowledge(query, kind, schema, limit)` searches everything in
  plain words, and returns ranked hits with a `key`.
- `get_knowledge_record(key)` returns the full text of one hit.
- `get_api_docs(function="pset.add_pset")` returns the exact call signature.
- `get_schema_docs(entity=..., pset=..., property=...)` answers schema
  questions directly, with or without the index.

From the terminal:

```
/kb fire rating
/kb assign material
/kb                     shows the index status
```

From the shell:

```bash
ifc-console knowledge build
ifc-console knowledge status
ifc-console knowledge search "which pset carries fire rating"
```

From the SDK: `wb.search_knowledge(...)`, `wb.knowledge_record(key)`, and
`wb.api_docs(...)`.

## Why SQLite and not embeddings

`sqlite3` with FTS5 is in the Python standard library on every platform we
support, so the index costs no new dependency, no model download, and no
network call. The corpus is thousands of short records of exact technical
vocabulary (`FireRating`, `IfcWallStandardCase`), which is where keyword search
with BM25 ranking does well. Ranking is deterministic, which matters for a tool
whose output ends up in an audit log.

Two things make plain keyword search behave: identifier names are split into
their word parts when indexed (`IfcWallStandardCase` also matches "wall"), and
an exact name match gets a fixed bonus so it outranks a lucky hit deep in some
other document.

If a build of Python ships without FTS5, the index still works and falls back
to a scan.

## Settings

| key | default | meaning |
| --- | ------- | ------- |
| `knowledge.enabled` | `true` | expose the knowledge tools at all |
| `knowledge.autobuild` | `true` | build in the background on first use |
| `knowledge.schemas` | all three | index fewer schemas for a smaller, faster index |
| `knowledge.max_results` | `10` | default hit count |
