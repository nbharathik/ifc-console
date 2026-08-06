"""The sandboxed executor process. Started by client.py, never by a user.

Startup order is the security-relevant part:

1. Move the protocol to a private file descriptor and point fd 1 at stderr,
   so a C extension that writes to stdout cannot corrupt the pipe.
2. Read the init frame and cap our own resources.
3. Import everything heavy while the process is still ordinary.
4. Arm the audit hook, permanently.
5. Only then serve requests.

The worker holds its own copy of the model, read from disk. It is never the
authority: mutations here are discarded, which is exactly why running an
untrusted query in it is safe.
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
from typing import Any, BinaryIO

from ifc_console.sandbox import protocol
from ifc_console.sandbox.policy import SandboxPolicy


class _Worker:
    def __init__(self, rx: BinaryIO, tx: BinaryIO) -> None:
        self.rx = rx
        self.tx = tx
        self.policy = SandboxPolicy()
        self.ifc: Any = None
        self.path: str | None = None
        self.load_key: str | None = None

    # -- startup ---------------------------------------------------------------
    def init(self, request: dict[str, Any]) -> dict[str, Any]:
        from ifc_console.sandbox import hooks, limits

        self.policy = SandboxPolicy.from_dict(request.get("policy") or {})
        applied = limits.apply_self_limits(self.policy.memory_mb)
        preloaded = _preload()
        controls = hooks.install(self.policy)
        return {
            "ok": True,
            "pid": os.getpid(),
            "limits": applied,
            "controls": controls,
            "preloaded": preloaded,
        }

    # -- model -------------------------------------------------------------------
    def load(self, request: dict[str, Any]) -> dict[str, Any]:
        import ifcopenshell

        path = str(request["path"])
        self.ifc = None  # drop the old copy before doubling memory
        self.ifc = ifcopenshell.open(path)
        self.path = path
        self.load_key = request.get("key")
        return {
            "ok": True,
            "schema": getattr(self.ifc, "schema", None),
            "key": self.load_key,
        }

    # -- execution ----------------------------------------------------------------
    def run(self, request: dict[str, Any]) -> dict[str, Any]:
        from pathlib import Path

        from ifc_console.policy.guards import GuardError, build_namespace, entity_mutation_lock
        from ifc_console.sandbox.hooks import SandboxViolation
        from ifc_console.session import executor

        code = request["code"]
        output_limit = int(request.get("output_limit") or 40_000)
        allowed_dirs = [Path(p) for p in request.get("allowed_dirs") or []]

        try:
            compiled = executor.prepare(code)
        except SyntaxError as exc:
            return {"ok": False, "kind": "syntax", "message": str(exc)}

        namespace = build_namespace(
            self.ifc,
            allow_mutation=False,
            allow_system=False,
            allowed_dirs=allowed_dirs,
            extra_system_modules=tuple(request.get("extra_system_modules") or ()),
        )
        before = self._max_id()
        try:
            with entity_mutation_lock(enabled=True):
                result = executor.run(compiled, namespace, output_limit=output_limit)
        except SandboxViolation as exc:
            return {"ok": False, "kind": "violation", "message": str(exc)}
        except GuardError as exc:
            return {"ok": False, "kind": "guard", "message": str(exc)}
        except BaseException as exc:  # noqa: BLE001 - reported, not handled
            return {
                "ok": False,
                "kind": "error",
                "type": type(exc).__name__,
                "message": str(exc),
                "traceback": executor.format_traceback(exc),
            }
        return {
            "ok": True,
            "stdout": result.stdout,
            "result": result.result_repr,
            "stdout_truncated": result.stdout_truncated,
            "max_id_before": before,
            "max_id_after": self._max_id(),
        }

    def _max_id(self) -> int | None:
        try:
            return int(self.ifc.wrapped_data.getMaxId())
        except Exception:
            return None

    # -- loop -----------------------------------------------------------------------
    def serve(self) -> None:
        while True:
            try:
                request = protocol.read_frame(self.rx)
            except protocol.ProtocolError as exc:
                protocol.write_frame(self.tx, {"ok": False, "kind": "protocol", "message": str(exc)})
                return
            if request is None:
                return
            op = request.get("op")
            if op == "shutdown":
                return
            handler = {
                "init": self.init,
                "load": self.load,
                "run": self.run,
                "ping": lambda _r: {"ok": True},
            }.get(str(op))
            if handler is None:
                reply: dict[str, Any] = {
                    "ok": False,
                    "kind": "protocol",
                    "message": f"unknown op {op!r}",
                }
            else:
                try:
                    reply = handler(request)
                except BaseException as exc:  # noqa: BLE001 - the worker must answer
                    reply = {
                        "ok": False,
                        "kind": "worker",
                        "type": type(exc).__name__,
                        "message": str(exc),
                    }
            reply["id"] = request.get("id")
            protocol.write_frame(self.tx, reply)


def _preload() -> list[str]:
    """Import everything the worker will need before the hook goes up.

    Extension modules sometimes reach for ctypes or the registry on first
    import; doing that now keeps the policy free to deny both afterwards.
    """
    loaded: list[str] = []
    for name in (
        "ifcopenshell",
        "ifcopenshell.api",
        "ifcopenshell.util.element",
        "ifcopenshell.util.selector",
        "ifcopenshell.util.unit",
        "ifcopenshell.util.placement",
        "ifc_console.policy.guards",
        "ifc_console.session.executor",
        "numpy",
        "json",
        "csv",
        "statistics",
        "datetime",
        "decimal",
        "fractions",
        "unicodedata",
        "uuid",
    ):
        try:
            __import__(name)
        except Exception:
            continue
        loaded.append(name)
    return loaded


def main() -> None:
    # A private copy of the real stdout carries the protocol; fd 1 becomes a
    # second stderr so anything that prints cannot corrupt a frame.
    private_fd = os.dup(1)
    os.dup2(2, 1)
    tx = os.fdopen(private_fd, "wb", buffering=0)
    rx = sys.stdin.buffer

    worker = _Worker(rx, tx)
    try:
        worker.serve()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    finally:
        with contextlib.suppress(Exception):
            tx.close()


if __name__ == "__main__":
    with contextlib.suppress(Exception):
        sys.stderr.reconfigure(errors="replace")  # type: ignore[union-attr]
    if not isinstance(sys.stdin, io.IOBase):  # pragma: no cover - defensive
        raise SystemExit("sandbox worker requires a piped stdin")
    main()
