"""Work on the model overlaps; lifecycle changes wait for it and never block on a parse."""

from __future__ import annotations

import asyncio
import shutil
import threading
from pathlib import Path

import pytest

from ifc_console.app import AppCore
from ifc_console.policy.modes import Mode
from ifc_console.sandbox.runner import secure_isolation_supported
from ifc_console.session.model import ModelSession

pytestmark = pytest.mark.asyncio


def _hold_opens_of(monkeypatch, target: Path) -> tuple[asyncio.Event, list[Path]]:
    """Make opening `target` wait for the returned event; record every open."""
    release = asyncio.Event()
    seen: list[Path] = []
    original = ModelSession.open

    async def slow_open(self, path, **kwargs):
        seen.append(Path(path))
        if Path(path) == target:
            await release.wait()
        return await original(self, path, **kwargs)

    monkeypatch.setattr(ModelSession, "open", slow_open)
    return release, seen


@pytest.mark.skipif(not secure_isolation_supported(), reason="needs the sandbox")
async def test_queries_answer_while_a_sandboxed_run_is_in_flight(ask_harness) -> None:
    long_run = asyncio.create_task(
        ask_harness.call("execute_ifc_code", code="import time\ntime.sleep(2)\n1")
    )
    await asyncio.sleep(0.3)

    listing = await asyncio.wait_for(
        ask_harness.call("query_elements", query="IfcWall", limit=1), timeout=1.5
    )

    assert listing["ok"] is True
    assert not long_run.done()
    assert (await long_run)["ok"] is True


async def test_a_cached_read_answers_while_an_edit_runs(
    harness_factory, work_model, monkeypatch
) -> None:
    from ifc_console.session import executor

    h = await harness_factory(model=work_model, mode=Mode.EDIT)
    await h.call("orient")  # fills the cache at the current revision
    running, release = threading.Event(), threading.Event()
    original = executor.run

    def slow_run(*args, **kwargs):
        running.set()
        release.wait(20)
        return original(*args, **kwargs)

    monkeypatch.setattr(executor, "run", slow_run)
    edit = asyncio.create_task(
        h.call(
            "execute_ifc_code",
            code="ifc_api.run('root.create_entity', ifc, ifc_class='IfcWall')",
        )
    )
    await asyncio.to_thread(running.wait, 10)

    try:
        answer = await asyncio.wait_for(h.call("orient"), timeout=2)
    finally:
        release.set()
    await edit

    assert answer["ok"] is True
    assert answer["meta"]["cached"] is True


async def test_an_edit_the_caller_gave_up_on_is_announced_when_it_finishes(
    harness_factory, work_model, monkeypatch
) -> None:
    from ifc_console.session import executor

    h = await harness_factory(model=work_model, mode=Mode.EDIT)
    seen: list[dict] = []
    h.core.events.subscribe(seen.append)
    running, release = threading.Event(), threading.Event()
    original = executor.run

    def slow_run(*args, **kwargs):
        running.set()
        release.wait(20)
        return original(*args, **kwargs)

    monkeypatch.setattr(executor, "run", slow_run)
    call = asyncio.create_task(
        h.core.tool_functions["execute_ifc_code"](
            code="ifc_api.run('root.create_entity', ifc, ifc_class='IfcWall')"
        )
    )
    await asyncio.to_thread(running.wait, 10)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    early = [e for e in seen if e["type"] == "model_mutated"]
    release.set()
    for _ in range(3000):
        if len([e for e in seen if e["type"] == "model_mutated"]) > len(early):
            break
        await asyncio.sleep(0.01)

    late = [e for e in seen if e["type"] == "model_mutated"][len(early) :]
    assert late and late[-1]["change_id"]
    assert h.core.session.change_count == 1


async def test_concurrent_edits_run_one_at_a_time(harness_factory, work_model) -> None:
    h = await harness_factory(model=work_model, mode=Mode.EDIT)
    code = "ifc_api.run('root.create_entity', ifc, ifc_class='IfcWall')"
    before = len(h.core.session.ifc.by_type("IfcWall"))

    results = await asyncio.gather(
        h.call("execute_ifc_code", code=code, description="first"),
        h.call("execute_ifc_code", code=code, description="second"),
    )

    assert all(r["ok"] for r in results)
    assert h.core.session.change_count == 2
    assert len(h.core.session.ifc.by_type("IfcWall")) == before + 2


async def test_the_current_model_keeps_answering_while_another_parses(
    harness_factory, work_model, tmp_path, monkeypatch
) -> None:
    h = await harness_factory(model=work_model, mode=Mode.ASK)
    annex = tmp_path / "annex.ifc"
    shutil.copy2(work_model, annex)
    release, _ = _hold_opens_of(monkeypatch, annex.resolve())

    opening = asyncio.create_task(h.core.open_model(annex, attach=True))
    await asyncio.sleep(0.1)
    answer = await asyncio.wait_for(h.call("orient"), timeout=2)

    assert answer["ok"] is True
    assert not opening.done()
    release.set()
    annex_id = await asyncio.wait_for(opening, timeout=10)
    assert annex_id in h.core.models.sessions


async def test_opening_the_same_file_twice_parses_it_once(
    harness_factory, work_model, tmp_path, monkeypatch
) -> None:
    h = await harness_factory(model=work_model, mode=Mode.ASK)
    annex = tmp_path / "annex.ifc"
    shutil.copy2(work_model, annex)
    release, seen = _hold_opens_of(monkeypatch, annex.resolve())

    first = asyncio.create_task(h.core.open_model(annex, attach=True))
    second = asyncio.create_task(h.core.open_model(annex, attach=True))
    await asyncio.sleep(0.1)
    release.set()
    ids = await asyncio.wait_for(asyncio.gather(first, second), timeout=10)

    assert ids[0] == ids[1]
    assert seen.count(annex.resolve()) == 1


async def test_a_call_waits_for_the_model_that_is_loading(
    harness_factory, work_model, monkeypatch
) -> None:
    h = await harness_factory(model=None, mode=Mode.ASK)
    h.core.add_allowed_dir(work_model.parent)
    release, _ = _hold_opens_of(monkeypatch, work_model.resolve())

    opening = asyncio.create_task(h.core.open_model(work_model))
    await asyncio.sleep(0.1)
    waiting = asyncio.create_task(h.call("orient"))
    await asyncio.sleep(0.2)
    assert not waiting.done()

    release.set()
    await asyncio.wait_for(opening, timeout=10)
    answer = await asyncio.wait_for(waiting, timeout=10)

    assert answer["ok"] is True
    assert answer["data"]["status"]["model"]["loaded"] is True


async def test_a_call_that_waits_too_long_for_a_load_says_busy(
    harness_factory, work_model, monkeypatch
) -> None:
    h = await harness_factory(model=None, mode=Mode.ASK)
    h.core.add_allowed_dir(work_model.parent)
    monkeypatch.setattr(AppCore, "LOAD_WAIT_S", 0.1)
    release, _ = _hold_opens_of(monkeypatch, work_model.resolve())

    opening = asyncio.create_task(h.core.open_model(work_model))
    await asyncio.sleep(0.1)
    answer = await h.call("orient")
    release.set()
    await asyncio.wait_for(opening, timeout=10)

    assert answer["ok"] is False
    assert answer["error"]["code"] == "MODEL_BUSY"


async def test_a_failed_parse_leaves_the_current_model_alone(
    harness_factory, work_model, tmp_path
) -> None:
    h = await harness_factory(model=work_model, mode=Mode.ASK)
    broken = tmp_path / "broken.ifc"
    broken.write_text("not an ifc file")
    h.core.add_allowed_dir(tmp_path)

    with pytest.raises(Exception):  # noqa: B017
        await h.core.open_model(broken)

    assert h.core.session.loaded
    assert h.core.session.path == work_model.resolve()
    assert not h.core._opening
