"""Run one agent pack from the shell: `ifc-console agents run <name> ...`.

Standalone opens a project folder or model file; attach connects to a
running console over MCP, which keeps its own mode switch and approvals.
The provider key comes from the system keyring (`ifc-console keys set`) or
the environment.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any


def resolve_model_file(path: Path) -> tuple[Path, Path | None]:
    """(project_dir, model file or None) from a folder or a direct .ifc path."""
    if path.is_file():
        return path.parent, path
    if not path.is_dir():
        raise SystemExit(f"{path} is neither an IFC file nor a project folder")
    candidates = sorted(path.glob("*.ifc")) or sorted(path.rglob("*.ifc"))
    if len(candidates) > 1:
        names = ", ".join(str(c.relative_to(path)) for c in candidates[:8])
        raise SystemExit(f"{path} holds several models ({names}); pass the file to open")
    return path, candidates[0] if candidates else None


async def _chat_loop(agent: Any, one_shot: str | None) -> None:
    async def ask(prompt: str) -> None:
        async for event in agent.stream(prompt, thread_id="shell"):
            if event.type == "text_delta":
                print(event.text or "", end="", flush=True)
            elif event.type == "tool_call_started":
                print(f"[tool] {event.tool_name}", flush=True)
            elif event.type == "run_failed":
                print(f"[failed] {event.text}")
        print()

    if one_shot:
        await ask(one_shot)
        return
    print("Type 'quit' to exit.")
    while True:
        prompt = await asyncio.to_thread(input, "> ")
        if prompt.strip().casefold() in {"quit", "exit"}:
            return
        if prompt.strip():
            await ask(prompt.strip())


async def run_pack(
    pack: Any,
    *,
    path: Path | None,
    attach: str | None,
    token: str | None,
    provider: str,
    model_id: str,
    base_url: str | None,
    prompt: str | None,
    viewer: bool,
    home: Path | None,
) -> None:
    from ifc_console import ConsoleRuntime, LocalRuntime

    from ifc_console_agents.providers import ProviderModel

    model = ProviderModel(
        provider=provider,
        model=model_id,
        base_url=base_url,
        local_only=provider == "local",
    )
    if attach:
        runtime = await ConsoleRuntime.connect_http(attach, token=token)
        async with runtime:
            agent = await pack.build(runtime, model=model, viewer=viewer)
            await _chat_loop(agent, prompt)
        return
    if path is None:
        raise SystemExit("pass a project folder or .ifc file, or use --attach")
    project_dir, model_file = resolve_model_file(path)
    # Pick up references copied directly into the managed project folder before
    # constructing the agent. Indexing failures are non-fatal: model-only work
    # can continue and the relevant knowledge tool will return a precise error.
    from ifc_console.knowledge.project import ProjectKnowledge

    from ifc_console_agents.files import AgentReferenceStore

    knowledge = ProjectKnowledge(project_dir)
    try:
        try:
            AgentReferenceStore(project_dir).sync(knowledge)
        except Exception as exc:
            print(f"[references] could not refresh: {exc}")
    finally:
        knowledge.close()
    runtime = await LocalRuntime.open(home=home, project_dir=project_dir)
    async with runtime:
        if model_file is not None:
            await runtime.open_model(model_file)
        agent = await pack.build(runtime, model=model, viewer=False)
        await _chat_loop(agent, prompt)


__all__ = ["resolve_model_file", "run_pack"]
