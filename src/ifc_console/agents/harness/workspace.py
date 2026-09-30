"""Run folders for engine sessions, always under the console home.

An engine gets a scratch folder as its working directory, so its own file
tools cannot reach the user's project unless the engine is configured with
``cwd: project``. The folder also holds the engine's stderr log and, when
asked, the instructions file (AGENTS.md) some engines read on start.
"""

from __future__ import annotations

import shutil
from hashlib import sha256
from pathlib import Path

from ifc_console.agents.paths import agents_dir

RUNS_DIRNAME = "harness/runs"
STDERR_LOG = "engine.stderr.log"
# Written beside the engine's own file so Claude and Gemini read it too.
ALIAS_FILES = ("CLAUDE.md", "GEMINI.md")


def runs_root(home: str | Path) -> Path:
    return agents_dir(home) / RUNS_DIRNAME


def run_directory(home: str | Path, thread_id: str) -> Path:
    key = sha256(thread_id.encode("utf-8")).hexdigest()[:16]
    return runs_root(home) / key


def prepare_run_directory(
    home: str | Path,
    thread_id: str,
    *,
    instructions: str = "",
    instructions_file: str = "AGENTS.md",
) -> Path:
    """Create (or refresh) the folder one engine session runs in."""
    folder = run_directory(home, thread_id)
    folder.mkdir(parents=True, exist_ok=True)
    if instructions:
        names = {instructions_file, *ALIAS_FILES} if instructions_file else set(ALIAS_FILES)
        for name in names:
            (folder / name).write_text(instructions, encoding="utf-8")
    folder.touch()
    return folder


def release_run_directory(home: str | Path, thread_id: str) -> None:
    shutil.rmtree(run_directory(home, thread_id), ignore_errors=True)


def sweep_run_directories(home: str | Path, *, keep: int, active: set[str] = frozenset()) -> int:
    """Drop the oldest run folders beyond ``keep``, never one in use."""
    root = runs_root(home)
    if not root.is_dir():
        return 0
    folders = [path for path in root.iterdir() if path.is_dir() and path.name not in active]
    folders.sort(key=lambda path: path.stat().st_mtime)
    removed = 0
    for path in folders[: max(0, len(folders) - keep)]:
        shutil.rmtree(path, ignore_errors=True)
        removed += 1
    return removed


__all__ = [
    "STDERR_LOG",
    "prepare_run_directory",
    "release_run_directory",
    "run_directory",
    "runs_root",
    "sweep_run_directories",
]
