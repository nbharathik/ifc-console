"""Runtime guard / security suite (plan 04 §5, plan 10 §2.2).

Every mutation-bypass attempt while mutation is locked (ask mode) must
raise AND leave the on-disk file byte-identical and the in-memory max_id
unchanged.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import ifcopenshell
import pytest

from ifc_console.policy.guards import (
    GuardError,
    build_namespace,
    entity_mutation_lock,
    model_write_lock,
)
from ifc_console.session import executor


def _run(
    code: str, ifc, *, allow_mutation: bool, allow_system: bool = False, allowed=(), denied=()
):
    ns = build_namespace(
        ifc,
        allow_mutation=allow_mutation,
        allow_system=allow_system,
        allowed_dirs=[Path(p) for p in allowed],
        deny_dirs=[Path(p) for p in denied],
    )
    return executor.run(executor.prepare(code), ns, output_limit=40_000)


def _run_locked(code: str, ifc, *, allowed=()):
    """A guarded run exactly as tools_exec composes it: namespace + entity lock."""
    ns = build_namespace(
        ifc,
        allow_mutation=False,
        allow_system=False,
        allowed_dirs=[Path(p) for p in allowed],
    )
    with entity_mutation_lock():
        return executor.run(executor.prepare(code), ns, output_limit=40_000)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


MUTATION_BLOCKED = [
    "ifc_api.run('root.create_entity', ifc, ifc_class='IfcWall')",
    "ifc.create_entity('IfcWall')",
    "ifc.remove(ifc.by_type('IfcWall')[0])",
    "ifc.write('escape.ifc')",
    "import ifcopenshell.api\nifcopenshell.api.run('root.create_entity', ifc, ifc_class='IfcWall')",
    "import ifcopenshell as x\nx.api.run('root.create_entity', ifc, ifc_class='IfcWall')",
    "from ifcopenshell import api\napi.run('root.create_entity', ifc, ifc_class='IfcWall')",
    "open('escape.txt', 'w').write('x')",
    "import os\nos.remove('x')",
    "import subprocess\nsubprocess.run(['echo'])",
    "__import__('os').listdir('.')",
    "from pathlib import Path\nPath('escape.txt').write_text('x')",
    "import shutil",
]


@pytest.mark.parametrize("code", MUTATION_BLOCKED, ids=[c[:32] for c in MUTATION_BLOCKED])
def test_locked_mutation_blocks_and_preserves_disk(code: str, work_model: Path) -> None:
    before = _digest(work_model)
    ifc = ifcopenshell.open(str(work_model))
    max_before = ifc.wrapped_data.getMaxId()

    with pytest.raises((GuardError, ImportError, RuntimeError, Exception)):
        _run(code, ifc, allow_mutation=False, allowed=[work_model.parent])

    assert ifc.wrapped_data.getMaxId() == max_before, "in-memory model changed"
    assert _digest(work_model) == before, "on-disk file changed while mutation locked"


# Bypass routes through raw entities and the io module: closed by
# entity_mutation_lock and the io shim, not by GuardedFile.
ENTITY_MUTATION_BLOCKED = [
    "e = ifc.by_type('IfcWall')[0]\ne.Name = 'renamed'",
    "e = ifc.by_type('IfcWall')[0]\ne[2] = 'renamed'",
    "e = ifc.by_type('IfcWall')[0]\ne.file.remove(e)",
    "e = ifc.by_type('IfcWall')[0]\ne.file.create_entity('IfcWall')",
    "import io\nio.open('escape.txt', 'w').write('x')",
    "from io import open as o\no('escape.txt', 'w').write('x')",
]


@pytest.mark.parametrize(
    "code", ENTITY_MUTATION_BLOCKED, ids=[c.splitlines()[-1][:36] for c in ENTITY_MUTATION_BLOCKED]
)
def test_entity_level_bypasses_blocked(code: str, work_model: Path) -> None:
    before = _digest(work_model)
    ifc = ifcopenshell.open(str(work_model))
    max_before = ifc.wrapped_data.getMaxId()

    with pytest.raises(GuardError):
        _run_locked(code, ifc, allowed=[work_model.parent])

    assert ifc.wrapped_data.getMaxId() == max_before, "in-memory model changed"
    assert _digest(work_model) == before, "on-disk file changed while mutation locked"


def test_entity_lock_allows_reads(ifc4) -> None:
    code = (
        "e = ifc.by_type('IfcWall')[0]\nn = len(e.file.by_type('IfcWall'))\n(e.Name, e[0], n > 0)"
    )
    result = _run_locked(code, ifc4)
    assert result.result_repr is not None and "True" in result.result_repr


def test_entity_lock_restores_mutation_after_run(work_model: Path) -> None:
    ifc = ifcopenshell.open(str(work_model))
    with pytest.raises(GuardError):
        _run_locked("ifc.by_type('IfcWall')[0].Name = 'blocked'", ifc)
    wall = ifc.by_type("IfcWall")[0]
    wall.Name = "renamed-after"
    assert wall.Name == "renamed-after"


def test_io_data_classes_still_available(ifc4) -> None:
    result = _run_locked("import io\nbuf = io.StringIO()\nbuf.write('q')\nbuf.getvalue()", ifc4)
    assert result.result_repr == "'q'"


def test_import_allowlist_permits_data_modules(ifc4) -> None:
    result = _run("import json, math, re, collections\nmath.floor(1.5)", ifc4, allow_mutation=False)
    assert result.result_repr == "1"


@pytest.mark.parametrize(
    "code, exc",
    [
        ("exec('x = 1')", NameError),  # fully removed
        ("eval('1')", NameError),  # fully removed
        ("compile('1', '<s>', 'eval')", NameError),  # fully removed
        ("__import__('os')", ImportError),  # present but allowlisted
        ("open('x', 'w')", GuardError),  # present but write-guarded
    ],
)
def test_dangerous_builtins_are_neutralized(code: str, exc: type, ifc4) -> None:
    with pytest.raises(exc):
        _run(code, ifc4, allow_mutation=False)


def test_read_open_outside_allowed_dirs_blocked(ifc4, tmp_path: Path) -> None:
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    with pytest.raises(GuardError):
        _run(f"open(r'{outside}').read()", ifc4, allow_mutation=False, allowed=[])


def test_read_open_inside_allowed_dirs_ok(ifc4, tmp_path: Path) -> None:
    inside = tmp_path / "ok.txt"
    inside.write_text("hello")
    result = _run(f"open(r'{inside}').read()", ifc4, allow_mutation=False, allowed=[tmp_path])
    assert result.result_repr == "'hello'"


def test_mutation_allowed_when_unlocked(work_model: Path) -> None:
    ifc = ifcopenshell.open(str(work_model))
    before = ifc.wrapped_data.getMaxId()
    _run(
        "ifc_api.run('root.create_entity', ifc, ifc_class='IfcWall')",
        ifc,
        allow_mutation=True,
    )
    assert ifc.wrapped_data.getMaxId() > before


def test_model_write_lock_allows_memory_edits_but_blocks_ifc_serialization(
    work_model: Path, tmp_path: Path
) -> None:
    ifc = ifcopenshell.open(str(work_model))
    output = tmp_path / "ai-output.ifc"

    with model_write_lock():
        ifc.create_entity("IfcWall")
        with pytest.raises(GuardError, match="files.allow_ai_save"):
            ifc.write(str(output))

    assert not output.exists()
    assert len(ifc.by_type("IfcWall")) == 4
    ifc.write(str(output))
    assert output.exists()


def test_system_import_allowed_only_with_system_flag(ifc4) -> None:
    with pytest.raises(ImportError):
        _run("import os", ifc4, allow_mutation=True, allow_system=False)
    # with the system flag, the import goes through (edit-approved SYSTEM run)
    _run("import os\nos.getcwd()", ifc4, allow_mutation=True, allow_system=True)


def test_denied_dirs_beat_an_allowed_root(ifc4, tmp_path) -> None:
    """The console home holds the bearer token. Launching from a directory that
    contains it must not make it readable."""
    home = tmp_path / ".ifc-console"
    home.mkdir()
    (home / "token").write_text("SECRET-TOKEN")

    with pytest.raises(GuardError):
        _run(
            f"print(open(r'{home / 'token'}').read())",
            ifc4,
            allow_mutation=False,
            allowed=[tmp_path],
            denied=[home],
        )

    readable = tmp_path / "plain.txt"
    readable.write_text("fine")
    ok = _run(
        f"print(open(r'{readable}').read())",
        ifc4,
        allow_mutation=False,
        allowed=[tmp_path],
        denied=[home],
    )
    assert "fine" in ok.stdout


@pytest.mark.parametrize(
    "relative",
    [
        ".npmrc",
        ".pypirc",
        ".aws/credentials",
        ".git/config",
        ".envrc",
        ".env.production",
        ".env/secret.txt",
        "frontend/.npmrc",
        "packages/service/.aws/credentials",
        "nested/repository/.git/config",
    ],
)
def test_generated_code_cannot_read_project_credentials(core, ifc4, relative: str) -> None:
    root = core.allowed_dirs[0]
    denied = core.generated_code_deny_paths()
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("PROJECT-SECRET", encoding="utf-8")

    with pytest.raises(GuardError):
        _run(
            f"open(r'{target}').read()",
            ifc4,
            allow_mutation=False,
            allowed=[root],
            denied=denied,
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows path comparison")
def test_generated_code_credential_paths_are_case_insensitive_on_windows(core, ifc4) -> None:
    root = core.allowed_dirs[0]
    denied = core.generated_code_deny_paths()
    target = root / ".NPMRC"
    target.write_text("PROJECT-SECRET", encoding="utf-8")

    with pytest.raises(GuardError):
        _run(
            f"open(r'{target}').read()",
            ifc4,
            allow_mutation=False,
            allowed=[root],
            denied=denied,
        )


def test_generated_code_deny_paths_follow_directory_symlinks(core, ifc4) -> None:
    root = core.allowed_dirs[0]
    credential_dir = root / ".aws"
    credential_dir.mkdir()
    secret = credential_dir / "credentials"
    secret.write_text("PROJECT-SECRET", encoding="utf-8")
    alias = root / "credential-link"
    try:
        alias.symlink_to(credential_dir, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")

    with pytest.raises(GuardError):
        _run(
            f"open(r'{alias / 'credentials'}').read()",
            ifc4,
            allow_mutation=False,
            allowed=[root],
            denied=core.generated_code_deny_paths(),
        )


def test_generated_code_can_still_read_an_allowed_ifc(core, ifc4, work_model: Path) -> None:
    core.add_allowed_dir(work_model.parent)
    result = _run(
        f"open(r'{work_model}', encoding='utf-8').read(7)",
        ifc4,
        allow_mutation=False,
        allowed=list(core.allowed_dirs),
        denied=core.generated_code_deny_paths(),
    )
    assert result.result_repr == "'ISO-103'"


class TestKnownBypasses:
    """Documents guard limits honestly (plan 04 §8). These are tracked M6 items.

    strict xfail: if in-process CPython ever stops allowing one of these, the
    test flips to a failure and we get to tighten the docs/claim.
    """

    @pytest.mark.xfail(strict=True, reason="in-process object-graph walk can reach real types")
    def test_subclass_walk_reaches_builtins(self, ifc4) -> None:
        # A determined adversary can still walk __subclasses__ to find os via
        # a live reference; classify() routes this to SYSTEM (blocked in ask
        # mode), but nothing at runtime prevents it once system is granted.
        code = (
            "cls = ().__class__.__bases__[0]\n"
            "next(c for c in cls.__subclasses__() if c.__name__ == 'nonexistent')"
        )
        with pytest.raises((GuardError, ImportError)):
            _run(code, ifc4, allow_mutation=False)
