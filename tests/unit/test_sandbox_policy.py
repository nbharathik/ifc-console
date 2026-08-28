"""Policy construction, path containment, and the environment handed to the worker."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from ifc_console.sandbox import protocol
from ifc_console.sandbox.client import (
    _child_env,
    worker_command,
    worker_executable,
    worker_path,
)
from ifc_console.sandbox.hooks import _under
from ifc_console.sandbox.limits import ProcessJail, isolated_process_kwargs
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


def test_worker_bootstrap_keeps_dependency_paths_after_the_standard_library(
    tmp_path: Path, monkeypatch
) -> None:
    fake_site = tmp_path / "site-packages"
    fake_site.mkdir()
    for module in ("asyncio", "json"):
        (fake_site / f"{module}.py").write_text(
            f"raise RuntimeError('{module} was shadowed')\n", encoding="utf-8"
        )
    (fake_site / "bootstrap_probe.py").write_text(
        "import asyncio, json\n"
        "print(json.dumps({'asyncio': asyncio.__file__, 'json': json.__file__}))\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "ifc_console.sandbox.client.worker_path", lambda _narrow=True: [str(fake_site)]
    )

    completed = subprocess.run(
        worker_command("bootstrap_probe"),
        cwd=tmp_path,
        env=_child_env(tmp_path),
        capture_output=True,
        check=True,
        text=True,
        timeout=30,
    )
    imported = json.loads(completed.stdout)

    assert Path(imported["asyncio"]).resolve() != (fake_site / "asyncio.py").resolve()
    assert Path(imported["json"]).resolve() != (fake_site / "json.py").resolve()


def test_worker_path_is_narrower_than_sys_path() -> None:
    narrow = worker_path(narrow=True)
    assert narrow, "the worker still needs somewhere to import from"
    assert len(narrow) <= len([p for p in sys.path if p and os.path.isdir(p)])
    # ifc_console itself must remain importable or the worker cannot start
    assert any(os.path.isdir(os.path.join(p, "ifc_console")) for p in narrow)


def test_worker_executable_is_a_real_interpreter() -> None:
    assert os.path.isfile(worker_executable())


def test_posix_workers_start_in_an_isolated_session() -> None:
    expected = {"start_new_session": True} if os.name == "posix" else {}
    assert isolated_process_kwargs() == expected


def test_process_jail_never_kills_the_console_group(monkeypatch) -> None:
    from ifc_console.sandbox import limits

    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(limits.sys, "platform", "linux")
    monkeypatch.setattr(limits.os, "getpgid", lambda _pid: 42, raising=False)
    monkeypatch.setattr(limits.os, "getpgrp", lambda: 42, raising=False)
    monkeypatch.setattr(
        limits.os,
        "killpg",
        lambda group, signal: killed.append((group, signal)),
        raising=False,
    )

    jail = ProcessJail(0)
    jail.attach(123)
    jail.kill()

    assert killed == []


def test_process_jail_kills_only_the_worker_owned_group(monkeypatch) -> None:
    import signal

    from ifc_console.sandbox import limits

    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(limits.sys, "platform", "linux")
    monkeypatch.setattr(limits.os, "getpgrp", lambda: 42, raising=False)
    monkeypatch.setattr(limits.os, "killpg", lambda group, sig: killed.append((group, sig)), raising=False)
    monkeypatch.setattr(signal, "SIGKILL", 9, raising=False)

    jail = ProcessJail(0)
    jail.attach(123)
    monkeypatch.setattr(limits.os, "getpgid", lambda _pid: 777, raising=False)
    jail.kill()
    assert killed == []

    jail = ProcessJail(0)
    jail.attach(123)
    monkeypatch.setattr(limits.os, "getpgid", lambda _pid: 123, raising=False)
    jail.kill()
    assert killed == [(123, 9)]


def test_worst_case_configured_output_fits_one_protocol_frame() -> None:
    text = "\0" * protocol.MAX_EXEC_OUTPUT_CHARS
    encoded = protocol.encode({"ok": True, "stdout": text, "result": text})
    assert len(encoded) <= protocol.MAX_FRAME


def test_protocol_round_trips_lone_unicode_surrogates() -> None:
    encoded = protocol.encode({"stdout": "\ud800"})
    header_size = 8
    assert json.loads(encoded[header_size:].decode("utf-8"))["stdout"] == "\ud800"
