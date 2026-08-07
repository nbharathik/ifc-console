# Python SDK

Everything the LLM can do, from a script. No server, no terminal, no port, no
token: `Workbench` opens a model in this process and calls the same tool
functions the MCP layer serves.

```python
from ifc_console import Workbench

with Workbench.open("tower.ifc") as wb:
    print(wb.info()["project"]["name"])
    walls = wb.query("IfcWall, Pset_WallCommon.FireRating=F30")
    print(len(walls), "fire-rated walls")
```

The workbench closes its model and worker threads on exit, so use it as a
context manager (or call `close()` yourself).

## The mode switch still applies

`Workbench.open(..., mode="ask")` is the default and behaves exactly like the
console: reads run, mutations are refused.

```python
wb = Workbench.open("tower.ifc", mode="edit")   # you are the human in the loop
wb.run_code("ifc_api.attribute.edit_attributes(ifc, product=..., attributes=...)",
            "rename the walls")
wb.save()
```

In the console the user owns that switch and the model cannot touch it. In a
script you are the user, so `set_mode()` is yours. If you build an agent, keep
`set_mode` in your own code and never expose it as a tool.

## Reads

| method | returns |
| ------ | ------- |
| `info()` | project summary: schema, units, counts, materials |
| `orient()` | status, summary, and the spatial tree in one call |
| `tree(root=None, depth=10)` | the spatial containment tree |
| `query(selector, limit=50, fields=None)` | element rows for a selector |
| `element(global_ids, include=[...])` | full detail per element |
| `psets(global_ids)` | property sets and quantity sets |
| `quantities(selector, by="storey")` | quantity takeoff, aggregated |
| `validate()` / `validate_ids(path)` | schema issues / IDS report |
| `clashes(set_a, set_b, tolerance=0.01)` | geometric clashes |
| `georeferencing()` | CRS, map conversion, true north |
| `schema_docs(entity=..., pset=..., property=...)` | IFC documentation |

Errors raise `IfcConsoleError`, which carries the same `code`, `message`, and
`hint` the LLM would see:

```python
from ifc_console import IfcConsoleError

try:
    wb.query("IfcWall, ((broken")
except IfcConsoleError as exc:
    print(exc.code, exc.hint)   # INVALID_QUERY, and what to do about it
```

## More than one model

```python
wb.attach("structural.ifc")
wb.attach("mep.ifc")
print(wb.models()["models"])
hits = wb.clashes("IfcWall", "IfcDuctSegment", other_model="mep", tolerance=0.02)
```

One model stays active and writable; attached models are read only, exactly as
in the console.

## The knowledge index

```python
wb.build_knowledge()                       # once, a few seconds, no network
wb.search_knowledge("which pset carries fire rating")
wb.api_docs("pset.add_pset")["meta"]["signature"]
```

## Agent bindings, provider neutral

`tools()` hands out plain JSON Schema definitions and `call()` runs one by
name. No LLM vendor is involved, and nothing here depends on a particular API
client, so the same two calls drive any provider's tool-use loop.

```python
tools = wb.tools()
# [{"name": "query_elements", "description": "...", "input_schema": {...}}, ...]

result = wb.call("query_elements", query="IfcDoor", limit=10)
# {"ok": True, "data": {...}, "meta": {...}}
```

A minimal loop looks like this, whatever the provider:

```python
def run_tool_call(name, arguments):
    envelope = wb.call(name, **arguments)
    return envelope          # errors included; the hint is written for a model
```

Feed `tools()` to your client as its tool list, route every tool call through
`run_tool_call`, and hand the envelope back as the tool result. Failures come
back as data (`ok: False` plus a hint), which is what lets a model correct
itself instead of stopping.

Two things worth keeping out of the model's reach: `set_mode` (the human owns
it) and any code path that changes allowed directories.

## Async

`AsyncWorkbench` is the same surface as coroutines, for code that already runs
an event loop:

```python
from ifc_console import AsyncWorkbench

wb = await AsyncWorkbench.create("tower.ifc")
walls = await wb.query("IfcWall")
wb.close()
```

`Workbench` is a thin synchronous wrapper around it, running its own event loop
on a private thread.

## Options

```python
Workbench.open(
    "tower.ifc",
    mode="ask",                       # or "edit"
    home="/tmp/ifc-home",             # where settings, audit, and the index live
    allowed_dirs=("/data/models",),   # extra readable roots
    settings={"knowledge.enabled": False},   # in-memory overrides
)
```

`settings` accepts any dotted key from [Settings](settings.md) and never writes
to the user's settings file.
