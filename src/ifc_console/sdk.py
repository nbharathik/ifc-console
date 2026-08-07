"""The Python SDK: use ifc-console from a script or an agent.

No server, no terminal, no port. `Workbench` opens a model, runs the same
tools the MCP layer serves, and returns plain Python data.

    from ifc_console import Workbench

    with Workbench.open("tower.ifc") as wb:
        walls = wb.query("IfcWall, Pset_WallCommon.FireRating=F30")
        report = wb.validate()

Agent bindings are model agnostic: `wb.tools()` hands out JSON Schema tool
definitions any provider can consume, and `wb.call(name, **kwargs)` runs one.
Nothing here talks to an LLM API or depends on a particular vendor.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Coroutine
from pathlib import Path
from types import TracebackType
from typing import Any

__all__ = ["AsyncWorkbench", "IfcConsoleError", "Workbench"]

_DEFAULT_TIMEOUT = 600.0


class IfcConsoleError(RuntimeError):
    """A tool refused the call. Carries the same code and hint the LLM sees."""

    def __init__(self, code: str, message: str, hint: str = "") -> None:
        super().__init__(f"{code}: {message}" + (f" ({hint})" if hint else ""))
        self.code = code
        self.message = message
        self.hint = hint


def _apply_settings(store: Any, overrides: dict[str, Any]) -> None:
    """In-memory settings for this workbench only; the user's file is untouched."""
    for dotted, value in overrides.items():
        target = store.settings
        *parents, leaf = dotted.split(".")
        for part in (*parents, leaf):
            if not hasattr(target, part):
                raise IfcConsoleError(
                    "INVALID_INPUT", f"unknown setting {dotted!r}", "See docs/settings.md."
                )
            if part != leaf:
                target = getattr(target, part)
        setattr(target, leaf, value)


def _unwrap(envelope: Any) -> dict[str, Any]:
    payload = envelope.model_dump() if hasattr(envelope, "model_dump") else dict(envelope)
    if payload.get("ok"):
        return payload.get("data") or {}
    error = payload.get("error") or {}
    raise IfcConsoleError(
        error.get("code", "INTERNAL_ERROR"),
        error.get("message", "the tool failed"),
        error.get("hint", ""),
    )


class AsyncWorkbench:
    """The async face. Every method is a coroutine; see Workbench for sync use."""

    def __init__(self, core: Any) -> None:
        self._core = core
        self._mcp: Any = None

    # -- construction ---------------------------------------------------------
    @classmethod
    async def create(
        cls,
        path: str | Path | None = None,
        *,
        mode: str = "ask",
        home: str | Path | None = None,
        allowed_dirs: tuple[str | Path, ...] = (),
        settings: dict[str, Any] | None = None,
    ) -> AsyncWorkbench:
        from ifc_console.app import AppCore
        from ifc_console.mcp.server import build_mcp
        from ifc_console.policy.modes import Mode
        from ifc_console.settings import SettingsStore

        store = SettingsStore(home=Path(home) if home else None)
        _apply_settings(store, settings or {})
        core = AppCore(store, mode=Mode(mode), transport="sdk")
        core.start_audit()
        for directory in allowed_dirs:
            core.add_allowed_dir(Path(directory))
        workbench = cls(core)
        workbench._mcp = build_mcp(core)
        if path is not None:
            await workbench.open_model(path)
        return workbench

    # -- session --------------------------------------------------------------
    @property
    def core(self) -> Any:
        """The AppCore underneath, for anything the SDK does not wrap yet."""
        return self._core

    @property
    def mode(self) -> str:
        return self._core.policy.mode.value

    def set_mode(self, mode: str) -> str:
        """Change what this process may do to the model.

        In the console the user owns this switch and the LLM cannot touch it.
        In a script you are the user, so it is yours; an agent loop should keep
        it in the caller's hands, not hand it to the model as a tool.
        """
        from ifc_console.policy.modes import Mode

        self._core.set_mode(Mode(mode), by="sdk")
        return self.mode

    async def open_model(self, path: str | Path, **kwargs: Any) -> dict[str, Any]:
        target = Path(path).expanduser().resolve()
        self._core.add_allowed_dir(target.parent)
        await self._core.open_model(target, **kwargs)
        return self.model

    @property
    def model(self) -> dict[str, Any]:
        session = self._core.session
        return {
            "loaded": session.loaded,
            "name": session.name,
            "path": str(session.path) if session.path else None,
            "schema": session.schema,
            "dirty": session.dirty,
            "fingerprint": session.fingerprint,
            "model_id": getattr(session, "model_id", None),
        }

    def close(self) -> None:
        self._core.shutdown()

    # -- the tool surface -----------------------------------------------------
    async def tools(self) -> list[dict[str, Any]]:
        """Every tool as a provider-neutral JSON Schema definition."""
        listing = await self._mcp.list_tools()
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.inputSchema,
            }
            for tool in sorted(listing, key=lambda t: t.name)
        ]

    async def call(self, name: str, **kwargs: Any) -> dict[str, Any]:
        """Run one tool and return its envelope, errors included.

        This is what an agent loop should use: the envelope's error code and
        hint are written for a model to read and retry from.
        """
        fn = self._core.tool_functions.get(name)
        if fn is None:
            known = ", ".join(sorted(self._core.tool_functions))
            raise IfcConsoleError("NOT_FOUND", f"no tool named {name!r}", f"Tools: {known}")
        envelope = await fn(**kwargs)
        return envelope.model_dump() if hasattr(envelope, "model_dump") else dict(envelope)

    async def _data(self, name: str, **kwargs: Any) -> dict[str, Any]:
        return _unwrap(await self.call(name, **kwargs))

    # -- reads ----------------------------------------------------------------
    async def info(self) -> dict[str, Any]:
        return await self._data("get_ifc_project_info")

    async def status(self) -> dict[str, Any]:
        return await self._data("get_session_status")

    async def orient(self) -> dict[str, Any]:
        return await self._data("orient")

    async def tree(self, root: str | None = None, depth: int = 10) -> dict[str, Any]:
        return await self._data("get_spatial_structure", root_global_id=root, depth=depth)

    async def query(self, selector: str, **kwargs: Any) -> list[dict[str, Any]]:
        data = await self._data("query_elements", query=selector, **kwargs)
        return data.get("rows", [])

    async def element(self, global_ids: str | list[str], **kwargs: Any) -> list[dict[str, Any]]:
        ids = [global_ids] if isinstance(global_ids, str) else list(global_ids)
        data = await self._data("get_element", global_ids=ids, **kwargs)
        return data.get("elements", [])

    async def psets(self, global_ids: str | list[str], **kwargs: Any) -> dict[str, Any]:
        ids = [global_ids] if isinstance(global_ids, str) else list(global_ids)
        return await self._data("get_psets", global_ids=ids, **kwargs)

    async def quantities(self, selector: str, by: str = "class", **kwargs: Any) -> dict[str, Any]:
        return await self._data(
            "compute_quantities", selector=selector, aggregate_by=by, **kwargs
        )

    async def validate(self, **kwargs: Any) -> dict[str, Any]:
        return await self._data("validate_model", **kwargs)

    async def validate_ids(self, ids_path: str | Path, **kwargs: Any) -> dict[str, Any]:
        return await self._data("validate_ids", ids_path=str(ids_path), **kwargs)

    async def clashes(self, set_a: str, set_b: str | None = None, **kwargs: Any) -> dict[str, Any]:
        return await self._data("detect_clashes", set_a=set_a, set_b=set_b, **kwargs)

    async def georeferencing(self) -> dict[str, Any]:
        return await self._data("get_georeferencing")

    async def schema_docs(self, **kwargs: Any) -> dict[str, Any]:
        return await self._data("get_schema_docs", **kwargs)

    # -- talking to a model ---------------------------------------------------
    async def ask(
        self,
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
        """Run one question through an LLM that can call the ifc-console tools.

        Provider neutral: OpenAI, Anthropic, OpenRouter, or any OpenAI
        compatible server (vLLM, LM Studio, Ollama). The key comes from the
        environment unless you pass one. Returns
        {"text", "tool_calls", "usage", "turns"}; pass on_event to watch the
        stream as it happens.
        """
        from ifc_console.chat.agent import converse

        turns = list(history or []) + [{"role": "user", "text": prompt}]
        parts: list[str] = []
        calls: list[dict[str, Any]] = []
        usage: dict[str, Any] = {}
        error: str | None = None
        async for event in converse(
            self._core,
            turns=turns,
            provider_id=provider,
            model=model,
            base_url=base_url,
            api_key=api_key,
            system=system,
            use_tools=tools,
            options=options,
        ):
            if on_event is not None:
                on_event(event)
            kind = event.get("type")
            if kind == "content":
                parts.append(event["text"])
            elif kind == "tool_result":
                calls.append({"name": event["name"], "ok": event["ok"], "summary": event["summary"]})
            elif kind == "usage":
                usage = {"in": event.get("in"), "out": event.get("out")}
            elif kind == "error":
                error = event["text"]
        text = "".join(parts).strip()
        if error and not text:
            raise IfcConsoleError("CHAT_FAILED", error, "Check the provider, model, and key.")
        turns.append({"role": "assistant", "text": text})
        return {"text": text, "tool_calls": calls, "usage": usage, "turns": turns, "error": error}

    # -- knowledge ------------------------------------------------------------
    def build_knowledge(self, *, force: bool = False) -> dict[str, Any]:
        """Build the offline reference index (seconds, no network)."""
        return self._core.knowledge.build(force=force)

    async def search_knowledge(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        data = await self._data("search_ifc_knowledge", query=query, **kwargs)
        return data.get("hits", [])

    async def api_docs(self, function: str) -> dict[str, Any]:
        return await self._data("get_api_docs", function=function)

    # -- writes ---------------------------------------------------------------
    async def run_code(self, code: str, description: str = "") -> dict[str, Any]:
        return await self._data("execute_ifc_code", code=code, description=description)

    async def export_csv(self, selector: str, path: str | Path, **kwargs: Any) -> dict[str, Any]:
        return await self._data("export_csv", selector=selector, path=str(path), **kwargs)

    async def save(self, path: str | Path | None = None, **kwargs: Any) -> dict[str, Any]:
        return await self._data(
            "save_ifc_file", output_path=str(path) if path else None, **kwargs
        )

    # -- more than one model --------------------------------------------------
    async def attach(self, path: str | Path, **kwargs: Any) -> dict[str, Any]:
        target = Path(path).expanduser().resolve()
        self._core.add_allowed_dir(target.parent)
        return await self._data("attach", path=str(target), **kwargs)

    async def detach(self, ref: str) -> dict[str, Any]:
        return await self._data("detach", id=ref)

    async def models(self) -> dict[str, Any]:
        return await self._data("list_models")

    async def use(self, model_id: str) -> dict[str, Any]:
        return await self._data("set_active_model", model_id=model_id)

    async def find_files(self, query: str | None = None, **kwargs: Any) -> dict[str, Any]:
        return await self._data("find_files", query=query, **kwargs)


class _Loop:
    """A private event loop on its own thread, so the SDK stays synchronous."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.closed = False
        self._thread = threading.Thread(
            target=self._run, name="ifc-console-sdk", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def run(self, coro: Coroutine, timeout: float | None = _DEFAULT_TIMEOUT) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=10)
        self.loop.close()


class Workbench:
    """The synchronous face. One model open, the same tools the LLM gets."""

    def __init__(self, workbench: AsyncWorkbench, loop: _Loop) -> None:
        self._wb = workbench
        self._loop = loop

    @classmethod
    def open(
        cls,
        path: str | Path | None = None,
        *,
        mode: str = "ask",
        home: str | Path | None = None,
        allowed_dirs: tuple[str | Path, ...] = (),
        settings: dict[str, Any] | None = None,
    ) -> Workbench:
        """Open a model (or none) and return a ready workbench."""
        loop = _Loop()
        try:
            workbench = loop.run(
                AsyncWorkbench.create(
                    path,
                    mode=mode,
                    home=home,
                    allowed_dirs=allowed_dirs,
                    settings=settings,
                )
            )
        except BaseException:
            loop.close()
            raise
        return cls(workbench, loop)

    def __enter__(self) -> Workbench:
        return self

    def __exit__(self, exc_type: type | None, exc: BaseException | None, tb: TracebackType | None) -> None:
        self.close()

    def close(self) -> None:
        try:
            self._wb.close()
        finally:
            self._loop.close()

    def _run(self, factory: Callable[[], Coroutine], timeout: float | None = None) -> Any:
        if self._loop.closed:
            raise RuntimeError("this Workbench is closed; open a new one")
        return self._loop.run(factory(), timeout or _DEFAULT_TIMEOUT)

    # -- session --------------------------------------------------------------
    @property
    def core(self) -> Any:
        return self._wb.core

    @property
    def mode(self) -> str:
        return self._wb.mode

    def set_mode(self, mode: str) -> str:
        return self._wb.set_mode(mode)

    @property
    def model(self) -> dict[str, Any]:
        return self._wb.model

    def open_model(self, path: str | Path, **kwargs: Any) -> dict[str, Any]:
        return self._run(lambda: self._wb.open_model(path, **kwargs))

    # -- tools ----------------------------------------------------------------
    def tools(self) -> list[dict[str, Any]]:
        return self._run(lambda: self._wb.tools())

    def call(self, name: str, **kwargs: Any) -> dict[str, Any]:
        return self._run(lambda: self._wb.call(name, **kwargs))

    # -- reads ----------------------------------------------------------------
    def info(self) -> dict[str, Any]:
        return self._run(lambda: self._wb.info())

    def status(self) -> dict[str, Any]:
        return self._run(lambda: self._wb.status())

    def orient(self) -> dict[str, Any]:
        return self._run(lambda: self._wb.orient())

    def tree(self, root: str | None = None, depth: int = 10) -> dict[str, Any]:
        return self._run(lambda: self._wb.tree(root, depth))

    def query(self, selector: str, **kwargs: Any) -> list[dict[str, Any]]:
        return self._run(lambda: self._wb.query(selector, **kwargs))

    def element(self, global_ids: str | list[str], **kwargs: Any) -> list[dict[str, Any]]:
        return self._run(lambda: self._wb.element(global_ids, **kwargs))

    def psets(self, global_ids: str | list[str], **kwargs: Any) -> dict[str, Any]:
        return self._run(lambda: self._wb.psets(global_ids, **kwargs))

    def quantities(self, selector: str, by: str = "class", **kwargs: Any) -> dict[str, Any]:
        return self._run(lambda: self._wb.quantities(selector, by, **kwargs))

    def validate(self, **kwargs: Any) -> dict[str, Any]:
        return self._run(lambda: self._wb.validate(**kwargs))

    def validate_ids(self, ids_path: str | Path, **kwargs: Any) -> dict[str, Any]:
        return self._run(lambda: self._wb.validate_ids(ids_path, **kwargs))

    def clashes(self, set_a: str, set_b: str | None = None, **kwargs: Any) -> dict[str, Any]:
        return self._run(lambda: self._wb.clashes(set_a, set_b, **kwargs))

    def georeferencing(self) -> dict[str, Any]:
        return self._run(lambda: self._wb.georeferencing())

    def schema_docs(self, **kwargs: Any) -> dict[str, Any]:
        return self._run(lambda: self._wb.schema_docs(**kwargs))

    # -- talking to a model ---------------------------------------------------
    def ask(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        """One question to an LLM that may call the ifc-console tools."""
        return self._run(lambda: self._wb.ask(prompt, **kwargs), timeout=1800.0)

    # -- knowledge ------------------------------------------------------------
    def build_knowledge(self, *, force: bool = False) -> dict[str, Any]:
        return self._wb.build_knowledge(force=force)

    def search_knowledge(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        return self._run(lambda: self._wb.search_knowledge(query, **kwargs))

    def api_docs(self, function: str) -> dict[str, Any]:
        return self._run(lambda: self._wb.api_docs(function))

    # -- writes ---------------------------------------------------------------
    def run_code(self, code: str, description: str = "") -> dict[str, Any]:
        return self._run(lambda: self._wb.run_code(code, description))

    def export_csv(self, selector: str, path: str | Path, **kwargs: Any) -> dict[str, Any]:
        return self._run(lambda: self._wb.export_csv(selector, path, **kwargs))

    def save(self, path: str | Path | None = None, **kwargs: Any) -> dict[str, Any]:
        return self._run(lambda: self._wb.save(path, **kwargs))

    # -- more than one model --------------------------------------------------
    def attach(self, path: str | Path, **kwargs: Any) -> dict[str, Any]:
        return self._run(lambda: self._wb.attach(path, **kwargs))

    def detach(self, ref: str) -> dict[str, Any]:
        return self._run(lambda: self._wb.detach(ref))

    def models(self) -> dict[str, Any]:
        return self._run(lambda: self._wb.models())

    def use(self, model_id: str) -> dict[str, Any]:
        return self._run(lambda: self._wb.use(model_id))

    def find_files(self, query: str | None = None, **kwargs: Any) -> dict[str, Any]:
        return self._run(lambda: self._wb.find_files(query, **kwargs))
