"""The capability boundary, enforced inside the worker with a CPython audit hook.

Namespace guards can be escaped: any object graph reachable from user code
eventually reaches the real builtins. Audit hooks cannot. They are installed
below the object graph, they fire on the C implementation of every dangerous
operation, and CPython offers no way to remove one. So the rule here is not
"the code cannot reach socket", it is "opening a socket fails, however it was
reached".

Consequence for a leak: the worker has no network, no process creation, no
credentials in its environment, and no write access outside its scratch
directory. Escaping the namespace buys an attacker nothing they can send
anywhere.

Install once, after the heavy imports, and never disarm.
"""

from __future__ import annotations

import os
import sys
import threading
from collections.abc import Callable
from typing import Any

from ifc_console.sandbox.policy import (
    SandboxPolicy,
    is_sensitive_generated_path,
    runtime_roots,
)


class SandboxViolation(PermissionError):
    """The sandbox policy refused an operation."""


# Categorically unavailable: no argument makes these safe, and nothing the
# worker legitimately does needs them. Matched as prefixes.
_DENIED_PREFIXES: tuple[str, ...] = (
    "socket.",
    "urllib.Request",
    "http.client.",
    "ftplib.",
    "smtplib.",
    "imaplib.",
    "poplib.",
    "nntplib.",
    "telnetlib.",
    "webbrowser.",
    "subprocess.",
    "os.system",
    "os.exec",
    "os.spawn",
    "os.posix_spawn",
    "os.fork",
    "os.startfile",
    "pty.",
    "winreg.",
    "shutil.",
    "os.add_dll_directory",
    "os.putenv",
    "os.unsetenv",
    "os.kill",
    "os.killpg",
    "os.setuid",
    "os.setgid",
    "os.seteuid",
    "os.setegid",
    "msvcrt.",
    "ctypes.",
    "_thread.",
    "sqlite3.",
    "mmap.",
    "fcntl.",
    "code.__init__",
    "code.interact",
    "cpython.run_command",
    "cpython.run_file",
    "cpython.run_module",
    "cpython.run_stdin",
)

# ctypes reaches arbitrary memory and arbitrary shared libraries, which would
# route around every check above. Only the escape-capable events are denied;
# the rest of ctypes stays usable so imports that merely touch it still work.
_DENIED_EXACT: frozenset[str] = frozenset(
    {
        "ctypes.dlopen",
        "ctypes.dlsym",
        "ctypes.dlsym/handle",
        "ctypes.call_function",
        "ctypes.cdata",
        "ctypes.cdata/buffer",
        "ctypes.set_exception",
        "cpython.PyInterpreterState_New",
    }
)

_USER_CODE_DENIED_EXACT: frozenset[str] = frozenset(
    {
        "object.__getattr__",
        "object.__setattr__",
        "object.__delattr__",
        "sys._getframe",
        "sys._current_frames",
        "sys._current_exceptions",
        "sys.settrace",
        "sys.setprofile",
        "sys.addaudithook",
        "gc.get_objects",
        "gc.get_referrers",
        "gc.get_referents",
    }
)

_DENIED_IMPORT_ROOTS: frozenset[str] = frozenset(
    {"_interpreters", "_xxinterpchannels", "_xxsubinterpreters", "interpreters"}
)

# Path-bearing events, checked against the policy rather than denied outright:
# the worker does create temporary files, just only inside its scratch.
_WRITE_PATH_EVENTS: dict[str, tuple[int, ...]] = {
    "os.remove": (0,),
    "os.rename": (0, 1),
    "os.rmdir": (0,),
    "os.mkdir": (0,),
    "os.link": (0, 1),
    "os.symlink": (0, 1),
    "os.truncate": (0,),
    "os.chmod": (0,),
    "os.chown": (0,),
    "os.setxattr": (0,),
    "os.removexattr": (0,),
}

_READ_PATH_EVENTS: dict[str, tuple[int, ...]] = {
    "os.listdir": (0,),
    "os.scandir": (0,),
    "os.chdir": (0,),
    "glob.glob": (0,),
}

_DIR_FD_INDEXES: dict[str, tuple[int, ...]] = {
    "os.remove": (1,),
    "os.rename": (2, 3),
    "os.rmdir": (1,),
    "os.mkdir": (2,),
    "os.link": (2, 3),
    "os.symlink": (2,),
    "os.chmod": (2,),
    "os.chown": (3,),
}

_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC

_installed = False


def _norm(path: str) -> str:
    try:
        return os.path.normcase(os.path.realpath(path))
    except (OSError, ValueError):
        return os.path.normcase(os.path.abspath(path))


def _under(path: str, roots: tuple[str, ...]) -> bool:
    for root in roots:
        if path == root or path.startswith(root + os.sep):
            return True
        # normcase leaves the separator alone on POSIX and folds it on
        # Windows, where a root may still carry the alternate one.
        if os.altsep and path.startswith(root + os.altsep):
            return True
    return False


def _as_path(value: Any) -> str | None:
    """The path a path-bearing audit argument refers to, or None if it is a
    file descriptor (already-open handles are not a fresh capability)."""
    if value is None or isinstance(value, int):
        return None
    try:
        return os.fspath(value)  # type: ignore[arg-type]
    except TypeError:
        return None


def _check_indexes(
    check_path, args: tuple[Any, ...], indexes: tuple[int, ...], write: bool
) -> None:
    for i in indexes:
        if len(args) > i and isinstance(args[i], int):
            raise SandboxViolation("sandbox: access through a raw file descriptor is blocked")
        path = _as_path(args[i]) if len(args) > i else None
        if path is not None:
            check_path(path, write=write)


def install(
    policy: SandboxPolicy,
    *,
    is_user_code: Callable[[], bool] | None = None,
) -> list[str]:
    """Arm the boundary. Returns the list of controls now in force."""
    global _installed
    if _installed:
        return []

    read_roots = tuple(policy.read_roots) + runtime_roots()
    write_roots = tuple(policy.write_roots)
    deny_roots = tuple(policy.deny_roots)
    allow_network = policy.allow_network
    allow_process = policy.allow_process

    # Bound into the closure, not read from the module at call time: rebinding
    # ifc_console.sandbox.hooks._DENIED_PREFIXES must not widen the boundary.
    denied_exact = _DENIED_EXACT
    denied_prefixes = _DENIED_PREFIXES
    denied_import_roots = _DENIED_IMPORT_ROOTS
    user_code_denied = _USER_CODE_DENIED_EXACT
    write_events = _WRITE_PATH_EVENTS
    read_events = _READ_PATH_EVENTS
    dir_fd_indexes = _DIR_FD_INDEXES
    network_events = ("socket.", "urllib.", "http.client.", "ftplib.", "smtplib.")
    process_events = ("subprocess.", "os.system", "os.exec", "os.spawn", "os.fork")
    state = threading.local()

    def check_path(path: str, *, write: bool) -> None:
        resolved = _norm(path)
        if is_sensitive_generated_path(path) or is_sensitive_generated_path(resolved):
            raise SandboxViolation(
                f"sandbox: reading {path} is blocked because it may contain credentials"
            )
        # The scratch is the worker's own directory and lives under the console
        # home, which is denied wholesale. Carve it out before the deny check,
        # or the sandbox has nowhere at all to write.
        in_scratch = _under(resolved, write_roots)
        if not in_scratch and _under(resolved, deny_roots):
            raise SandboxViolation(f"sandbox: {path} is in a directory the sandbox may never touch")
        if write:
            if not in_scratch:
                raise SandboxViolation(
                    f"sandbox: writing {path} is blocked; the sandbox may only write "
                    "to its own scratch directory. Use save_ifc_file for model output."
                )
            return
        if not in_scratch and not _under(resolved, read_roots):
            raise SandboxViolation(
                f"sandbox: reading {path} is blocked; it is outside the allowed "
                "directories. Ask the user to launch with --allow-dir if it is needed."
            )

    def check_open(args: tuple[Any, ...]) -> None:
        if args and isinstance(args[0], int):
            raise SandboxViolation("sandbox: opening a raw file descriptor is blocked")
        path = _as_path(args[0]) if args else None
        if path is None:
            return
        mode = args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 else None
        if mode is None and not os.path.isabs(path):
            raise SandboxViolation(
                "sandbox: relative low-level file opens are blocked because their "
                "directory capability is not visible to the audit hook"
            )
        write = bool(mode) and any(c in str(mode) for c in "wax+")
        if isinstance(flags, int):
            write = write or bool(flags & _WRITE_FLAGS)
        check_path(path, write=write)

    def guarded(check, args: tuple[Any, ...]) -> None:
        """Run a path check under a re-entrancy guard that fails closed.

        The checks below touch no audited API, so re-entry means the flag was
        forged. Denying is then the safe answer; returning would disarm the
        whole path boundary from one assignment.
        """
        if getattr(state, "busy", False):
            raise SandboxViolation(
                "sandbox: the path boundary was re-entered; the operation is denied."
            )
        state.busy = True
        try:
            check(args)
        finally:
            state.busy = False

    def hook(event: str, args: tuple[Any, ...]) -> None:
        if event == "import" and args:
            import_root = str(args[0]).split(".", 1)[0]
            if import_root in denied_import_roots:
                raise SandboxViolation(
                    f"sandbox: importing {import_root} is blocked because it can "
                    "create an interpreter outside the audit boundary"
                )
        # `open` is by far the hottest event, and it carries no categorical
        # denial, so it is answered first.
        if event == "open":
            guarded(check_open, args)
            return
        # Categorical denials are decided before any mutable state is read.
        # No flag, forged or not, can put network, subprocess or ctypes back.
        if event in denied_exact or event.startswith(denied_prefixes):
            if allow_network and event.startswith(network_events):
                return
            if allow_process and event.startswith(process_events):
                return
            raise SandboxViolation(
                f"sandbox: {event} is blocked. The sandbox has no network, no "
                "subprocesses, and no access outside the model directories."
            )
        if is_user_code is not None:
            try:
                active = is_user_code()
            except BaseException:  # noqa: BLE001 - a broken gate must fail closed
                active = True
            if active and event in user_code_denied:
                raise SandboxViolation(
                    f"sandbox: {event} is blocked while generated code is running"
                )
        for index in dir_fd_indexes.get(event, ()):
            if len(args) > index and args[index] not in (None, -1):
                raise SandboxViolation("sandbox: directory-relative file operations are blocked")
        indexes = write_events.get(event)
        if indexes is not None:
            guarded(lambda a: _check_indexes(check_path, a, indexes, True), args)
            return
        indexes = read_events.get(event)
        if indexes is not None:
            guarded(lambda a: _check_indexes(check_path, a, indexes, False), args)

    sys.addaudithook(hook)
    _installed = True

    controls = ["filesystem-allowlist"]
    if not allow_network:
        controls.append("network-blocked")
    if not allow_process:
        controls.append("subprocess-blocked")
    controls.append("ctypes-blocked")
    if is_user_code is not None:
        controls.append("introspection-blocked")
    return controls


def installed() -> bool:
    return _installed
