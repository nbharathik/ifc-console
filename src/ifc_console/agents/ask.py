"""One question to a model, run on the bundled Agent (behind Workbench.ask)."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ifc_console.agents import Agent, AgentMessage, InMemoryThreadStore, ProviderModel
from ifc_console.agents.chat import SYSTEM_PROMPT
from ifc_console.agents.chat.events import tool_event
from ifc_console.agents.chat.providers import PROVIDERS
from ifc_console.sdk import IfcConsoleError
from ifc_console.toolsets import Toolset

if TYPE_CHECKING:
    from ifc_console.sdk import AsyncWorkbench


async def ask(
    workbench: AsyncWorkbench,
    prompt: str,
    *,
    provider: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    system: str | None = None,
    tools: bool = True,
    history: list[dict[str, Any]] | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    **options: Any,
) -> dict[str, Any]:
    from ifc_console.runtime import LocalRuntime

    core = workbench.core
    chosen = (provider or core.chat.provider or "openai").lower()
    spec = PROVIDERS.get(chosen)
    if spec is None:
        raise IfcConsoleError(
            "CHAT_FAILED", f"unknown provider {chosen!r}", f"Use one of {sorted(PROVIDERS)}."
        )
    model_id = (model or core.chat.model or spec.suggested_model).strip()
    if not model_id:
        raise IfcConsoleError("CHAT_FAILED", f"pick a model for {spec.label} first", "Pass model=...")
    turns = list(history or []) + [{"role": "user", "text": prompt}]
    store = InMemoryThreadStore()
    earlier = [
        AgentMessage(role=turn["role"], text=str(turn.get("text", "")))
        for turn in turns[:-1]
        if turn.get("role") in ("user", "assistant")
    ]
    await store.save("ask", earlier)
    runtime = LocalRuntime.from_workbench(workbench)
    agent = Agent(
        name="ask",
        model=ProviderModel(
            provider=chosen,
            model=model_id,
            api_key=api_key or core.chat.key_for(chosen) or None,
            base_url=base_url or core.chat.base_url or None,
            local_only=core.settings.chat.local_only,
            timeout_s=float(core.settings.chat.timeout_s),
            options=options,
        ),
        tools=await runtime.toolset() if tools else Toolset(()),
        instructions=(system or "").strip() or SYSTEM_PROMPT,
        thread_store=store,
    )
    core.audit.record("chat_request", provider=chosen, model=model_id, tools=bool(tools))
    parts: list[str] = []
    calls: list[dict[str, Any]] = []
    usage: dict[str, Any] = {}
    error: str | None = None
    async for event in agent.stream(prompt, thread_id="ask"):
        if event.type == "text_delta":
            piece = event.text or ""
            parts.append(piece)
            legacy: dict[str, Any] = {"type": "content", "text": piece}
        elif event.type == "tool_call_started":
            legacy = {
                "type": "tool_call",
                "id": event.tool_call_id,
                "name": event.tool_name,
                "arguments": event.arguments or {},
            }
        elif event.type == "tool_call_finished":
            info = tool_event(event.result or {})
            calls.append({"name": event.tool_name, "ok": info["ok"], "summary": info["summary"]})
            legacy = {"type": "tool_result", "id": event.tool_call_id, "name": event.tool_name, **info}
        elif event.type == "usage" and event.usage is not None:
            tokens = (("in", event.usage.input_tokens), ("out", event.usage.output_tokens))
            legacy = {"type": "usage", **dict(tokens)}
            for key, value in tokens:
                if value is not None:
                    usage[key] = usage.get(key, 0) + value
        elif event.type == "run_failed":
            error = event.text or "the run failed"
            legacy = {"type": "error", "text": error}
        else:
            continue
        if on_event is not None:
            on_event(legacy)
    text = "".join(parts).strip()
    if error and not text:
        raise IfcConsoleError("CHAT_FAILED", error, "Check the provider, model, and key.")
    turns.append({"role": "assistant", "text": text})
    return {"text": text, "tool_calls": calls, "usage": usage, "turns": turns, "error": error}
