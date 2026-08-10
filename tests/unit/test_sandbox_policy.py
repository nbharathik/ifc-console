"""Policy construction, path containment, and the environment handed to the worker."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from ifc_console.sandbox import protocol
from ifc_console.sandbox.client import _child_env, worker_executable, worker_path
from ifc_console.sandbox.hooks import _under
from ifc_console.sandbox.policy import SandboxPolicy, runtime_roots


def test_build_normalises_and_separates_read_from_write(tmp_path: Path) -> None:
    models = tmp_path / "models"
    scratch = tmp_path / "scratch"
    home = tmp_path / "home"
    for d in (models, scratch, home):
        d.mkdir()

    policy = SandboxPolicy.build(
        read_dirs=[models], scratch_dir=scratch, deny_dirs=[home], memory_mb=512
    )
    assert os.path.normcase(str(models.resolve())) in policy.read_roots
    # the scratch is readable as well as writable; nothing else is writable
    assert os.path.normcase(str(scratch.resolve())) in policy.read_roots
    assert policy.write_roots == (os.path.normcase(str(scratch.resolve())),)
    assert policy.deny_roots == (os.path.normcase(str(home.resolve())),)
    assert policy.allow_network is False
    assert policy.allow_process is False


def test_policy_round_trips_over_the_wire(tmp_path: Path) -> None:
    policy = SandboxPolicy.build(
        read_dirs=[tmp_path], scratch_dir=tmp_path / "s", deny_dirs=[], memory_mb=99
    )
    assert SandboxPolicy.from_dict(policy.to_dict()) == policy


def test_under_matches_only_real_containment() -> None:
    root = os.path.normcase(os.path.abspath("/a/b"))
    assert _under(os.path.normcase(os.path.abspath("/a/b")), (root,))
    assert _under(os.path.normcase(os.path.abspath("/a/b/c.txt")), (root,))
    # a sibling whose name merely starts with the root is not inside it
    assert not _under(os.path.normcase(os.path.abspath("/a/bc")), (root,))
    assert not _under(os.path.normcase(os.path.abspath("/a")), (root,))


def test_runtime_roots_cover_the_interpreter() -> None:
    roots = runtime_roots()
    assert os.path.normcase(os.path.realpath(sys.prefix)) in roots


# -- the environment is the anti-exfiltration story ------------------------------
def test_child_env_carries_no_credentials(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    monkeypatch.setenv("IFC_CONSOLE_HOME", str(tmp_path / "home"))

    env = _child_env(tmp_path)

    assert "ANTHROPIC_API_KEY" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "IFC_CONSOLE_HOME" not in env
    assert not any("secret" in v.lower() for v in env.values())
    # temp files land in the scratch, not the user's temp directory
    assert env["TEMP"] == str(tmp_path)


def test_child_env_keeps_only_what_the_interpreter_needs(tmp_path: Path) -> None:
    env = _child_env(tmp_path)
    allowed = {
        "PYTHONPATH",
        "PYTHONNOUSERSITE",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONUNBUFFERED",
        "PYTHONSAFEPATH",
        "PYTHONUTF8",
        "TMPDIR",
        "TEMP",
        "TMP",
        "PATH",
        "SYSTEMROOT",
        "SystemRoot",
        "SystemDrive",
        "PATHEXT",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
        "HOME",
        "LANG",
        "LC_ALL",
    }
    assert set(env) <= allowed


def test_worker_path_is_narrower_than_sys_path() -> None:
    narrow = worker_path(narrow=True)
    assert narrow, "the worker still needs somewhere to import from"
    assert len(narrow) <= len([p for p in sys.path if p and os.path.isdir(p)])
    # ifc_console itself must remain importable or the worker cannot start
    assert any(os.path.isdir(os.path.join(p, "ifc_console")) for p in narrow)


def test_worker_executable_is_a_real_interpreter() -> None:
    assert os.path.isfile(worker_executable())


def test_worst_case_configured_output_fits_one_protocol_frame() -> None:
    text = "\0" * protocol.MAX_EXEC_OUTPUT_CHARS
    encoded = protocol.encode({"ok": True, "stdout": text, "result": text})
    assert len(encoded) <= protocol.MAX_FRAME


def test_protocol_round_trips_lone_unicode_surrogates() -> None:
    encoded = protocol.encode({"stdout": "\ud800"})
    header_size = 8
    assert json.loads(encoded[header_size:].decode("utf-8"))["stdout"] == "\ud800"
