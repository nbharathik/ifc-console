"""IFC Console extension entry point for agents, chat, and their browser panel."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ifc_console.extensions import BrowserPanel, ExtensionManifest
from starlette.routing import Mount

from ifc_console_agents import __version__
from ifc_console_agents.assets import require_static_dir, static_app
from ifc_console_agents.chat import ChatState
from ifc_console_agents.chat.routes import build_chat_routes
from ifc_console_agents.files import AgentReferenceStore
from ifc_console_agents.packs import AgentPackRegistry
from ifc_console_agents.panel import build_agent_panel_routes
from ifc_console_agents.tools_skills import register as register_skill_operations

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from ifc_console.app import AppCore
    from ifc_console.core.operations import OperationRegistry


@dataclass
class AgentExtensionState:
    """State owned by one attached agents extension."""

    chat: ChatState
    packs: AgentPackRegistry
    files: AgentReferenceStore


class AgentExtension:
    """Attach the optional agent product without coupling it into core imports."""

    manifest = ExtensionManifest(
        name="agents",
        version=__version__,
        description="Agent SDK, provider chat, browser panel, and reusable skills.",
    )

    def attach(self, core: AppCore) -> AgentExtensionState:
        # Fail while the extension manager is isolating discovery errors, not
        # later when Starlette asks the extension to construct its route tree.
        require_static_dir()
        settings = core.settings.chat
        chat = ChatState(
            provider=settings.provider,
            model=settings.model,
            base_url=settings.base_url,
        )
        packs = AgentPackRegistry(core.store.project_dir)
        files = AgentReferenceStore(core.store.project_dir)
        state = AgentExtensionState(chat=chat, packs=packs, files=files)

        # Compatibility attributes keep current CLI/TUI, SDK, and embedders
        # working while the implementation itself lives in this distribution.
        core.chat = chat
        core.agent_packs = packs
        core.agent_files = files
        return state

    def register_operations(
        self,
        core: AppCore,
        registry: OperationRegistry,
        state: AgentExtensionState,
    ) -> None:
        del state
        register_skill_operations(registry, core)

    def http_routes(
        self, core: AppCore, state: AgentExtensionState
    ) -> Sequence[Any]:
        del state
        return [
            *build_chat_routes(core),
            *build_agent_panel_routes(core),
            Mount("/agents/static", app=static_app(), name="agents-static"),
        ]

    def status(
        self, core: AppCore, state: AgentExtensionState
    ) -> Mapping[str, Any]:
        return {
            "enabled": state.chat.enabled,
            "provider": state.chat.provider,
            "model": state.chat.model,
        }

    def browser_panel(
        self, core: AppCore, state: AgentExtensionState
    ) -> BrowserPanel | None:
        del core, state
        return BrowserPanel(
            name="agents",
            label="Agent",
            module_url="/agents/static/chat.js",
            stylesheet_url="/agents/static/chat.css",
            standalone_url="/chat",
        )

    def close(self, core: AppCore, state: AgentExtensionState) -> None:
        panel = getattr(core, "agent_panel", None)
        if panel is not None:
            for tasks in tuple(panel.active_streams.values()):
                for task in tuple(tasks):
                    if not task.done():
                        with suppress(RuntimeError):
                            task.cancel()
            for _owner, pending in tuple(panel.pending_approvals.values()):
                if not pending.done():
                    with suppress(RuntimeError):
                        pending.set_result(False)
            panel.threads.clear()
            panel.active_streams.clear()
            panel.pending_approvals.clear()
        state.chat.keys.clear()
        state.chat.enabled = False
        state.chat.url = None
        core.agent_panel = None
        core.agent_packs = None
        core.agent_files = None


__all__ = ["AgentExtension", "AgentExtensionState"]
