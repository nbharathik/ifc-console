"""What generated code may import, and how that answer is published.

Two policies. ``open`` (the default) lets code import anything installed
except a denied set: the model is not held to a list someone thought of in
advance, which is what generating real geometry needs. ``strict`` restores the
older curated allowlist for installations that want a small, fixed surface.

The denied set is the point of the open policy. It is organised by the
capability it would hand over, not by package popularity: reaching the
operating system, the network, another process, the interpreter itself, this
console's own state, or a deserializer that executes what it reads.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

# Reaching the machine. Also what the static classifier calls SYSTEM-class
# code, so importing one of these is explained before the code ever runs.
SYSTEM_MODULES = {
    "os",
    "sys",
    "subprocess",
    "shutil",
    "pathlib",
    "socket",
    "http",
    "urllib",
    "requests",
    "ftplib",
    "ctypes",
    "multiprocessing",
    "threading",
    "importlib",
    "builtins",
    "pickle",
    "marshal",
    "tempfile",
    "webbrowser",
    "asyncio",
    "_thread",
    "sqlite3",
    "mmap",
    "fcntl",
    "gc",
    "_xxsubinterpreters",
}

# Installed packages that would hand back a capability the stdlib list above
# already denies. A denylist is never complete; these are the ones that ship in
# this environment or commonly sit beside it.
_NETWORK = {
    "httpx",
    "httpcore",
    "urllib3",
    "aiohttp",
    "websockets",
    "websocket",
    "socks",
    "socketio",
    "smtplib",
    "telnetlib",
    "imaplib",
    "poplib",
    "nntplib",
    "paramiko",
    "fabric",
    "boto3",
    "botocore",
    "google",
    "azure",
    "gql",
}

# Credentials, and the clients that hold them.
_SECRETS = {
    "keyring",
    "openai",
    "anthropic",
    "mcp",
    "dotenv",
    "netrc",
}

# Starting another process, or becoming another interpreter.
_PROCESS = {
    "joblib",
    "loky",
    "multiprocess",
    "dill",
    "cloudpickle",
    "pip",
    "setuptools",
    "pkg_resources",
    "distutils",
    "venv",
    "ensurepip",
    "runpy",
    "code",
    "codeop",
    "pdb",
    "bdb",
    "trace",
    "IPython",
    "numba",
    "cython",
    "Cython",
}

# Deserializers that run what they read, and templating engines that do the
# same with a string.
_EXECUTES_DATA = {
    "yaml",
    "ruamel",
    "jsonpickle",
    "shelve",
    "dbm",
    "jinja2",
    "mako",
}

# This console. Generated code must not reach the session token, the settings
# store, the audit log, or the policy engine that is gating it.
_HOST = {
    "ifc_console",
    "ifc_console_agents",
}

DENIED_IMPORT_ROOTS = SYSTEM_MODULES | _NETWORK | _SECRETS | _PROCESS | _EXECUTES_DATA | _HOST

# Named in the refusal message: the ones a model reaches for first.
DENIED_EXAMPLES = ("os", "sys", "subprocess", "pathlib", "requests", "httpx", "pickle")

# Known to be here and worth suggesting. Not a limit under the open policy:
# anything else installed imports too, and anything absent fails the way a
# missing package always does.
IMPORT_GROUPS: dict[str, tuple[str, ...]] = {
    "geometry and numerics": ("numpy", "shapely", "trimesh"),
    "ifc": ("ifcopenshell", "ifctester", "bcf"),
    "numbers and text": (
        "math",
        "cmath",
        "statistics",
        "random",
        "secrets",
        "fractions",
        "decimal",
        "numbers",
        "json",
        "re",
        "csv",
        "textwrap",
        "string",
        "unicodedata",
        "difflib",
        "colorsys",
    ),
    "structure and iteration": (
        "itertools",
        "functools",
        "operator",
        "collections",
        "heapq",
        "bisect",
        "graphlib",
        "array",
        "struct",
        "enum",
        "dataclasses",
        "typing",
        "abc",
        "contextlib",
        "copy",
        "pprint",
        "warnings",
        "datetime",
        "calendar",
        "time",
        "uuid",
        "hashlib",
        "base64",
        "binascii",
        "zlib",
        "io",
    ),
}

# The whole surface under the strict policy.
SAFE_IMPORT_ROOTS = {name for names in IMPORT_GROUPS.values() for name in names}

def import_allowed(
    root: str,
    *,
    policy: str = "open",
    extra_allow: frozenset[str] | set[str] = frozenset(),
    extra_deny: frozenset[str] | set[str] = frozenset(),
) -> bool:
    """Whether generated code may import this top-level package."""
    if root in extra_deny:
        return False
    if root in extra_allow:
        return True
    if policy == "strict":
        return root in SAFE_IMPORT_ROOTS
    return root not in DENIED_IMPORT_ROOTS


def refusal(name: str, *, policy: str) -> str:
    """Why an import was refused, and what to reach for instead."""
    if policy == "strict":
        return (
            f"import of {name!r} is not available in execute_ifc_code: this session "
            "uses the strict import policy, which allows ifcopenshell, numpy, "
            "shapely, trimesh and the stdlib compute modules only. The user can "
            "open it with /settings exec.import_policy open, or add one package "
            "with exec.import_roots_extra."
        )
    return (
        f"import of {name!r} is blocked in execute_ifc_code: it reaches the operating "
        "system, the network, another process, this console's own state, or a "
        "deserializer that executes what it reads. Every other installed package is "
        "importable. Read files with the built-in open() inside the allowed "
        "directories, and write IFC with save_ifc_file."
    )


@lru_cache(maxsize=256)
def is_installed(root: str) -> bool:
    """Whether a package is importable here. Touches the path, not the module.

    Cached: the environment is published on every capability listing, and a
    path scan per name per call is not worth paying. A package installed
    mid-session is picked up on the next start, like the sandbox worker's.
    """
    import importlib.util

    try:
        return importlib.util.find_spec(root) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


def code_environment(
    *,
    policy: str = "open",
    extra_import_roots: tuple[str, ...] = (),
    injected: dict[str, str] | None = None,
) -> dict[str, Any]:
    """What execute_ifc_code offers, resolved against this installation.

    Published before any code is written, so a run is never spent discovering
    that a library is missing or refused.
    """
    groups: dict[str, list[str]] = {}
    for label, names in IMPORT_GROUPS.items():
        available = [name for name in names if is_installed(name)]
        if available:
            groups[label] = available
    extra = sorted({root for root in extra_import_roots if root and is_installed(root)})
    if extra:
        groups["added by the user"] = extra
    open_policy = policy != "strict"
    return {
        "policy": "open" if open_policy else "strict",
        "injected": dict(injected or {}),
        "installed": groups,
        "other_packages": (
            "any other installed package imports too; one that is not installed "
            "fails with ModuleNotFoundError, which costs a single call"
            if open_policy
            else "nothing outside this list may be imported"
        ),
        "blocked": list(DENIED_EXAMPLES),
        "blocked_reason": (
            "the operating system, the network, other processes, this console's own "
            "state, and deserializers that execute what they read"
        ),
        "note": (
            "open() is read-only and limited to the allowed directories; write IFC "
            "with save_ifc_file."
        ),
    }
