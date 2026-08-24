"""A deterministic offline provider used to rehearse the panel end to end.

The panel is hard to test because every path needs a paid key. The rehearsal
provider speaks the same normalized event vocabulary the real providers emit,
walks the same multi-round tool loop a real model would (scope, then evidence,
then measurement, then an optional proposal), and answers in markdown that
exercises tables, code fences, and citations. It is registered only when
``ifc-console dev`` or IFC_CONSOLE_DEV=1 asks for it, so a normal run cannot
reach it.
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Iterator
from typing import Any

from ifc_console.chat.providers import PROVIDERS, Provider

REHEARSAL_ID = "rehearsal"
REHEARSAL_MODELS = ("rehearsal-tools", "rehearsal-fast", "rehearsal-vision")
_ENV_FLAG = "IFC_CONSOLE_DEV"

REHEARSAL = Provider(
    id=REHEARSAL_ID,
    label="Rehearsal (offline test double)",
    family="rehearsal",
    # Loopback so chat.local_only accepts it. Nothing is ever sent here.
    base_url="http://127.0.0.1:1/rehearsal",
    needs_key=False,
    suggested_model="rehearsal-tools",
    note="Scripted answers for testing the panel. No network, no key, no cost.",
)

_PROPOSAL_TOOL = "measure__propose_measured_value"
_PDF_PATH = re.compile(r'"path"\s*:\s*"([^"]+\.pdf)"', re.IGNORECASE)
_IMAGE_PATH = re.compile(r'"path"\s*:\s*"([^"]+\.(?:png|jpe?g))"', re.IGNORECASE)


def rehearsal_enabled() -> bool:
    return REHEARSAL_ID in PROVIDERS


def enable_rehearsal_provider() -> Provider:
    """Add the rehearsal provider to the process-wide provider table."""
    PROVIDERS.setdefault(REHEARSAL_ID, REHEARSAL)
    os.environ.setdefault(_ENV_FLAG, "1")
    return REHEARSAL


def enable_from_environment() -> None:
    """Honour IFC_CONSOLE_DEV=1 so a plain `serve` can rehearse too."""
    if os.environ.get(_ENV_FLAG, "").strip().lower() in {"1", "true", "yes", "on"}:
        PROVIDERS.setdefault(REHEARSAL_ID, REHEARSAL)


# ------------------------------------------------------------------ transcript
def _chunks(text: str, size: int = 24) -> Iterator[str]:
    for start in range(0, len(text), size):
        yield text[start : start + size]


def _text_of(turn: dict[str, Any]) -> str:
    content = turn.get("text") or turn.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    return ""


def _user_intent(turns: list[dict[str, Any]]) -> str:
    """Every real user turn joined.

    The agent loop injects synthetic ``[image content from ...]`` user
    messages after a tool returns pixels, so "the last user message" is not
    the request once vision is in play.
    """
    said = [
        _text_of(turn)
        for turn in turns
        if turn.get("role") == "user" and not _text_of(turn).startswith("[image content from ")
    ]
    return "\n".join(said)


def _image_count(turns: list[dict[str, Any]]) -> int:
    """How many images actually reached the model as pixels."""
    total = 0
    for turn in turns:
        # AgentMessage.images is a tuple field, so model_dump keeps it a tuple.
        images = turn.get("images")
        if isinstance(images, (list, tuple)):
            total += len(images)
        content = turn.get("content")
        if isinstance(content, (list, tuple)):
            total += sum(
                1
                for part in content
                if isinstance(part, dict) and str(part.get("type", "")).startswith("image")
            )
    return total


def _tool_results(turns: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [
        (str(turn.get("name") or ""), _text_of(turn))
        for turn in turns
        if turn.get("role") == "tool"
    ]


def _called(turns: list[dict[str, Any]]) -> set[str]:
    return {name for name, _ in _tool_results(turns) if name}


def _first_match(pattern: re.Pattern[str], results: list[tuple[str, str]]) -> str | None:
    for _, body in results:
        found = pattern.search(body)
        if found:
            return found.group(1)
    return None


def _tool_names(tools: list[dict] | None) -> set[str]:
    names: set[str] = set()
    for tool in tools or []:
        name = tool.get("name")
        if isinstance(name, str):
            names.add(name)
            continue
        function = tool.get("function")
        if isinstance(function, dict) and isinstance(function.get("name"), str):
            names.add(function["name"])
    return names


def _call(index: int, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"rehearsal-{index}",
        "name": name,
        "arguments": json.dumps(arguments),
    }


# ----------------------------------------------------------------- round logic
def _round_one(available: set[str]) -> list[dict[str, Any]]:
    """Scope the work: what model is open, what evidence exists, what elements."""
    wanted = [
        ("get_ifc_project_info", {}),
        ("list_project_documents", {}),
        ("query_elements", {"query": "IfcWall", "limit": 10}),
    ]
    return [
        _call(index + 1, name, arguments)
        for index, (name, arguments) in enumerate(wanted)
        if name in available
    ]


def _round_two(
    available: set[str], results: list[tuple[str, str]]
) -> list[dict[str, Any]]:
    """Read the evidence and measure: pages, images, recipe, then geometry."""
    calls: list[dict[str, Any]] = []
    pdf = _first_match(_PDF_PATH, results)
    image = _first_match(_IMAGE_PATH, results)
    if pdf and "get_project_document_page" in available:
        calls.append(_call(len(calls) + 10, "get_project_document_page", {"path": pdf, "page": 1}))
    if image and "get_project_reference_image" in available:
        calls.append(
            _call(len(calls) + 10, "get_project_reference_image", {"path": image})
        )
    if "get_measurement_recipe" in available:
        calls.append(
            _call(
                len(calls) + 10,
                "get_measurement_recipe",
                {"ifc_class": "IfcWall", "property_name": "thickness"},
            )
        )
    if "measure_elements" in available:
        calls.append(
            _call(
                len(calls) + 10,
                "measure_elements",
                {
                    "method": "geometry_extent",
                    "selector": "IfcWall",
                    "metric": "thickness",
                    "axis": "local_y",
                },
            )
        )
    if "search_ifc_knowledge" in available and not calls:
        calls.append(
            _call(10, "search_ifc_knowledge", {"query": "wall thickness", "corpus": "project"})
        )
    return calls


def _global_ids(results: list[tuple[str, str]]) -> list[str]:
    found: list[str] = []
    for _, body in results:
        for match in re.finditer(r'"global_id"\s*:\s*"([A-Za-z0-9_$]{22})"', body):
            if match.group(1) not in found:
                found.append(match.group(1))
    return found[:5]


def _round_three(
    available: set[str], results: list[tuple[str, str]], prompt: str
) -> list[dict[str, Any]]:
    """Only when the user asked for a written value, and only as a preview."""
    if _PROPOSAL_TOOL not in available:
        return []
    if not re.search(r"propose|write|store|record|add", prompt, re.IGNORECASE):
        return []
    ids = _global_ids(results)
    if not ids:
        return []
    return [
        _call(
            20,
            _PROPOSAL_TOOL,
            {
                "global_ids": ids,
                "metric": "thickness",
                "value": 0.24,
                "unit": "m",
                "method": "geometry_extent (local_y)",
                "source": "Demo-Measurement-Manual.pdf p2",
                "confidence": "medium",
            },
        )
    ]


_ANSWER = """### Rehearsal answer

This reply came from the offline rehearsal provider, not from a real model.
It exists so the panel can be exercised without a provider key.

| Element | Metric | Value | Method | Source |
| --- | --- | --- | --- | --- |
| Interior Wall A | thickness | 240 mm | geometry_extent | Demo manual p2 |
| Interior Wall B | thickness | 300 mm | geometry_extent | Demo manual p2 |

```python
# renderer coverage: table, fence, list, inline `code`
print("rehearsal")
```

- The chips above are the real tool calls this run made.
- Nothing in the IFC file changed; any proposal is a preview only.
"""


def rehearsal_stream(
    provider: Provider,
    *,
    model: str,
    system: str,
    turns: list[dict],
    tools: list[dict] | None,
    options: dict,
    cancel: threading.Event | None = None,
    **_ignored: Any,
) -> Iterator[dict]:
    """Walk the same rounds a real model would, then answer."""
    del provider, system, options
    stop = cancel.is_set if cancel is not None else (lambda: False)
    prompt = _user_intent(turns)
    available = _tool_names(tools)
    results = _tool_results(turns)
    already = _called(turns)

    yield {"type": "reasoning", "text": "Rehearsing the panel; "}
    if stop():
        return
    yield {"type": "reasoning", "text": "no provider is contacted.\n"}

    if available and model != "rehearsal-fast":
        calls: list[dict[str, Any]] = []
        if not results:
            calls = _round_one(available)
            note = "Scoping the model and the project evidence.\n\n"
        elif not (already & {"get_project_document_page", "measure_elements", "search_ifc_knowledge"}):
            calls = _round_two(available, results)
            note = "Reading the evidence and measuring.\n\n"
        elif _PROPOSAL_TOOL not in already:
            calls = _round_three(available, results, prompt)
            note = "Preparing a reviewable proposal.\n\n"
        else:
            calls = []
            note = ""
        if calls:
            if not stop():
                yield {"type": "content", "text": note}
            yield {"type": "tool_calls", "calls": calls}
            yield {"type": "usage", "in": 480, "out": 32}
            return

    images = _image_count(turns)
    answer = _ANSWER
    if images:
        answer += f"\nThis run received {images} image(s) as pixels.\n"
    for chunk in _chunks(answer):
        if stop():
            return
        yield {"type": "content", "text": chunk}
    yield {"type": "usage", "in": 640, "out": 210}
    yield {"type": "finish", "reason": "stop"}


__all__ = [
    "REHEARSAL",
    "REHEARSAL_ID",
    "REHEARSAL_MODELS",
    "enable_from_environment",
    "enable_rehearsal_provider",
    "rehearsal_enabled",
    "rehearsal_stream",
]
