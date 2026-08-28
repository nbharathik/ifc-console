"""The sandbox worker, spawned for real.

These tests are the security claim in executable form: code that fully
escapes the namespace guards still cannot open a socket, start a process,
read outside the allowed directories, or write anywhere but its scratch.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

from ifc_console.sandbox.client import SandboxProcess, SandboxTimeout
from ifc_console.sandbox.policy import SandboxPolicy
from ifc_console.sandbox.runner import secure_isolation_supported

# Reaches the real builtins through the object graph. tests/unit/test_guards.py
# documents the same walk as a known in-process bypass; here it must not help.
ESCAPE = """
b = None
for c in ().__class__.__base__.__subclasses__():
    if c.__name__ == 'catch_warnings':
        b = c()._module.__builtins__
        break
"""


@pytest.fixture(scope="module")
def model_path() -> Path:
    """Module-scoped twin of the minimal_ifc4_path fixture: one worker for the
    whole file is worth the duplication."""
    from tests.fixtures_plugin import GENERATED, _ensure_fixtures

    _ensure_fixtures()
    return GENERATED / "minimal_ifc4.ifc"


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory, model_path: Path):
    tmp = tmp_path_factory.mktemp("sandbox")
    scratch = tmp / "scratch"
    denied = tmp / "denied"
    denied.mkdir()
    (denied / "token").write_text("SECRET-TOKEN")

    policy = SandboxPolicy.build(
        read_dirs=[model_path.parent],
        scratch_dir=scratch,
        deny_dirs=[denied],
        memory_mb=1024,
    )
    process = SandboxProcess(policy, scratch)
    process.start(timeout=300)
    process.request({"op": "load", "path": str(model_path), "key": "k"}, timeout=300)
    process.model_dir = model_path.parent  # type: ignore[attr-defined]
    process.scratch_dir = scratch  # type: ignore[attr-defined]
    process.denied_dir = denied  # type: ignore[attr-defined]
    yield process
    process.terminate()


def run(sandbox: SandboxProcess, code: str, timeout: float = 60) -> dict:
    return sandbox.request(
        {
            "op": "run",
            "code": code,
            "output_limit": 4000,
            "allowed_dirs": [str(sandbox.model_dir)],  # type: ignore[attr-defined]
        },
        timeout=timeout,
    )


# -- it still works as an executor -------------------------------------------------
def test_query_runs_and_returns_a_repl_value(sandbox) -> None:
    reply = run(sandbox, "len(ifc.by_type('IfcWall'))")
    assert reply["ok"] is True
    assert reply["result"] == "3"


def test_stdout_is_captured_not_leaked_into_the_pipe(sandbox) -> None:
    reply = run(sandbox, "print('hello')\nprint(1 + 1)")
    assert reply["ok"] is True
    assert reply["stdout"] == "hello\n2\n"


def test_runtime_errors_come_back_with_a_traceback(sandbox) -> None:
    reply = run(sandbox, "1 / 0")
    assert reply["ok"] is False
    assert reply["kind"] == "error"
    assert "ZeroDivisionError" in reply["traceback"]


def test_large_exception_text_is_clipped_and_the_worker_survives(sandbox) -> None:
    reply = run(sandbox, "raise RuntimeError('x' * 2_000_000)")
    assert reply["ok"] is False
    assert reply["kind"] == "error"
    assert len(reply["message"]) < 110_000
    assert len(reply["traceback"]) < 110_000
    assert run(sandbox, "1 + 1")["result"] == "2"


def test_lone_unicode_surrogates_do_not_break_the_protocol(sandbox) -> None:
    reply = run(sandbox, "print(chr(0xD800))\nchr(0xD800)")
    assert reply["ok"] is True
    assert reply["stdout"].startswith("\ud800")


def test_syntax_errors_are_reported_as_syntax(sandbox) -> None:
    reply = run(sandbox, "for x in :")
    assert reply["ok"] is False
    assert reply["kind"] == "syntax"


def test_the_worker_survives_the_startup_controls(sandbox) -> None:
    controls = sandbox.info["controls"]
    assert "network-blocked" in controls
    assert "subprocess-blocked" in controls
    assert "filesystem-allowlist" in controls


# -- containment ---------------------------------------------------------------------
def test_namespace_guard_still_blocks_mutation(sandbox) -> None:
    reply = run(sandbox, "getattr(ifc, 'create' + '_entity')('IfcWall')")
    assert reply["ok"] is False
    assert reply["kind"] == "guard"


@pytest.mark.parametrize(
    ("label", "code"),
    [
        ("socket", "b['__import__']('socket').socket()"),
        ("os.system", "b['__import__']('os').system('echo pwned')"),
        ("subprocess", "b['__import__']('subprocess').Popen(['echo'])"),
        ("ctypes", "b['__import__']('ctypes').CDLL('kernel32')"),
        ("urllib", "b['__import__']('urllib.request').request.urlopen('http://127.0.0.1:1/')"),
    ],
)
def test_escaped_code_cannot_reach_the_outside_world(sandbox, label, code) -> None:
    reply = run(sandbox, ESCAPE + code)
    assert reply["ok"] is False, f"{label} was not blocked"
    assert reply["kind"] == "violation", f"{label}: {reply.get('message')}"


@pytest.mark.parametrize(
    ("label", "code"),
    [
        ("ctypes memory", "b['__import__']('ctypes').string_at(b'abc', 1)"),
        pytest.param(
            "raw thread",
            "b['__import__']('_thread').start_new_thread(lambda: None, ())",
            marks=pytest.mark.skipif(
                not secure_isolation_supported(),
                reason=(
                    "the product disables secure isolation when raw thread creation "
                    "is not audited"
                ),
            ),
        ),
        ("raw descriptor", "b['open'](2, closefd=False)"),
    ],
)
def test_escaped_code_cannot_use_native_escape_primitives(sandbox, label, code) -> None:
    reply = run(sandbox, ESCAPE + code)
    assert reply["ok"] is False, f"{label} was not blocked"
    assert reply["kind"] == "violation", f"{label}: {reply.get('message')}"


def test_escaped_code_cannot_create_a_sqlite_database(sandbox) -> None:
    target = sandbox.scratch_dir / "escape.sqlite"  # type: ignore[attr-defined]
    reply = run(
        sandbox,
        ESCAPE + f"b['__import__']('sqlite3').connect(r'{target}')",
    )
    assert reply["ok"] is False
    assert reply["kind"] == "violation"
    assert not target.exists()


def test_escaped_code_cannot_create_an_unhooked_subinterpreter(sandbox) -> None:
    if importlib.util.find_spec("_xxsubinterpreters") is None:
        pytest.skip("this Python build has no subinterpreter module")
    reply = run(sandbox, ESCAPE + "b['__import__']('_xxsubinterpreters').create()")
    assert reply["ok"] is False
    assert reply["kind"] == "violation"


def test_traceback_frames_are_hidden_while_generated_code_runs(sandbox) -> None:
    code = ESCAPE + (
        "try:\n"
        "    1 / 0\n"
        "except Exception as exc:\n"
        "    print(exc.__traceback__.tb_frame.f_globals)\n"
    )
    reply = run(sandbox, code)
    assert reply["ok"] is False
    assert reply["kind"] == "violation"

    ordinary_error = run(sandbox, "1 / 0")
    assert ordinary_error["kind"] == "error"
    assert "ZeroDivisionError" in ordinary_error["traceback"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX dir_fd behavior")
def test_relative_os_open_cannot_hide_a_directory_fd(sandbox) -> None:
    target = sandbox.model_dir / "dir-fd-escape.txt"  # type: ignore[attr-defined]
    code = ESCAPE + (
        "o = b['__import__']('os')\n"
        f"fd = o.open(r'{sandbox.model_dir}', o.O_RDONLY)\n"  # type: ignore[attr-defined]
        "try:\n"
        "    o.open('dir-fd-escape.txt', o.O_WRONLY | o.O_CREAT, dir_fd=fd)\n"
        "finally:\n"
        "    o.close(fd)\n"
    )
    reply = run(sandbox, code)
    assert reply["ok"] is False
    assert reply["kind"] == "violation"
    assert not target.exists()


def test_escaped_code_cannot_read_outside_the_allowed_directories(sandbox) -> None:
    secret = sandbox.denied_dir / "token"  # type: ignore[attr-defined]
    reply = run(sandbox, ESCAPE + f"print(b['open'](r'{secret}').read())")
    assert reply["ok"] is False
    assert reply["kind"] == "violation"
    assert "SECRET-TOKEN" not in str(reply)


def test_escaped_code_cannot_write_into_the_model_directory(sandbox) -> None:
    target = sandbox.model_dir / "evil.txt"  # type: ignore[attr-defined]
    reply = run(sandbox, ESCAPE + f"b['open'](r'{target}', 'w').write('x')")
    assert reply["ok"] is False
    assert reply["kind"] == "violation"
    assert not target.exists()


def test_escaped_code_cannot_delete_the_model(sandbox, model_path: Path) -> None:
    reply = run(sandbox, ESCAPE + f"b['__import__']('os').remove(r'{model_path}')")
    assert reply["ok"] is False
    assert reply["kind"] == "violation"
    assert model_path.exists()


def test_escaped_code_cannot_list_directories_outside_the_roots(sandbox, tmp_path) -> None:
    reply = run(sandbox, ESCAPE + f"print(b['__import__']('os').listdir(r'{tmp_path.parent}'))")
    assert reply["ok"] is False
    assert reply["kind"] == "violation"


def test_the_environment_holds_no_credentials(sandbox) -> None:
    reply = run(sandbox, ESCAPE + "print(sorted(b['__import__']('os').environ))")
    assert reply["ok"] is True
    names = reply["stdout"].upper()
    for leaky in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL"):
        assert leaky not in names


@pytest.mark.parametrize(
    "label, tamper",
    [
        ("busy flag", "h._state.busy = True"),
        ("module busy flag", "setattr(h, '_state', type('s', (), {'busy': True})())"),
        ("deny prefixes", "h._DENIED_PREFIXES = ()"),
        ("deny exact", "h._DENIED_EXACT = frozenset()"),
    ],
)
def test_escaped_code_cannot_disarm_the_audit_hook(sandbox, label, tamper) -> None:
    """Module state is reachable once the namespace is escaped, so no check may
    depend on it staying honest."""
    code = ESCAPE + (
        "h = b['__import__']('sys').modules['ifc_console.sandbox.hooks']\n"
        f"try:\n    {tamper}\nexcept Exception:\n    pass\n"
        "b['__import__']('socket').socket()\n"
    )
    reply = run(sandbox, code)
    assert reply["ok"] is False, f"{label} disarmed the network block"
    assert reply["kind"] == "violation", f"{label}: {reply.get('message')}"


def test_a_forged_busy_flag_denies_file_access_rather_than_allowing_it(sandbox) -> None:
    secret = sandbox.denied_dir / "token"  # type: ignore[attr-defined]
    code = ESCAPE + (
        "h = b['__import__']('sys').modules['ifc_console.sandbox.hooks']\n"
        "try:\n    h._state.busy = True\nexcept Exception:\n    pass\n"
        f"print(b['open'](r'{secret}').read())\n"
    )
    reply = run(sandbox, code)
    assert reply["ok"] is False
    assert "SECRET-TOKEN" not in str(reply)


def test_the_scratch_stays_writable_when_it_sits_inside_a_denied_root(tmp_path, model_path) -> None:
    """The production layout: scratch lives under the console home, and the
    console home is denied wholesale."""
    home = tmp_path / "home"
    scratch = home / "sandbox" / "run"
    home.mkdir()
    (home / "token").write_text("SECRET-TOKEN")

    policy = SandboxPolicy.build(
        read_dirs=[model_path.parent],
        scratch_dir=scratch,
        deny_dirs=[home],
        memory_mb=1024,
    )
    process = SandboxProcess(policy, scratch)
    process.start(timeout=300)
    try:
        target = scratch / "ok.txt"
        reply = process.request(
            {
                "op": "run",
                "code": ESCAPE + f"b['open'](r'{target}', 'w').write('fine')",
                "output_limit": 4096,
            },
            timeout=60,
        )
        assert reply["ok"] is True, reply.get("message")
        assert target.read_text() == "fine"

        blocked = process.request(
            {
                "op": "run",
                "code": ESCAPE + f"print(b['open'](r'{home / 'token'}').read())",
                "output_limit": 4096,
            },
            timeout=60,
        )
        assert blocked["ok"] is False
        assert "SECRET-TOKEN" not in str(blocked)
    finally:
        process.terminate()


def test_the_scratch_directory_stays_writable(sandbox) -> None:
    target = sandbox.scratch_dir / "ok.txt"  # type: ignore[attr-defined]
    reply = run(sandbox, ESCAPE + f"b['open'](r'{target}', 'w').write('fine')")
    assert reply["ok"] is True, reply.get("message")
    assert target.read_text() == "fine"


# -- routing: which runs actually get sandboxed --------------------------------------
@pytest.mark.skipif(
    not secure_isolation_supported(),
    reason="secure sandbox isolation requires CPython 3.12 or newer",
)
async def test_ask_mode_queries_are_sandboxed(ask_harness) -> None:
    out = await ask_harness.call("execute_ifc_code", code="len(ifc.by_type('IfcWall'))")
    assert out["ok"] is True
    assert out["data"]["sandboxed"] is True
    assert out["data"]["result"] == "3"


async def test_runtime_without_thread_audit_falls_back_or_refuses(
    harness_factory, work_model, monkeypatch
) -> None:
    from ifc_console.sandbox import runner as sandbox_runner

    monkeypatch.setattr(sandbox_runner, "secure_isolation_supported", lambda: False)
    h = await harness_factory(model=work_model)

    fallback = await h.call("execute_ifc_code", code="len(ifc.by_type('IfcWall'))")
    assert fallback["ok"] is True
    assert fallback["data"]["sandboxed"] is False
    assert "CPython 3.12 or newer" in fallback["data"]["note"]

    h.core.settings.sandbox.mode = "strict"
    refused = await h.call("execute_ifc_code", code="len(ifc.by_type('IfcWall'))")
    assert refused["ok"] is False
    assert refused["error"]["code"] == "SANDBOX_UNAVAILABLE"
    assert "CPython 3.12 or newer" in refused["error"]["message"]


async def test_sandbox_blocks_project_credentials_but_allows_the_ifc(
    ask_harness, work_model: Path
) -> None:
    root = work_model.parent
    credentials = [
        root / ".npmrc",
        root / ".aws" / "credentials",
        root / ".git" / "config",
        root / ".envrc",
        root / ".env" / "secret.txt",
        root / "frontend" / ".npmrc",
        root / "packages" / "service" / ".aws" / "credentials",
    ]
    for target in credentials:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("PROJECT-SECRET", encoding="utf-8")

    for target in credentials:
        out = await ask_harness.call(
            "execute_ifc_code",
            code=f"open(r'{target}', encoding='utf-8').read()",
        )
        assert out["ok"] is False
        assert out["error"]["code"] == "EXEC_BLOCKED"
        assert "PROJECT-SECRET" not in str(out)

    allowed = await ask_harness.call(
        "execute_ifc_code",
        code=f"open(r'{work_model}', encoding='utf-8').read(7)",
    )
    assert allowed["ok"] is True
    assert allowed["data"]["result"] == "'ISO-103'"


async def test_mutations_run_in_process_so_the_edit_lands(harness_factory, work_model) -> None:
    from ifc_console.policy.modes import Mode

    h = await harness_factory(model=work_model, mode=Mode.EDIT)
    out = await h.call(
        "execute_ifc_code",
        code="ifc_api.run('root.create_entity', ifc, ifc_class='IfcWall')",
    )
    assert out["ok"] is True
    assert out["data"]["sandboxed"] is False
    assert out["data"]["mutated"] is True


async def test_unsaved_changes_fall_back_with_a_stated_reason(
    harness_factory, work_model, monkeypatch
) -> None:
    from ifc_console.policy.modes import Mode

    monkeypatch.setattr(
        "ifc_console.sandbox.runner.secure_isolation_supported", lambda: True
    )
    h = await harness_factory(model=work_model, mode=Mode.EDIT)
    await h.call(
        "execute_ifc_code",
        code="ifc_api.run('root.create_entity', ifc, ifc_class='IfcWall')",
    )
    # the sandbox copy comes from disk, which no longer matches memory
    out = await h.call("execute_ifc_code", code="len(ifc.by_type('IfcWall'))")
    assert out["ok"] is True
    assert out["data"]["sandboxed"] is False
    assert "unsaved changes" in out["data"]["note"]
    assert out["data"]["result"] == "4"  # the in-memory edit is visible


async def test_strict_mode_refuses_instead_of_falling_back(harness_factory, work_model) -> None:
    from ifc_console.policy.modes import Mode

    h = await harness_factory(model=work_model, mode=Mode.EDIT)
    h.core.settings.sandbox.mode = "strict"
    await h.call(
        "execute_ifc_code",
        code="ifc_api.run('root.create_entity', ifc, ifc_class='IfcWall')",
    )
    out = await h.call("execute_ifc_code", code="len(ifc.by_type('IfcWall'))")
    assert out["ok"] is False
    assert out["error"]["code"] == "SANDBOX_UNAVAILABLE"


async def test_sandbox_off_restores_the_in_process_path(harness_factory, work_model) -> None:
    h = await harness_factory(model=work_model)
    h.core.settings.sandbox.mode = "off"
    out = await h.call("execute_ifc_code", code="len(ifc.by_type('IfcWall'))")
    assert out["ok"] is True
    assert out["data"]["sandboxed"] is False
    assert "note" not in out["data"]


# -- recovery -----------------------------------------------------------------------
def test_a_runaway_run_is_killed_and_the_console_survives(tmp_path, model_path) -> None:
    """The in-process executor cannot kill a wedged thread; the sandbox can."""
    scratch = tmp_path / "scratch"
    policy = SandboxPolicy.build(
        read_dirs=[model_path.parent],
        scratch_dir=scratch,
        deny_dirs=[],
        memory_mb=1024,
    )
    process = SandboxProcess(policy, scratch)
    try:
        process.start(timeout=300)
        with pytest.raises(SandboxTimeout):
            process.request(
                {"op": "run", "code": "while True: pass", "output_limit": 100, "allowed_dirs": []},
                timeout=3,
            )
        assert not process.alive
    finally:
        process.terminate()
