"""Live, human-readable catalog for the TUI's ``/tools`` command.

The catalog reads the registries that power the application instead of
maintaining a second hand-written inventory.  This matters for plugins and
viewer tools, which can change the MCP surface at runtime.
"""

from __future__ import annotations

import asyncio
import json
import textwrap
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from rich.markup import escape

if TYPE_CHECKING:
    from ifc_console.app import AppCore


SECTIONS = ("slash", "ai", "prompts", "resources", "settings", "search", "all")
SECTION_HELP = {
    "all": "list every registered item",
    "slash": "commands you can run in this TUI",
    "ai": "functions exposed to connected AI clients",
    "prompts": "guided workflows exposed through MCP clients",
    "resources": "read-only context exposed through MCP clients",
    "settings": "configuration keys and their current values",
    "search": "search every category",
}
SECTION_ALIASES = {
    "command": "slash",
    "commands": "slash",
    "function": "ai",
    "functions": "ai",
    "prompt": "prompts",
    "resource": "resources",
    "setting": "settings",
}


@dataclass(frozen=True)
class CatalogSurfaces:
    tools: tuple[Any, ...]
    prompts: tuple[Any, ...]
    resources: tuple[Any, ...]
    resource_templates: tuple[Any, ...]


@dataclass(frozen=True)
class SearchEntry:
    section: str
    name: str
    description: str


def _plain(value: Any) -> str:
    return " ".join(str(value or "").split())


def _purpose(value: Any, *, width: int = 112) -> str:
    text = _plain(value)
    if text.startswith("[") and "] " in text:
        text = text.partition("] ")[2]
    return textwrap.shorten(text, width=width, placeholder="...") if text else "no description"


def _value(value: Any, *, width: int = 72) -> str:
    rendered = json.dumps(value, default=str)
    return textwrap.shorten(rendered, width=width, placeholder="...")


async def _surfaces(core: AppCore) -> CatalogSurfaces:
    # The normal TUI startup has already attached the MCP server.  Building
    # just its registry here is a safe fallback when /tools is the very first
    # command or when the TUI is embedded without server autostart.  It does
    # not open a port or start a listener.
    mcp = core._mcp
    if mcp is None:
        from ifc_console.mcp.server import build_mcp

        mcp = build_mcp(core)
    prompts, resources, templates = await asyncio.gather(
        mcp.list_prompts(),
        mcp.list_resources(),
        mcp.list_resource_templates(),
    )
    tools = tuple(core.operations.specs())
    surfaces = CatalogSurfaces(
        tools=tools,
        prompts=tuple(sorted(prompts, key=lambda item: item.name)),
        resources=tuple(sorted(resources, key=lambda item: item.name)),
        resource_templates=tuple(sorted(templates, key=lambda item: item.name)),
    )
    # Completion is synchronous. Cache only names that came from the public
    # live listing so it can offer prompt/resource details after any catalog
    # view has been opened.
    core.tool_catalog_names = {
        "prompts": tuple(item.name for item in surfaces.prompts),
        "resources": tuple(
            item.name for item in (*surfaces.resources, *surfaces.resource_templates)
        ),
    }
    return surfaces


def _tool_state(core: AppCore, tool: Any) -> tuple[str, str]:
    decision = core.policy.evaluate(list(tool.required_capabilities))
    if decision.allowed:
        return "[green]available[/green]", "available in the current session"
    missing = ", ".join(item.value for item in decision.missing)
    return "[yellow]blocked[/yellow]", f"{decision.rule}; missing {missing}"


def _schema_type(schema: Mapping[str, Any]) -> str:
    if "type" in schema:
        value = schema["type"]
        if value == "array":
            return f"array[{_schema_type(schema.get('items', {}))}]"
        return str(value)
    variants = [
        _schema_type(item)
        for item in schema.get("anyOf", [])
        if item.get("type") != "null"
    ]
    if variants:
        return " | ".join(variants)
    if "$ref" in schema:
        return str(schema["$ref"]).rsplit("/", 1)[-1]
    return "value"


def _slash_listing(slash_commands: Mapping[str, Any]) -> str:
    lines = ["[b]slash commands[/b]  [dim]run these directly in the TUI[/dim]"]
    for command in sorted(slash_commands.values(), key=lambda item: (item.group, item.name)):
        lines.append(
            f"  [cyan]{escape(command.usage):<30}[/cyan] {_purpose(command.help)}"
        )
    return "\n".join(lines)


def _slash_detail(command: Any) -> str:
    lines = [
        f"[b]slash command[/b]  [cyan]{escape(command.usage)}[/cyan]",
        f"  {escape(_plain(command.help))}",
        f"  [dim]group[/dim] {escape(command.group)}",
    ]
    if command.examples:
        lines.append("  [dim]examples[/dim]")
        lines.extend(f"    [cyan]{escape(example)}[/cyan]" for example in command.examples)
    return "\n".join(lines)


def _ai_listing(core: AppCore, tools: Sequence[Any]) -> str:
    lines = [
        "[b]AI tools[/b]  [dim]live MCP functions; available/blocked reflects this session[/dim]"
    ]
    for tool in tools:
        state, _reason = _tool_state(core, tool)
        lines.append(
            f"  [cyan]{escape(tool.name):<34}[/cyan] {state:<28} "
            f"{escape(_purpose(tool.description, width=92))}"
        )
    if not tools:
        lines.append("  [dim]no AI tools are currently registered[/dim]")
    lines.append("[dim]viewer tools appear here only while the viewer is enabled[/dim]")
    return "\n".join(lines)


def _ai_detail(core: AppCore, tool: Any) -> str:
    state, reason = _tool_state(core, tool)
    lines = [
        f"[b]AI tool[/b]  [cyan]{escape(tool.name)}[/cyan]  {state}",
        f"  {escape(_plain(tool.description))}",
    ]
    properties = tool.input_schema.get("properties", {})
    required = set(tool.input_schema.get("required", []))
    if properties:
        lines.append("  [dim]arguments[/dim]")
        for name, schema in properties.items():
            need = "required" if name in required else "optional"
            description = _purpose(schema.get("description"), width=84)
            lines.append(
                f"    [cyan]{escape(name)}[/cyan] "
                f"[dim]({_schema_type(schema)}, {need})[/dim] {escape(description)}"
            )
    else:
        lines.append("  [dim]arguments[/dim] none")
    capabilities = ", ".join(item.value for item in tool.required_capabilities) or "none"
    lines.extend(
        (
            f"  [dim]capabilities[/dim] {escape(capabilities)}",
            f"  [dim]permission[/dim] {escape(reason)}",
        )
    )
    return "\n".join(lines)


def _prompt_listing(prompts: Sequence[Any]) -> str:
    lines = ["[b]prompts[/b]  [dim]choose these from a connected MCP client[/dim]"]
    for prompt in prompts:
        args = ", ".join(argument.name for argument in (prompt.arguments or []))
        signature = f"({args})" if args else ""
        lines.append(
            f"  [cyan]{escape(prompt.name + signature):<34}[/cyan] "
            f"{escape(_purpose(prompt.description))}"
        )
    return "\n".join(lines)


def _prompt_detail(prompt: Any) -> str:
    lines = [
        f"[b]prompt[/b]  [cyan]{escape(prompt.name)}[/cyan]",
        f"  {escape(_plain(prompt.description))}",
    ]
    arguments = prompt.arguments or []
    if arguments:
        lines.append("  [dim]arguments[/dim]")
        for argument in arguments:
            need = "required" if argument.required else "optional"
            description = _purpose(argument.description, width=88)
            lines.append(
                f"    [cyan]{escape(argument.name)}[/cyan] [dim]({need})[/dim] "
                f"{escape(description)}"
            )
    else:
        lines.append("  [dim]arguments[/dim] none")
    lines.append("  [dim]use it from your MCP client's prompt/workflow menu[/dim]")
    return "\n".join(lines)


def _resource_uri(resource: Any) -> str:
    return str(getattr(resource, "uri", None) or getattr(resource, "uriTemplate", ""))


def _resource_listing(resources: Sequence[Any], templates: Sequence[Any]) -> str:
    lines = ["[b]resources[/b]  [dim]read-only context exposed to MCP clients[/dim]"]
    for resource in (*resources, *templates):
        template = " template" if resource in templates else ""
        lines.append(
            f"  [cyan]{escape(resource.name):<24}[/cyan] "
            f"[dim]{escape(_resource_uri(resource))}{template}[/dim]  "
            f"{escape(_purpose(resource.description, width=82))}"
        )
    return "\n".join(lines)


def _resource_detail(resource: Any, *, template: bool) -> str:
    kind = "resource template" if template else "resource"
    lines = [
        f"[b]{kind}[/b]  [cyan]{escape(resource.name)}[/cyan]",
        f"  {escape(_plain(resource.description))}",
        f"  [dim]URI[/dim] {escape(_resource_uri(resource))}",
    ]
    mime_type = getattr(resource, "mimeType", None)
    if mime_type:
        lines.append(f"  [dim]media type[/dim] {escape(mime_type)}")
    return "\n".join(lines)


def _settings_listing(core: AppCore) -> str:
    flat = core.store.flat()
    lines = ["[b]settings[/b]  [dim]current values and their source[/dim]"]
    for key in sorted(flat):
        source = core.store.provenance.get(key, "default")
        lines.append(
            f"  [cyan]{escape(key):<38}[/cyan] {escape(_value(flat[key]))} "
            f"[dim]({escape(source)})[/dim]"
        )
    lines.append("[dim]/settings <key> <value> changes a setting[/dim]")
    return "\n".join(lines)


def _setting_detail(core: AppCore, key: str) -> str:
    flat = core.store.flat()
    value = flat[key]
    source = core.store.provenance.get(key, "default")
    return "\n".join(
        (
            f"[b]setting[/b]  [cyan]{escape(key)}[/cyan]",
            f"  [dim]current[/dim] {escape(_value(value, width=160))}",
            f"  [dim]source[/dim] {escape(source)}",
            f"  [dim]change with[/dim] /settings {escape(key)} <value>",
        )
    )


def _entries(
    core: AppCore,
    slash_commands: Mapping[str, Any],
    surfaces: CatalogSurfaces,
) -> list[SearchEntry]:
    entries = [
        SearchEntry("slash", command.name, _plain(command.help))
        for command in slash_commands.values()
    ]
    entries.extend(
        SearchEntry("ai", tool.name, _plain(tool.description)) for tool in surfaces.tools
    )
    entries.extend(
        SearchEntry("prompts", prompt.name, _plain(prompt.description))
        for prompt in surfaces.prompts
    )
    entries.extend(
        SearchEntry(
            "resources",
            resource.name,
            f"{_resource_uri(resource)} {_plain(resource.description)}",
        )
        for resource in (*surfaces.resources, *surfaces.resource_templates)
    )
    entries.extend(SearchEntry("settings", key, "setting") for key in core.store.flat())
    return entries


def _search(
    core: AppCore,
    slash_commands: Mapping[str, Any],
    surfaces: CatalogSurfaces,
    query: str,
) -> str:
    needle = query.casefold()
    matches = [
        entry
        for entry in _entries(core, slash_commands, surfaces)
        if needle in entry.name.casefold() or needle in entry.description.casefold()
    ]
    matches.sort(
        key=lambda entry: (
            entry.name.casefold() != needle,
            not entry.name.casefold().startswith(needle),
            entry.section,
            entry.name,
        )
    )
    if not matches:
        return f"[yellow]no catalog entries match {escape(query)!r}[/yellow]"
    lines = [f"[b]catalog search[/b]  [dim]{escape(query)} · {len(matches)} result(s)[/dim]"]
    for entry in matches:
        lines.append(
            f"  [dim]{entry.section:<9}[/dim] [cyan]{escape(entry.name):<34}[/cyan] "
            f"{escape(_purpose(entry.description, width=86))}"
        )
    lines.append("[dim]/tools <name> or /tools <section> <name> explains one[/dim]")
    return "\n".join(lines)


def _find_resource(surfaces: CatalogSurfaces, name: str) -> tuple[Any, bool] | None:
    for resource in surfaces.resources:
        if name in (resource.name.casefold(), _resource_uri(resource).casefold()):
            return resource, False
    for resource in surfaces.resource_templates:
        if name in (resource.name.casefold(), _resource_uri(resource).casefold()):
            return resource, True
    return None


def _detail(
    core: AppCore,
    slash_commands: Mapping[str, Any],
    surfaces: CatalogSurfaces,
    section: str,
    name: str,
) -> str | None:
    wanted = name.lstrip("/").casefold()
    if section == "slash":
        command = next(
            (item for item in slash_commands.values() if item.name.casefold() == wanted), None
        )
        return _slash_detail(command) if command else None
    if section == "ai":
        tool = next((item for item in surfaces.tools if item.name.casefold() == wanted), None)
        return _ai_detail(core, tool) if tool else None
    if section == "prompts":
        prompt = next((item for item in surfaces.prompts if item.name.casefold() == wanted), None)
        return _prompt_detail(prompt) if prompt else None
    if section == "resources":
        found = _find_resource(surfaces, wanted)
        return _resource_detail(found[0], template=found[1]) if found else None
    if section == "settings":
        key = next((item for item in core.store.flat() if item.casefold() == wanted), None)
        return _setting_detail(core, key) if key else None
    return None


def _overview(
    core: AppCore,
    slash_commands: Mapping[str, Any],
    surfaces: CatalogSurfaces,
) -> str:
    available = sum(_tool_state(core, tool)[0].startswith("[green]") for tool in surfaces.tools)
    resource_count = len(surfaces.resources) + len(surfaces.resource_templates)
    save_state = "enabled" if core.policy.allow_ai_save else "off; only you can save"
    lines = [
        "[b]tools & capabilities[/b]  [dim]live catalog[/dim]",
        f"  [cyan]/tools slash[/cyan]      {len(slash_commands)} TUI commands",
        f"  [cyan]/tools ai[/cyan]         {len(surfaces.tools)} AI functions "
        f"({available} available now)",
        f"  [cyan]/tools prompts[/cyan]    {len(surfaces.prompts)} guided workflows",
        f"  [cyan]/tools resources[/cyan]  {resource_count} read-only context sources",
        f"  [cyan]/tools settings[/cyan]   {len(core.store.flat())} configuration keys",
        "  [cyan]/tools search <text>[/cyan]  search names and descriptions",
        "  [cyan]/tools all[/cyan]        print every category",
        "",
        f"[dim]session[/dim] mode={core.policy.mode.value} · AI saving={save_state}",
        "[dim]/tools <name> explains an exact match across any category[/dim]",
    ]
    return "\n".join(lines)


async def render_catalog(
    core: AppCore,
    slash_commands: Mapping[str, Any],
    raw_args: str,
) -> str:
    """Render one /tools view from the live application registries."""
    surfaces = await _surfaces(core)
    first, _separator, remainder = raw_args.strip().partition(" ")
    section = SECTION_ALIASES.get(first.casefold(), first.casefold()) if first else ""
    target = remainder.strip()

    if not section:
        return _overview(core, slash_commands, surfaces)
    if section == "search":
        if not target:
            return "[yellow]usage: /tools search <text>[/yellow]"
        return _search(core, slash_commands, surfaces, target)
    if section == "all":
        if target:
            return "[yellow]usage: /tools all[/yellow]"
        return "\n\n".join(
            (
                _slash_listing(slash_commands),
                _ai_listing(core, surfaces.tools),
                _prompt_listing(surfaces.prompts),
                _resource_listing(surfaces.resources, surfaces.resource_templates),
                _settings_listing(core),
            )
        )

    if section in SECTIONS:
        if target:
            detail = _detail(core, slash_commands, surfaces, section, target)
            if detail is not None:
                return detail
            return (
                f"[yellow]no {escape(section)} entry named {escape(target)!r}[/yellow]; "
                f"run /tools {escape(section)} to list them"
            )
        if section == "slash":
            return _slash_listing(slash_commands)
        if section == "ai":
            return _ai_listing(core, surfaces.tools)
        if section == "prompts":
            return _prompt_listing(surfaces.prompts)
        if section == "resources":
            return _resource_listing(surfaces.resources, surfaces.resource_templates)
        if section == "settings":
            return _settings_listing(core)

    # A bare name is a convenient cross-category detail lookup.
    exact = [
        candidate
        for candidate in ("slash", "ai", "prompts", "resources", "settings")
        if _detail(core, slash_commands, surfaces, candidate, first) is not None
    ]
    if len(exact) == 1:
        return _detail(core, slash_commands, surfaces, exact[0], first) or ""
    if len(exact) > 1:
        choices = ", ".join(f"/tools {candidate} {escape(first)}" for candidate in exact)
        return f"[yellow]{escape(first)!r} exists in several categories[/yellow]: {choices}"
    return _search(core, slash_commands, surfaces, raw_args.strip())
