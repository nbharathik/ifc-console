"""Integration harness: an in-memory MCP client wired to a real AppCore."""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path

import pytest_asyncio
from mcp.shared.memory import create_connected_server_and_client_session

from ifc_console.app import AppCore
from ifc_console.policy.modes import Mode
from ifc_console.settings import SettingsStore


class Harness:
    def __init__(self, core: AppCore, session) -> None:
        self.core = core
        self.session = session

    async def call(self, tool: str, **arguments) -> dict:
        result = await self.session.call_tool(tool, arguments)
        texts = [c.text for c in result.content if getattr(c, "type", None) == "text"]
        payload = json.loads(texts[0]) if texts else {}
        payload["_images"] = sum(
            1 for c in result.content if getattr(c, "type", None) == "image"
        )
        return payload

    async def list_tools(self) -> list[str]:
        result = await self.session.list_tools()
        return [t.name for t in result.tools]

    def set_mode(self, mode: Mode) -> None:
        self.core.set_mode(mode, by="test")


async def _make_core(
    home: Path,
    project_dir: Path,
    model: Path | None,
    mode: Mode,
    *,
    allow_ai_save: bool = False,
) -> AppCore:
    store = SettingsStore(
        home=home,
        project_dir=project_dir,
        env={},
        flag_overrides={"files.allow_ai_save": allow_ai_save},
    )
    core = AppCore(store, mode=mode)
    core.start_audit()
    if model is not None:
        core.add_allowed_dir(model.parent)
        await core.open_model(model)
    return core


@pytest_asyncio.fixture
async def harness_factory(tmp_path: Path):
    created: list[tuple[AppCore, asyncio.Event, asyncio.Task[None]]] = []

    async def serve(mcp, ready: asyncio.Future, stop: asyncio.Event) -> None:
        try:
            async with create_connected_server_and_client_session(
                mcp, raise_exceptions=True
            ) as session:
                await session.initialize()
                ready.set_result(session)
                await stop.wait()
        except BaseException as exc:
            if not ready.done():
                ready.set_exception(exc)
            raise

    async def factory(
        model: Path | None = None,
        mode: Mode = Mode.ASK,
        *,
        allow_ai_save: bool = False,
    ) -> Harness:
        home = tmp_path / "home"
        core = await _make_core(
            home,
            tmp_path,
            model,
            mode,
            allow_ai_save=allow_ai_save,
        )
        from ifc_console.mcp.server import build_mcp

        mcp = build_mcp(core)
        ready = asyncio.get_running_loop().create_future()
        stop = asyncio.Event()
        task = asyncio.create_task(serve(mcp, ready, stop))
        try:
            session = await ready
        except BaseException:
            stop.set()
            with contextlib.suppress(BaseException):
                await task
            await core.ashutdown()
            raise
        created.append((core, stop, task))
        return Harness(core, session)

    yield factory
    for core, stop, task in reversed(created):
        stop.set()
        await task
        await core.ashutdown()


@pytest_asyncio.fixture
async def ask_harness(harness_factory, work_model: Path):
    return await harness_factory(model=work_model, mode=Mode.ASK)
