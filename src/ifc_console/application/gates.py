"""One gate for what may run against the loaded models.

Work on a model (queries, viewer routes, edits, saves) runs shared. Changing
which models are loaded runs exclusive. Waiters are served in arrival order, so
a queued exclusive request is never starved by a stream of shared ones.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from collections.abc import AsyncIterator
from contextvars import ContextVar

# (gate id, mode, holding task). Tasks started inside a section inherit this, so
# the holder is recorded to tell a nested request from a new one.
_HELD: ContextVar[tuple[tuple[int, str, asyncio.Task | None], ...]] = ContextVar(
    "ifc_console_gates_held", default=()
)


class GateUpgradeError(RuntimeError):
    """A task that already holds the gate asked for it exclusively.

    The request could never be granted, so failing loudly beats a deadlock.
    """


class LifecycleGate:
    def __init__(self) -> None:
        self._shared = 0
        self._exclusive = False
        self._queue: deque[tuple[bool, asyncio.Future[None]]] = deque()

    @property
    def shared_count(self) -> int:
        return self._shared

    @property
    def exclusive_held(self) -> bool:
        return self._exclusive

    @property
    def waiting(self) -> int:
        return sum(1 for _, waiter in self._queue if not waiter.done())

    def _holding(self) -> tuple[str, asyncio.Task | None] | None:
        me = id(self)
        return next(((mode, task) for gate, mode, task in _HELD.get() if gate == me), None)

    def _grantable(self, exclusive: bool) -> bool:
        if self._exclusive:
            return False
        return self._shared == 0 if exclusive else True

    def _grant(self, exclusive: bool) -> None:
        if exclusive:
            self._exclusive = True
        else:
            self._shared += 1

    def _wake(self) -> None:
        while self._queue:
            exclusive, waiter = self._queue[0]
            if waiter.done():
                self._queue.popleft()
                continue
            if not self._grantable(exclusive):
                return
            self._queue.popleft()
            self._grant(exclusive)
            waiter.set_result(None)
            if exclusive:
                return

    def _release(self, exclusive: bool) -> None:
        if exclusive:
            self._exclusive = False
        else:
            self._shared -= 1
        self._wake()

    async def _acquire(self, exclusive: bool) -> None:
        if not self._queue and self._grantable(exclusive):
            self._grant(exclusive)
            return
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._queue.append((exclusive, waiter))
        try:
            await waiter
        except asyncio.CancelledError:
            if waiter.done() and not waiter.cancelled():
                self._release(exclusive)  # granted an instant before the cancel
            else:
                self._wake()
            raise

    @contextlib.asynccontextmanager
    async def shared(self) -> AsyncIterator[None]:
        """Work on the loaded models.

        Inside a section of this gate, including one a task started from it, the
        request passes through: queueing behind a waiting writer there would
        make the outer section wait on itself.
        """
        if self._holding() is not None:
            yield
            return
        await self._acquire(False)
        token = _HELD.set((*_HELD.get(), (id(self), "shared", asyncio.current_task())))
        try:
            yield
        finally:
            _HELD.reset(token)
            self._release(False)

    @contextlib.asynccontextmanager
    async def exclusive(self) -> AsyncIterator[None]:
        """A change to which models are loaded. Waits for shared work to drain."""
        holding = self._holding()
        if holding is not None and holding[1] is asyncio.current_task():
            raise GateUpgradeError(
                f"the lifecycle gate is already held {holding[0]} by this task; "
                "asking for it exclusively would wait on itself"
            )
        await self._acquire(True)
        token = _HELD.set((*_HELD.get(), (id(self), "exclusive", asyncio.current_task())))
        try:
            yield
        finally:
            _HELD.reset(token)
            self._release(True)
