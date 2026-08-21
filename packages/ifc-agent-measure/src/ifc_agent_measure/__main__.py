"""Run the measurement agent on a project folder or against a running console.

Standalone (opens the project directly):

    ifc-measure path/to/project --model MODEL_ID
    ifc-measure tower.ifc --model MODEL_ID --provider anthropic

Attach (the user's running IFC Console keeps mode and approvals):

    ifc-measure --attach http://127.0.0.1:8383/mcp --token TOKEN --model MODEL_ID

The provider API key comes from the environment (OPENAI_API_KEY,
ANTHROPIC_API_KEY, or OPENROUTER_API_KEY); use --provider local with
--base-url for an OpenAI-compatible local server.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from pathlib import Path

from ifc_agent_measure.agent import MeasurementReport, build_agent, report_to_csv
from ifc_console import ConsoleRuntime, LocalRuntime, ProviderModel


def resolve_model_file(path: Path) -> tuple[Path, Path]:
    """(project_dir, model file) from a folder or a direct .ifc path."""
    if path.is_file():
        return path.parent, path
    if not path.is_dir():
        raise SystemExit(f"{path} is neither an IFC file nor a project folder")
    candidates = sorted(path.glob("*.ifc")) or sorted(path.rglob("*.ifc"))
    if not candidates:
        raise SystemExit(f"{path} holds no .ifc file")
    if len(candidates) > 1:
        names = ", ".join(str(c.relative_to(path)) for c in candidates[:8])
        raise SystemExit(f"{path} holds several models ({names}); pass the file to open")
    return path, candidates[0]


async def chat(agent, *, one_shot: str | None, report_path: Path | None) -> None:
    async def ask(prompt: str) -> None:
        if report_path is not None:
            result = await agent.run(prompt, response_model=MeasurementReport)
            print(result.text)
            written = report_to_csv(result.data, report_path)
            print(f"report written to {written}")
            return
        async for event in agent.stream(prompt, thread_id="measure"):
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
    print("Describe what to measure. Type 'quit' to exit.")
    while True:
        prompt = await asyncio.to_thread(input, "> ")
        if prompt.strip().casefold() in {"quit", "exit"}:
            return
        if prompt.strip():
            await ask(prompt.strip())


async def run(args: argparse.Namespace) -> None:
    model = ProviderModel(
        provider=args.provider,
        model=args.model,
        base_url=args.base_url,
        local_only=args.provider == "local",
    )

    if args.attach:
        runtime = await ConsoleRuntime.connect_http(args.attach, token=args.token)
        async with runtime:
            agent = await build_agent(runtime, model=model, viewer=args.viewer)
            await chat(agent, one_shot=args.prompt, report_path=args.report)
        return

    if args.path is None:
        raise SystemExit("pass a project folder or .ifc file, or use --attach")
    project_dir, model_file = resolve_model_file(args.path)
    runtime = await LocalRuntime.open(
        home=args.home,
        project_dir=project_dir,
        settings={"server.port": args.port} if args.viewer else None,
    )
    async with runtime:
        await runtime.open_model(model_file)
        runtime.set_mode("ask")

        server = None
        server_task = None
        if args.viewer:
            import uvicorn

            runtime.enable_viewer()
            surface = runtime.build_web_app(viewer=True)
            server = uvicorn.Server(
                uvicorn.Config(
                    surface.app,
                    host="127.0.0.1",
                    port=args.port,
                    access_log=False,
                    log_config=None,
                    lifespan="on",
                )
            )
            server_task = asyncio.create_task(server.serve())
            print(f"Viewer: {surface.viewer_url}")

        proposals: list[str] = []
        proposal_source = None
        if args.propose:
            from ifc_agent_measure.agent import build_proposal_source

            proposal_source = build_proposal_source(runtime, proposals)

        try:
            agent = await build_agent(
                runtime,
                model=model,
                viewer=args.viewer,
                proposal_source=proposal_source,
            )
            await chat(agent, one_shot=args.prompt, report_path=args.report)
            for change_set_id in proposals:
                print(
                    f"ChangeSet {change_set_id} awaits host review; approve and "
                    "commit it in ifc-console or with the Workbench API."
                )
        finally:
            if server is not None and server_task is not None:
                server.should_exit = True
                with contextlib.suppress(asyncio.CancelledError):
                    await server_task


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, nargs="?", help="Project folder or .ifc file")
    parser.add_argument("--model", required=True, help="Model id at the chosen provider")
    parser.add_argument("--provider", default="openai", help="openai, anthropic, openrouter, local")
    parser.add_argument("--base-url", default=None, help="OpenAI-compatible server URL")
    parser.add_argument("--attach", default=None, help="MCP URL of a running console")
    parser.add_argument("--token", default=None, help="Session token for --attach")
    parser.add_argument("--viewer", action="store_true", help="Serve the 3D viewer")
    parser.add_argument("--prompt", default=None, help="Run once instead of a chat loop")
    parser.add_argument(
        "--report", type=Path, default=None, help="With --prompt: write a CSV report here"
    )
    parser.add_argument(
        "--propose", action="store_true", help="Allow ChangeSet previews of measured values"
    )
    parser.add_argument("--port", type=int, default=8768)
    parser.add_argument("--home", type=Path, default=None, help="ifc-console home directory")
    args = parser.parse_args()
    asyncio.run(run(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
