"""The lifecycle gate: shared work overlaps, lifecycle changes do not."""

from __future__ import annotations

import asyncio

import pytest

from ifc_console.application.gates import GateUpgradeError, LifecycleGate

pytestmark = pytest.mark.asyncio


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def _enter_and_leave(section) -> None:
    """Take a gate section and release it again, failing after two seconds."""

    async def go() -> None:
        async with section:
            pass

    await asyncio.wait_for(go(), 2)


async def test_shared_holders_overlap() -> None:
    gate = LifecycleGate()
    inside = 0
    peak = 0

    async def work() -> None:
        nonlocal inside, peak
        async with gate.shared():
            inside += 1
            peak = max(peak, inside)
            await asyncio.sleep(0.01)
            inside -= 1

    await asyncio.gather(work(), work(), work())

    assert peak == 3
    assert gate.shared_count == 0


async def test_exclusive_waits_for_shared_and_blocks_later_shared() -> None:
    gate = LifecycleGate()
    order: list[str] = []
    release = asyncio.Event()

    async def first_reader() -> None:
        async with gate.shared():
            order.append("read-1 in")
            await release.wait()
            order.append("read-1 out")

    async def writer() -> None:
        async with gate.exclusive():
            order.append("write")

    async def late_reader() -> None:
        async with gate.shared():
            order.append("read-2")

    tasks = [asyncio.create_task(first_reader())]
    await _settle()
    tasks.append(asyncio.create_task(writer()))
    await _settle()
    tasks.append(asyncio.create_task(late_reader()))
    await _settle()
    assert order == ["read-1 in"]
    assert gate.waiting == 2

    release.set()
    await asyncio.gather(*tasks)

    assert order == ["read-1 in", "read-1 out", "write", "read-2"]


async def test_exclusive_holders_do_not_overlap() -> None:
    gate = LifecycleGate()
    inside = 0
    peak = 0

    async def work() -> None:
        nonlocal inside, peak
        async with gate.exclusive():
            inside += 1
            peak = max(peak, inside)
            await asyncio.sleep(0.005)
            inside -= 1

    await asyncio.gather(work(), work(), work())

    assert peak == 1
    assert not gate.exclusive_held


async def test_a_nested_shared_request_passes_through_a_queued_writer() -> None:
    gate = LifecycleGate()
    entered_inner = asyncio.Event()
    outer_may_finish = asyncio.Event()

    async def reader() -> None:
        async with gate.shared():
            await outer_may_finish.wait()
            async with gate.shared():  # would deadlock behind the writer if it queued
                entered_inner.set()

    async def writer() -> None:
        async with gate.exclusive():
            pass

    r = asyncio.create_task(reader())
    await _settle()
    w = asyncio.create_task(writer())
    await _settle()
    outer_may_finish.set()

    await asyncio.wait_for(asyncio.gather(r, w), timeout=2)

    assert entered_inner.is_set()


async def test_upgrading_shared_to_exclusive_fails_instead_of_deadlocking() -> None:
    gate = LifecycleGate()

    async with gate.shared():
        with pytest.raises(GateUpgradeError):
            async with gate.exclusive():
                pass

    assert gate.shared_count == 0


async def test_a_task_started_inside_a_shared_section_is_a_new_requester() -> None:
    gate = LifecycleGate()
    entered = asyncio.Event()

    async def child() -> None:
        async with gate.exclusive():
            entered.set()

    async with gate.shared():
        task = asyncio.create_task(child())
        await _settle()
        assert not entered.is_set()  # waits for the parent instead of raising
    await asyncio.wait_for(task, timeout=2)

    assert entered.is_set()


async def test_nested_exclusive_fails_instead_of_deadlocking() -> None:
    gate = LifecycleGate()

    async with gate.exclusive():
        with pytest.raises(GateUpgradeError):
            async with gate.exclusive():
                pass

    assert not gate.exclusive_held


async def test_two_gates_do_not_see_each_other() -> None:
    a, b = LifecycleGate(), LifecycleGate()

    async with a.shared(), b.exclusive():
        assert b.exclusive_held


async def test_a_cancelled_waiter_leaves_the_gate_usable() -> None:
    gate = LifecycleGate()
    release = asyncio.Event()

    async def holder() -> None:
        async with gate.shared():
            await release.wait()

    async def writer() -> None:
        async with gate.exclusive():
            pass

    h = asyncio.create_task(holder())
    await _settle()
    w = asyncio.create_task(writer())
    await _settle()
    w.cancel()
    with pytest.raises(asyncio.CancelledError):
        await w
    release.set()
    await h

    await _enter_and_leave(gate.exclusive())
    assert gate.waiting == 0


async def test_a_cancel_landing_right_after_the_grant_releases_it() -> None:
    gate = LifecycleGate()
    release = asyncio.Event()

    async def holder() -> None:
        async with gate.shared():
            await release.wait()

    async def writer() -> None:
        async with gate.exclusive():
            await asyncio.sleep(10)

    h = asyncio.create_task(holder())
    await _settle()
    w = asyncio.create_task(writer())
    await _settle()
    release.set()
    await h  # the writer is granted here, before it has run again
    w.cancel()
    with pytest.raises(asyncio.CancelledError):
        await w

    await _enter_and_leave(gate.shared())
    assert not gate.exclusive_held
