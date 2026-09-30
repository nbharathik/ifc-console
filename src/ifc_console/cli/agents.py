"""The agents command group. Agent code loads on demand, never at CLI import."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ifc_console.cli.common import _agent_module, _new_store


def add_agents_parser(sub: argparse._SubParsersAction) -> None:
    ag = sub.add_parser(
        "agents",
        help="Built-in and project agents used by the Agent workspace.",
    )
    ag_sub = ag.add_subparsers(dest="agents_cmd", required=True)
    ag_list = ag_sub.add_parser("list", help="List built-in and project agents.")
    ag_list.add_argument("--json", action="store_true")
    ag_list.set_defaults(func=_cmd_agents_list)
    ag_blocks = ag_sub.add_parser(
        "blocks",
        help="List the capability blocks every agent is assembled from.",
    )
    ag_blocks.add_argument("--json", action="store_true")
    ag_blocks.set_defaults(func=_cmd_agents_blocks)
    ag_files = ag_sub.add_parser(
        "files",
        help="Add, index, and list agents' project references.",
    )
    ag_files.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Documents, images, or directories to copy into the managed folder.",
    )
    ag_files.add_argument("--collection", default=None, help="Library folder to file them under.")
    ag_files.add_argument("--json", action="store_true")
    ag_files.set_defaults(func=_cmd_agents_files)
    ag_run = ag_sub.add_parser("run", help="Run one agent in the terminal, standalone or attached.")
    ag_run.add_argument("name", help="Agent name, e.g. measurement or docs.")
    ag_run.add_argument("path", type=Path, nargs="?", help="Project folder or .ifc file.")
    ag_run.add_argument("--model", required=True, help="Model id at the chosen provider.")
    ag_run.add_argument("--provider", default="openai", help="openai, anthropic, openrouter, local")
    ag_run.add_argument("--base-url", default=None, help="OpenAI-compatible server URL.")
    ag_run.add_argument("--attach", default=None, help="MCP URL of a running console.")
    ag_run.add_argument("--token", default=None, help="Session token for --attach.")
    ag_run.add_argument("--prompt", default=None, help="Run once instead of a chat loop.")
    ag_run.add_argument("--home", type=Path, default=None, help="ifc-console home directory.")
    ag_run.set_defaults(func=_cmd_agents_run)
    ag_pack = ag_sub.add_parser(
        "pack",
        help="Install skill packs (a zip or folder made from a document) for this user.",
    )
    ag_pack_sub = ag_pack.add_subparsers(dest="pack_cmd", required=True)
    ag_pack_check = ag_pack_sub.add_parser("check", help="Validate a pack and show what it holds.")
    ag_pack_check.add_argument("path", type=Path, help="A pack .zip or its folder.")
    ag_pack_check.add_argument("--json", action="store_true")
    ag_pack_check.set_defaults(func=_cmd_agents_pack, pack_action="check")
    ag_pack_install = ag_pack_sub.add_parser("install", help="Install a pack into ~/.ifc-console.")
    ag_pack_install.add_argument("path", type=Path, help="A pack .zip or its folder.")
    ag_pack_install.add_argument(
        "--agent", default=None, help="Agent name to record with the pack."
    )
    ag_pack_install.add_argument("--json", action="store_true")
    ag_pack_install.set_defaults(func=_cmd_agents_pack, pack_action="install")
    ag_pack_list = ag_pack_sub.add_parser("list", help="List installed packs.")
    ag_pack_list.add_argument("--json", action="store_true")
    ag_pack_list.set_defaults(func=_cmd_agents_pack, pack_action="list")
    ag_pack_remove = ag_pack_sub.add_parser("uninstall", help="Remove an installed pack.")
    ag_pack_remove.add_argument("name", help="Pack name as listed.")
    ag_pack_remove.add_argument("--json", action="store_true")
    ag_pack_remove.set_defaults(func=_cmd_agents_pack, pack_action="uninstall")
    ag_engines = ag_sub.add_parser(
        "engines",
        help="External agent engines (OpenCode, Codex, ...) the Agent workspace may run.",
    )
    ag_engines_sub = ag_engines.add_subparsers(dest="engines_cmd", required=True)
    ag_engines_list = ag_engines_sub.add_parser("list", help="Known and enabled engines.")
    ag_engines_list.add_argument("--json", action="store_true")
    ag_engines_list.set_defaults(func=_cmd_agents_engines, engines_action="list")
    ag_engines_enable = ag_engines_sub.add_parser(
        "enable", help="Add an engine to harness.engines and turn engines on."
    )
    ag_engines_enable.add_argument(
        "name", help="A known engine (opencode, codex, ...) or a new name."
    )
    ag_engines_enable.add_argument(
        "--command", default=None, help="Executable for a custom engine."
    )
    ag_engines_enable.add_argument(
        "--arg", action="append", default=None, help="Argument for a custom engine (repeatable)."
    )
    ag_engines_enable.add_argument("--mode", default=None, help="ACP session mode to select.")
    ag_engines_enable.add_argument(
        "--local", action="store_true", help="The engine only talks to a local model."
    )
    ag_engines_enable.add_argument("--json", action="store_true")
    ag_engines_enable.set_defaults(func=_cmd_agents_engines, engines_action="enable")
    ag_engines_disable = ag_engines_sub.add_parser(
        "disable", help="Remove an engine from harness.engines."
    )
    ag_engines_disable.add_argument("name")
    ag_engines_disable.add_argument("--json", action="store_true")
    ag_engines_disable.set_defaults(func=_cmd_agents_engines, engines_action="disable")


def _agents_registry():
    packs = _agent_module("packs")
    store = _new_store()
    from ifc_console.agents.paths import blueprints_dir

    return packs.AgentPackRegistry(store.project_dir, blueprints_dir=blueprints_dir(store.home))


def _cmd_agents_list(args: argparse.Namespace) -> int:
    registry = _agents_registry()
    installed = registry.installed()
    if args.json:
        print(
            json.dumps(
                {
                    "agents": [
                        {
                            **info.model_dump(mode="json"),
                            "builtin": registry.is_builtin(info.name),
                        }
                        for info in installed
                    ],
                },
                indent=2,
            )
        )
        return 0
    if installed:
        for info in installed:
            print(f"{info.name:20} [{info.kind}] {info.title}: {info.description}")
    else:
        print("this build ships no agents")
    return 0


def _cmd_agents_blocks(args: argparse.Namespace) -> int:
    """The blocks a built-in or custom agent can be built from."""
    blocks = _agent_module("blocks")
    available_blocks = blocks.BLOCKS

    if args.json:
        print(
            json.dumps(
                [block.info().model_dump(mode="json") for block in available_blocks],
                indent=2,
            )
        )
        return 0
    for block in available_blocks:
        marks = []
        if block.viewer_only:
            marks.append("viewer only")
        if block.advanced:
            marks.append("advanced")
        if block.proposals:
            marks.append("writes previews")
        suffix = f" ({', '.join(marks)})" if marks else ""
        print(f"{block.name:20} {block.title}{suffix}")
        print(f"{'':20} {block.description}")
        if block.tools:
            print(f"{'':20} tools: {', '.join(block.tools)}")
    return 0


def _cmd_agents_files(args: argparse.Namespace) -> int:
    agent_files = _agent_module("files")
    from ifc_console.knowledge.project import ProjectKnowledge
    from ifc_console.mcp.envelope import ToolError

    store = _new_store()
    # the library under the console home serves every project
    references = agent_files.AgentReferenceStore.for_library(store.home)
    knowledge = ProjectKnowledge.for_library(store.home)
    try:
        collection = getattr(args, "collection", None)
        added = references.add_paths(args.paths, collection=collection) if args.paths else []
        summary = references.sync(knowledge)
    except ToolError as exc:
        print(f"{exc.code}: {exc}")
        if exc.hint:
            print(exc.hint)
        return 1
    finally:
        knowledge.close()
    payload = {**summary, "added": [str(path) for path in added]}
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0
    print(f"reference folder: {summary['directory']}")
    for row in summary["files"]:
        state = "indexed" if row["indexed"] else "not indexed"
        print(f"{row['name']:28} [{row['media']}, {state}]")
    if not summary["files"]:
        print("no references; copy files into the folder above or pass paths to this command")
    return 0


def _pack_bytes(path: Path) -> tuple[bytes, str]:
    """A pack zip as bytes: read a .zip, or zip a pack folder in memory."""
    import io
    import zipfile

    path = path.expanduser()
    if path.is_file():
        return path.read_bytes(), path.name
    if not path.is_dir():
        raise FileNotFoundError(path)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(p for p in path.rglob("*") if p.is_file()):
            archive.write(file, f"{path.name}/{file.relative_to(path).as_posix()}")
    return buffer.getvalue(), f"{path.name}.zip"


def _print_pack_preview(preview: dict) -> None:
    print(f"pack     {preview['name']} v{preview['version']}: {preview['title']}")
    for skill in preview["skills"]:
        print(f"skill    #{skill['name']}: {skill['description']}")
    print(
        f"files    {len(preview['knowledge'])} knowledge, {len(preview['documents'])} documents, "
        f"{len(preview['tables'])} tables ({sum(t['rows'] for t in preview['tables'])} rows)"
    )
    for warning in preview["warnings"]:
        print(f"note     {warning}")


def _cmd_agents_pack(args: argparse.Namespace) -> int:
    packs_module = _agent_module("skill_packs")
    skills_module = _agent_module("skills")
    from ifc_console.knowledge.project import ProjectKnowledge
    from ifc_console.mcp.envelope import ToolError

    store = _new_store()
    packs = packs_module.SkillPackStore(store.home)
    action = args.pack_action
    if action == "list":
        rows = packs.installed()
        if args.json:
            print(json.dumps(rows, indent=2))
            return 0
        for row in rows:
            skills = ", ".join(f"#{name}" for name in row.get("skills") or []) or "no skills"
            print(f"{row['name']:40} v{row.get('version')}  {skills}")
        if not rows:
            print(f"no packs installed under {packs.directory}")
        return 0
    from ifc_console.agents.paths import project_state_dir

    skills = skills_module.AgentSkillStore(
        store.project_dir,
        user_dir=store.home,
        project_skills_dir=project_state_dir(store.home, store.project_dir) / "skills",
    )
    knowledge = ProjectKnowledge.for_library(store.home)
    try:
        if action == "uninstall":
            removed = packs.uninstall(args.name, skills=skills, knowledge=knowledge)
            if args.json:
                print(json.dumps(removed, indent=2))
            else:
                print(
                    f"removed {args.name}: {len(removed['skills'])} skills, {len(removed['documents'])} documents"
                )
            return 0
        try:
            data, filename = _pack_bytes(args.path)
        except FileNotFoundError:
            print(f"no such pack: {args.path}")
            return 1
        preview = packs.stage(data, filename)
        if action == "check":
            packs.discard(preview["staging_id"])
            if args.json:
                print(json.dumps(preview, indent=2, default=str))
            else:
                _print_pack_preview(preview)
            return 0
        record = packs.commit(
            preview["staging_id"], skills=skills, knowledge=knowledge, agent=args.agent
        )
    except ToolError as exc:
        print(f"{exc.code}: {exc}")
        if exc.hint:
            print(exc.hint)
        return 1
    finally:
        knowledge.close()
    if args.json:
        print(json.dumps(record, indent=2))
        return 0
    _print_pack_preview(preview)
    print(f"installed into {record['directory']}")
    for name in record["skills"]:
        print(f"type #{name} in the agent panel to use it")
    return 0


def _cmd_agents_engines(args: argparse.Namespace) -> int:
    """List, enable, or disable the external engines the panel may spawn."""
    engine_module = _agent_module("harness.engine")
    store = _new_store()
    action = args.engines_action
    if action == "list":
        registry = engine_module.EngineRegistry(store.settings)
        rows = []
        for name, template in engine_module.TEMPLATES.items():
            spec = registry.get(name)
            rows.append(
                {
                    "name": name,
                    "label": template["label"],
                    "command": spec.command if spec else template["command"],
                    "enabled": spec is not None,
                    "installed": (
                        spec
                        or engine_module.EngineSpec(
                            name=name, label="", command=template["command"]
                        )
                    ).available(),
                }
            )
        for name, spec in registry.engines.items():
            if name not in engine_module.TEMPLATES:
                rows.append(
                    {
                        "name": name,
                        "label": spec.label,
                        "command": spec.command,
                        "enabled": True,
                        "installed": spec.available(),
                    }
                )
        if args.json:
            print(json.dumps({"enabled": registry.enabled, "engines": rows}, indent=2))
            return 0
        print(f"engines: {'on' if registry.enabled else 'off'} (harness.enabled)")
        for row in rows:
            state = "enabled" if row["enabled"] else "known"
            found = "installed" if row["installed"] else f"{row['command']} not on PATH"
            print(f"{row['name']:12} {row['label']:14} {state:8} {found}")
        return 0

    rows = [row.model_dump(mode="json") for row in store.settings.harness.engines]
    name = args.name.strip().lower()
    if action == "disable":
        kept = [row for row in rows if row["name"] != name]
        if len(kept) == len(rows):
            print(f"engine {name!r} is not enabled")
            return 1
        store.set_user("harness.engines", json.dumps(kept))
        print(f"removed {name} from harness.engines")
        return 0

    if args.command:
        from ifc_console.settings import HarnessEngineSettings

        row = HarnessEngineSettings(name=name, command=args.command, args=list(args.arg or []))
    else:
        try:
            row = engine_module.template_settings(name)
        except KeyError:
            print(f"unknown engine {name!r}; pass --command for a custom one")
            print("known engines: " + ", ".join(engine_module.TEMPLATES))
            return 1
    if args.mode:
        row = row.model_copy(update={"mode": args.mode})
    if args.local:
        row = row.model_copy(update={"local": True})
    rows = [existing for existing in rows if existing["name"] != name]
    rows.append(row.model_dump(mode="json"))
    store.set_user("harness.engines", json.dumps(rows))
    store.set_user("harness.enabled", "true")
    spec = engine_module.EngineSpec.from_settings(row)
    if args.json:
        print(
            json.dumps(
                {
                    "enabled": True,
                    "engine": row.model_dump(mode="json"),
                    "installed": spec.available(),
                }
            )
        )
        return 0
    print(f"enabled {name}: {row.command} {' '.join(row.args)}".rstrip())
    if not spec.available():
        print(f"note: {row.command} is not on PATH yet; install it before picking the engine")
    print("pick it in the Agent workspace under Provider")
    return 0


def _cmd_agents_run(args: argparse.Namespace) -> int:
    import asyncio

    runner = _agent_module("runner")

    registry = _agents_registry()
    pack = registry.get(args.name.lower())
    if pack is None:
        active = ", ".join(info.name for info in registry.active()) or "(none)"
        print(f"no agent named {args.name!r}; available: {active}")
        print("`ifc-console agents list` shows built-in and project agents")
        return 1
    asyncio.run(
        runner.run_pack(
            pack,
            path=args.path,
            attach=args.attach,
            token=args.token,
            provider=args.provider,
            model_id=args.model,
            base_url=args.base_url,
            prompt=args.prompt,
            # terminal runs stay text-first; the panel is the viewer surface
            viewer=False,
            home=args.home,
        )
    )
    return 0
