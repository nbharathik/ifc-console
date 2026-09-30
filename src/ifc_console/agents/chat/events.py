"""The panel-facing view of one tool result."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

# What the panel shows under a tool call. Smaller than the model's copy: a
# reader skims it, and the console keeps the full envelope.
TOOL_PREVIEW_LIMIT = 1600
_PREVIEW_ROWS = 50


def _light(value: Any, depth: int = 0) -> Any:
    """The same value with image bytes replaced by a count.

    Base64 pixels are for the model, never for the panel: one screenshot would
    otherwise put a megabyte of text into the transcript.
    """
    if depth > 6:
        return "..."
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if key == "images" and isinstance(item, (list, tuple)):
                out[key] = f"{len(item)} image(s)"
            else:
                out[key] = _light(item, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        rows = list(value)
        shown = [_light(item, depth + 1) for item in rows[:_PREVIEW_ROWS]]
        # Never a silent cut: a reader who sees 50 rows must know there were
        # 400, or they will read the preview as the whole answer.
        if len(rows) > _PREVIEW_ROWS:
            shown.append(f"...{len(rows) - _PREVIEW_ROWS} more not shown")
        return shown
    if isinstance(value, str) and len(value) > 2000:
        return value[:2000] + "..."
    return value


def _inspectable(value: Any, depth: int = 0) -> Any:
    """A complete panel result with binary image payloads made harmless."""
    if depth > 12:
        return "..."
    if isinstance(value, Mapping):
        return {
            key: (
                f"{len(item)} image(s)"
                if key == "images" and isinstance(item, (list, tuple))
                else _inspectable(item, depth + 1)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_inspectable(item, depth + 1) for item in value]
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"{len(value)} binary byte(s)"
    return value


def _error_text(error: Mapping[str, Any]) -> str:
    """One failure, written the way the console would say it out loud."""
    parts = [str(error.get("message") or "").strip()]
    if error.get("hint"):
        parts.append(f"Hint: {str(error['hint']).strip()}")
    extra = {
        key: value for key, value in error.items() if key not in {"code", "message", "hint"}
    }
    if extra:
        parts.append(json.dumps(_light(extra), default=str, ensure_ascii=False, indent=1))
    body = "\n\n".join(part for part in parts if part)
    return body or str(error.get("code") or "the tool failed without a message")


def tool_event(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The panel-facing view of one tool result.

    The panel draws a tool where it ran, so it needs more than "ok": the row
    count, the error the console reported, and a readable slice of the data.
    The bounded console envelope is kept as structured output as well. The
    browser only turns that into DOM when the reader opens the call, so a
    complete inspectable result does not make streamed rendering expensive.
    """
    ok = bool(payload.get("ok"))
    meta = payload.get("meta") if isinstance(payload.get("meta"), Mapping) else {}
    error = payload.get("error") if isinstance(payload.get("error"), Mapping) else {}
    rows = meta.get("returned") if isinstance(meta, Mapping) else None
    if ok:
        summary = f"{rows} row(s)" if rows is not None else "ok"
        detail = ""
        text = json.dumps(_light(payload.get("data")), default=str, ensure_ascii=False, indent=1)
    else:
        summary = str(error.get("code") or "failed")
        detail = str(error.get("message") or error.get("hint") or "")
        # A failure is prose, not a record. Rendering it as JSON turned the
        # parser's own line breaks into a wall of literal \n for the reader.
        text = _error_text(error)
    if len(text) > TOOL_PREVIEW_LIMIT:
        text = text[:TOOL_PREVIEW_LIMIT] + "\n... truncated"
    return {
        "ok": ok,
        "summary": summary,
        "rows": rows if isinstance(rows, int) else None,
        "detail": detail[:400],
        "preview": text,
        # Operation envelopes have already been capped by output_char_limit.
        # Round-trip through JSON so custom scalar types cannot break SSE.
        "output": json.loads(
            json.dumps(_inspectable(payload), default=str, ensure_ascii=False)
        ),
    }
