"""Runtime guards for execute_ifc_code: the enforcement layer.

Two independent switches shape the namespace:

- ``allow_mutation``: False in ask mode, and also False for QUERY-classified
  code in edit mode (a classifier false negative must not mutate silently).
  True only for EDIT/SYSTEM-classified code in edit mode.
- ``allow_system``: True only for SYSTEM-classified code in edit mode with
  exec.allow_system_access enabled.

Honesty: these guards stop accidents and defaults, not a determined
adversary; in-process CPython can always be escaped.
"""

from __future__ import annotations

import builtins as _builtins
import contextlib
import os
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import ifcopenshell
import ifcopenshell.api
import ifcopenshell.util.element
import ifcopenshell.util.selector
import ifcopenshell.util.unit

from ifc_console.sandbox.policy import is_sensitive_generated_path


class GuardError(RuntimeError):
    """A runtime guard blocked the operation."""


SAFE_IMPORT_ROOTS = {
    "math",
    "json",
    "re",
    "csv",
    "itertools",
    "functools",
    "collections",
    "statistics",
    "datetime",
    "textwrap",
    "uuid",
    "string",
    "fractions",
    "decimal",
    "heapq",
    "bisect",
    "enum",
    "dataclasses",
    "typing",
    "pprint",
    "unicodedata",
    "operator",
    "copy",
    "io",
    "ifcopenshell",
}

_REMOVED_BUILTINS = {
    "exec",
    "eval",
    "compile",
    "input",
    "breakpoint",
    "exit",
    "quit",
    "help",
    "open",
    "__import__",
}

# Attributes of the model file object blocked while mutation is locked.
BLOCKED_FILE_ATTRS = {
    "create_entity",
    "add",
    "remove",
    "batch",
    "unbatch",
    "assign_inverse",
    "unassign_inverse",
    "write",
    "wrapped_data",
    "begin_transaction",
    "end_transaction",
    "discard_transaction",
}

# io stays importable (StringIO/BytesIO are legitimate query tools), but its
# file constructors are the real builtins and would bypass both the write
# block and the allowed-dirs read restriction of the guarded open().
_IO_BLOCKS = {
    "open": "io.open is disabled in execute_ifc_code; use the built-in open() "
    "(read-only, inside the allowed directories) or save_ifc_file for model output.",
    "open_code": "io.open_code is disabled in execute_ifc_code; use the built-in open().",
    "FileIO": "io.FileIO is disabled in execute_ifc_code; use the built-in open().",
}

# Attributes of the top-level ifcopenshell module blocked while mutation is
# locked (each maps to the message shown to the LLM).
_LOCKED_MODULE_BLOCKS = {
    "api": "ifcopenshell.api is disabled while mutation is locked (ask mode, or "
    "code that was not classified as an edit). Ask the user to run /mode edit "
    "in the ifc-console terminal.",
    "open": "ifcopenshell.open is disabled in guarded runs; the session model is "
    "already available as `ifc` (use open_ifc_file to switch models).",
    "file": "creating new ifcopenshell files is disabled while mutation is locked.",
    "ifcopenshell_wrapper": "the low-level wrapper is disabled in guarded runs.",
}


class RaisingProxy:
    """Any use raises GuardError with a helpful message."""

    def __init__(self, label: str, message: str) -> None:
        object.__setattr__(self, "_label", label)
        object.__setattr__(self, "_message", message)

    def __getattr__(self, name: str) -> Any:
        raise GuardError(object.__getattribute__(self, "_message"))

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        raise GuardError(object.__getattribute__(self, "_message"))

    def __repr__(self) -> str:
        return f"<blocked: {object.__getattribute__(self, '_label')}>"


class ApiModule:
    """ifcopenshell.api with its submodules resolved on first use.

    The real package imports submodules lazily, so `ifc_api.pset` is an
    AttributeError until something imports it. Generated code (and every
    example in the docs) expects the attribute to work, so resolve it here.
    """

    def __init__(self, real: Any) -> None:
        object.__setattr__(self, "_real", real)

    def __getattr__(self, name: str) -> Any:
        real = object.__getattribute__(self, "_real")
        try:
            return getattr(real, name)
        except AttributeError:
            pass
        if name.startswith("_"):
            raise AttributeError(name)
        import importlib

        try:
            return importlib.import_module(f"ifcopenshell.api.{name}")
        except ImportError as exc:
            raise AttributeError(
                f"ifcopenshell.api has no module {name!r}; get_api_docs() lists them"
            ) from exc

    def __setattr__(self, name: str, value: Any) -> None:
        raise GuardError("modifying modules is blocked in execute_ifc_code")

    def __repr__(self) -> str:
        return "<ifcopenshell.api>"


class ModuleShim:
    """Delegates to a real module, blocking specific attributes."""

    def __init__(self, real: Any, blocked: dict[str, str]) -> None:
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_blocked", blocked)

    def __getattr__(self, name: str) -> Any:
        blocked = object.__getattribute__(self, "_blocked")
        if name in blocked:
            raise GuardError(blocked[name])
        return getattr(object.__getattribute__(self, "_real"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        raise GuardError("modifying modules is blocked in execute_ifc_code")

    def __repr__(self) -> str:
        return f"<guarded {object.__getattribute__(self, '_real').__name__}>"


class GuardedFile:
    """Wraps the session's ifcopenshell file; blocks mutating methods.

    Entities obtained by traversal are raw (proxying every entity is
    impractical); mutation through them is blocked at runtime by
    entity_mutation_lock, with the static classifier explaining the common
    cases up front.
    """

    def __init__(self, real: Any) -> None:
        object.__setattr__(self, "_real", real)

    def __getattr__(self, name: str) -> Any:
        if name in BLOCKED_FILE_ATTRS:
            raise GuardError(
                f"ifc.{name} is blocked while mutation is locked (ask mode, or code "
                "that was not classified as an edit). Ask the user to run /mode edit "
                "in the ifc-console terminal, then resubmit."
            )
        return getattr(object.__getattribute__(self, "_real"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        raise GuardError("assigning attributes on the model file is blocked")

    def __iter__(self):
        return iter(object.__getattribute__(self, "_real"))

    def __getitem__(self, key: Any) -> Any:
        return object.__getattribute__(self, "_real")[key]

    def __len__(self) -> int:
        return len(object.__getattribute__(self, "_real"))

    def __contains__(self, item: Any) -> bool:
        return item in object.__getattribute__(self, "_real")

    def __repr__(self) -> str:
        return f"<guarded {object.__getattribute__(self, '_real')!r}>"


_ENTITY_LOCKED_MSG = (
    "{what} is blocked while mutation is locked (ask mode, or code that was "
    "not classified as an edit). Ask the user to run /mode edit in the "
    "ifc-console terminal, then resubmit."
)

# Both guards below temporarily patch IfcOpenShell classes. Those classes are
# process-global, while ModelSession serialization only covers one AppCore.
# Install dispatching wrappers once and keep their policy thread-local: guarded
# runs stay blocked, while unrelated runtimes continue to use the real methods.
# A user count makes overlapping exits restore the exact originals only once.
_IFCOPENSHELL_PATCH_LOCK = threading.RLock()
_IFCOPENSHELL_GUARD_STATE = threading.local()
_IFCOPENSHELL_PATCH_USERS = 0
_IFCOPENSHELL_ORIGINALS: tuple[Any, ...] | None = None


def _guard_depth(kind: str) -> int:
    return int(getattr(_IFCOPENSHELL_GUARD_STATE, kind, 0))


def _enter_ifcopenshell_guard(kind: str) -> None:
    global _IFCOPENSHELL_ORIGINALS, _IFCOPENSHELL_PATCH_USERS

    with _IFCOPENSHELL_PATCH_LOCK:
        if _IFCOPENSHELL_PATCH_USERS == 0:
            entity_cls = ifcopenshell.entity_instance
            file_cls = ifcopenshell.file
            entity_setattr = entity_cls.__setattr__
            entity_setitem = entity_cls.__setitem__
            entity_file = entity_cls.file
            file_write = file_cls.write
            _IFCOPENSHELL_ORIGINALS = (
                entity_cls,
                entity_setattr,
                entity_setitem,
                entity_file,
                file_cls,
                file_write,
            )

            def guarded_setattr(self: Any, name: str, value: Any) -> None:
                if _guard_depth("entity"):
                    raise GuardError(
                        _ENTITY_LOCKED_MSG.format(what="assigning entity attributes")
                    )
                entity_setattr(self, name, value)

            def guarded_setitem(self: Any, index: Any, value: Any) -> None:
                if _guard_depth("entity"):
                    raise GuardError(
                        _ENTITY_LOCKED_MSG.format(
                            what="assigning entity attributes by index"
                        )
                    )
                entity_setitem(self, index, value)

            def guarded_file(self: Any) -> Any:
                real = entity_file.fget(self)
                return GuardedFile(real) if _guard_depth("entity") else real

            def guarded_write(self: Any, *args: Any, **kwargs: Any) -> Any:
                if _guard_depth("write"):
                    raise GuardError(
                        "writing an IFC file is disabled by files.allow_ai_save; changes "
                        "remain in memory until the user runs /save"
                    )
                return file_write(self, *args, **kwargs)

            entity_cls.__setattr__ = guarded_setattr
            entity_cls.__setitem__ = guarded_setitem
            entity_cls.file = property(
                guarded_file, entity_file.fset, entity_file.fdel, entity_file.__doc__
            )
            file_cls.write = guarded_write

        _IFCOPENSHELL_PATCH_USERS += 1
        setattr(_IFCOPENSHELL_GUARD_STATE, kind, _guard_depth(kind) + 1)


def _exit_ifcopenshell_guard(kind: str) -> None:
    global _IFCOPENSHELL_ORIGINALS, _IFCOPENSHELL_PATCH_USERS

    with _IFCOPENSHELL_PATCH_LOCK:
        depth = _guard_depth(kind) - 1
        if depth:
            setattr(_IFCOPENSHELL_GUARD_STATE, kind, depth)
        else:
            with contextlib.suppress(AttributeError):
                delattr(_IFCOPENSHELL_GUARD_STATE, kind)

        _IFCOPENSHELL_PATCH_USERS -= 1
        if _IFCOPENSHELL_PATCH_USERS == 0:
            assert _IFCOPENSHELL_ORIGINALS is not None
            entity_cls, entity_setattr, entity_setitem, entity_file, file_cls, file_write = (
                _IFCOPENSHELL_ORIGINALS
            )
            entity_cls.__setattr__ = entity_setattr
            entity_cls.__setitem__ = entity_setitem
            entity_cls.file = entity_file
            file_cls.write = file_write
            _IFCOPENSHELL_ORIGINALS = None


@contextlib.contextmanager
def entity_mutation_lock(enabled: bool = True) -> Iterator[None]:
    """Blocks mutation through raw entities for the duration of a guarded run.

    Entities reached by traversal are raw (proxying every entity is
    impractical), which leaves three accident-level routes around GuardedFile:
    `e.Name = x`, `e[2] = x`, and the `e.file` property handing back the
    unguarded model. While the lock is held, the entity class's setters raise
    and `.file` returns a GuardedFile. Process-global wrappers consult
    thread-local state so independent model sessions cannot interfere.
    """
    if not enabled:
        yield
        return
    _enter_ifcopenshell_guard("entity")
    try:
        yield
    finally:
        _exit_ifcopenshell_guard("entity")


@contextlib.contextmanager
def model_write_lock(enabled: bool = True) -> Iterator[None]:
    """Block raw ``ifcopenshell.file.write`` while AI persistence is off.

    Edit-mode code still receives the real model so every in-memory mutation
    API keeps working. Patching only ``write`` closes direct ``ifc.write`` and
    entity ``.file.write`` routes without affecting viewer ``to_string``.
    """
    if not enabled:
        yield
        return
    _enter_ifcopenshell_guard("write")
    try:
        yield
    finally:
        _exit_ifcopenshell_guard("write")


def _normalized(path: Path) -> str:
    try:
        resolved = os.path.realpath(os.fspath(path))
    except (OSError, TypeError, ValueError):
        resolved = os.path.abspath(os.fspath(path))
    return os.path.normcase(os.path.abspath(resolved))


def _inside_any(path: Path, roots: list[Path]) -> bool:
    normalized = _normalized(path)
    for root in roots:
        try:
            common = os.path.commonpath((normalized, _normalized(root)))
        except ValueError:
            continue
        if common == _normalized(root):
            return True
    return False


def make_open_guard(
    allowed_dirs: list[Path],
    allow_system: bool,
    deny_dirs: list[Path] | None = None,
) -> Callable:
    real_open = _builtins.open
    denied = list(deny_dirs or ())

    def guarded_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any):
        if allow_system:
            return real_open(file, mode, *args, **kwargs)
        if any(c in str(mode) for c in "wax+"):
            raise GuardError(
                "opening files for writing is blocked in execute_ifc_code; use "
                "save_ifc_file for model output, or ask the user for system access."
            )
        try:
            raw_path = Path(os.fspath(file))
            p = raw_path.resolve()
        except TypeError as exc:
            raise GuardError("only path-based, read-mode open() is allowed") from exc
        if (
            is_sensitive_generated_path(raw_path)
            or is_sensitive_generated_path(p)
            or _inside_any(p, denied)
        ):
            raise GuardError(f"reading {p} is blocked: it may contain credentials")
        if not _inside_any(p, allowed_dirs):
            allowed = ", ".join(str(r) for r in allowed_dirs) or "(none)"
            raise GuardError(f"reading {p} is outside the allowed directories: {allowed}")
        return real_open(p, mode, *args, **kwargs)

    return guarded_open


def make_import_guard(
    *,
    allow_system: bool,
    allow_mutation: bool,
    extra_deny: set[str] | None = None,
) -> Callable:
    """Allowlisting __import__ replacement.

    While mutation is locked, importing ifcopenshell (in any spelling) hands
    back the guarded shim, so `import ifcopenshell as x; x.api...` cannot
    sidestep the injected shim.
    """
    real_import = _builtins.__import__
    extra_deny = extra_deny or set()

    def guarded_import(
        name: str,
        globals: Any = None,
        locals: Any = None,
        fromlist: Any = (),
        level: int = 0,
    ):
        top = name.split(".")[0]
        if allow_system:
            return real_import(name, globals, locals, fromlist, level)
        if top in extra_deny or top not in SAFE_IMPORT_ROOTS:
            raise ImportError(
                f"import of {name!r} is not available in execute_ifc_code. Available: "
                "ifcopenshell (+submodules) and stdlib data modules (math, json, re, "
                "csv, statistics, datetime, collections, itertools, functools, ...)."
            )
        if top == "io":
            return ModuleShim(real_import(name, globals, locals, fromlist, level), _IO_BLOCKS)
        if top == "ifcopenshell" and not allow_mutation:
            if name == "ifcopenshell.api" or name.startswith("ifcopenshell.api."):
                raise ImportError(_LOCKED_MODULE_BLOCKS["api"])
            module = real_import(name, globals, locals, fromlist, level)
            if fromlist:
                # IMPORT_FROM will getattr() on what we return
                leaf = sys.modules.get(name, module)
                if name == "ifcopenshell":
                    return ModuleShim(leaf, _LOCKED_MODULE_BLOCKS)
                return leaf
            return ModuleShim(module, _LOCKED_MODULE_BLOCKS)
        return real_import(name, globals, locals, fromlist, level)

    return guarded_import


def build_namespace(
    ifc_file: Any,
    *,
    allow_mutation: bool,
    allow_system: bool,
    allowed_dirs: list[Path],
    extra_system_modules: tuple[str, ...] = (),
    deny_dirs: list[Path] | None = None,
) -> dict[str, Any]:
    """Fresh globals dict for one execute_ifc_code run."""
    ns_builtins = {k: v for k, v in vars(_builtins).items() if k not in _REMOVED_BUILTINS}
    ns_builtins["open"] = make_open_guard(allowed_dirs, allow_system, deny_dirs)
    ns_builtins["__import__"] = make_import_guard(
        allow_system=allow_system,
        allow_mutation=allow_mutation,
        extra_deny=set(extra_system_modules),
    )

    if allow_mutation:
        ifc_obj: Any = ifc_file
        ifcos: Any = ifcopenshell
        api_obj: Any = ApiModule(ifcopenshell.api)
    else:
        ifc_obj = GuardedFile(ifc_file) if ifc_file is not None else None
        ifcos = ModuleShim(ifcopenshell, _LOCKED_MODULE_BLOCKS)
        api_obj = RaisingProxy("ifc_api", _LOCKED_MODULE_BLOCKS["api"])

    def get_ifc_file() -> Any:
        if ifc_obj is None:
            raise RuntimeError("no IFC model is loaded")
        return ifc_obj

    def query(selector: str) -> list[Any]:
        if ifc_file is None:
            raise RuntimeError("no IFC model is loaded")
        return list(ifcopenshell.util.selector.filter_elements(ifc_file, selector))

    # Read-only domain helpers: shorter generated code, fewer guard trips.
    def by_class(name: str) -> list[Any]:
        if ifc_file is None:
            raise RuntimeError("no IFC model is loaded")
        return list(ifc_file.by_type(name))

    def psets(element: Any) -> dict:
        return ifcopenshell.util.element.get_psets(element, psets_only=True)

    def qtos(element: Any) -> dict:
        return ifcopenshell.util.element.get_psets(element, qtos_only=True)

    def container(element: Any) -> Any:
        return ifcopenshell.util.element.get_container(element)

    return {
        "__builtins__": ns_builtins,
        "__name__": "__ifc_code_exec__",
        "ifc": ifc_obj,
        "ifcopenshell": ifcos,
        "ifc_api": api_obj,
        "element_util": ifcopenshell.util.element,
        "selector_util": ifcopenshell.util.selector,
        "unit_util": ifcopenshell.util.unit,
        "get_ifc_file": get_ifc_file,
        "query": query,
        "by_class": by_class,
        "psets": psets,
        "qtos": qtos,
        "container": container,
    }
