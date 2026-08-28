"""ModelRegistry: one writable model, LRU eviction, dirty models pinned."""

from __future__ import annotations

import asyncio
import shutil
import threading
from pathlib import Path

import pytest

from ifc_console.mcp.envelope import ToolError
from ifc_console.session.model import ModelSession
from ifc_console.workspace.registry import Attachment, ModelRegistry

pytestmark = pytest.mark.asyncio


def _fake(name: str, size_mb: float = 1.0) -> ModelSession:
    """A session that owns no IfcOpenShell file; the registry never touches one."""
    session = ModelSession()
    session.path = Path(f"/models/{name}.ifc")
    session.size_bytes = int(size_mb * 1_048_576)
    session.fingerprint = name
    return session


async def test_idle_session_keeps_core_session_non_null() -> None:
    registry = ModelRegistry()
    assert registry.active.loaded is False
    assert registry.active_id is None
    registry.close_all()


async def test_active_is_the_only_writable_model() -> None:
    registry = ModelRegistry()
    registry.add("arch", _fake("arch"), active=True)
    registry.add("struct", _fake("struct"), active=False)
    assert registry.active.read_only is False
    assert registry.sessions["struct"].read_only is True

    registry.set_active("struct")
    assert registry.sessions["struct"].read_only is False
    assert registry.sessions["arch"].read_only is True
    registry.close_all()


async def test_read_only_session_refuses_to_save(tmp_path: Path, work_model: Path) -> None:
    from ifc_console.session.backups import BackupStore

    session = ModelSession()
    await session.open(work_model)
    session.read_only = True
    with pytest.raises(ToolError) as excinfo:
        await session.save(work_model, BackupStore(tmp_path / "backups", 5))
    assert excinfo.value.code == "MODEL_READ_ONLY"
    session.close()


async def test_ids_are_deduped() -> None:
    registry = ModelRegistry()
    first = registry.make_id(Path("/a/tower.ifc"))
    registry.add(first, _fake("tower"), active=True)
    second = registry.make_id(Path("/b/tower.ifc"))
    assert (first, second) == ("tower", "tower-2")
    registry.close_all()


async def test_make_room_evicts_least_recently_used_clean_model() -> None:
    registry = ModelRegistry(max_resident=2)
    registry.add("arch", _fake("arch"), active=True)
    registry.add("struct", _fake("struct"), active=False)
    registry.touch("arch")  # struct is now the least recently used

    evicted = registry.make_room(1_000)
    assert evicted == ["struct"]
    assert set(registry.sessions) == {"arch"}
    registry.close_all()


async def test_dirty_models_are_never_evicted() -> None:
    registry = ModelRegistry(max_resident=2)
    registry.add("arch", _fake("arch"), active=True)
    registry.add("struct", _fake("struct"), active=False)
    registry.sessions["struct"].dirty = True

    with pytest.raises(ToolError) as excinfo:
        registry.make_room(1_000)
    assert excinfo.value.code == "WORKSPACE_BUDGET"
    assert set(registry.sessions) == {"arch", "struct"}
    registry.close_all()


async def test_total_budget_refuses_an_oversized_newcomer() -> None:
    registry = ModelRegistry(max_resident=4, max_total_mb=10)
    registry.add("arch", _fake("arch", size_mb=6), active=True)
    with pytest.raises(ToolError) as excinfo:
        registry.make_room(8 * 1_048_576)
    assert excinfo.value.code == "WORKSPACE_BUDGET"
    assert "workspace.max_total_mb" in excinfo.value.hint
    registry.close_all()


async def test_failed_budget_plan_does_not_evict_existing_models() -> None:
    registry = ModelRegistry(max_resident=4, max_total_mb=10)
    registry.add("arch", _fake("arch", size_mb=6), active=True)
    registry.add("struct", _fake("struct", size_mb=3), active=False)

    with pytest.raises(ToolError) as excinfo:
        registry.plan_room(8 * 1_048_576)
    assert excinfo.value.code == "WORKSPACE_BUDGET"
    assert set(registry.sessions) == {"arch", "struct"}
    registry.close_all()


async def test_budget_plan_accounts_for_replaced_active_model() -> None:
    registry = ModelRegistry(max_resident=2, max_total_mb=10)
    registry.add("arch", _fake("arch", size_mb=6), active=True)
    registry.add("struct", _fake("struct", size_mb=3), active=False)
    assert registry.plan_room(8 * 1_048_576, replacing=("arch",)) == ["struct"]
    assert set(registry.sessions) == {"arch", "struct"}
    registry.close_all()


async def test_drop_refuses_dirty_unless_forced() -> None:
    registry = ModelRegistry()
    registry.add("arch", _fake("arch"), active=True)
    registry.sessions["arch"].dirty = True
    with pytest.raises(ToolError) as excinfo:
        registry.drop("arch")
    assert excinfo.value.code == "UNSAVED_CHANGES"
    registry.drop("arch", force=True)
    assert registry.sessions == {}
    registry.close_all()


async def test_cancelled_worker_fences_lifecycle_changes_until_recovery() -> None:
    registry = ModelRegistry()
    session = ModelSession()
    registry.add("arch", session, active=True)
    registry.add("struct", _fake("struct"), active=False)
    started = threading.Event()
    release = threading.Event()

    def blocking() -> None:
        started.set()
        release.wait(10)

    running = asyncio.create_task(session.run(blocking))
    assert await asyncio.to_thread(started.wait, 10)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    with pytest.raises(ToolError, match="still fenced") as switching:
        registry.set_active("struct")
    assert switching.value.code == "MODEL_BUSY"
    with pytest.raises(ToolError, match="still fenced") as dropping:
        registry.drop("arch", force=True)
    assert dropping.value.code == "MODEL_BUSY"

    release.set()
    await session.recover()
    registry.set_active("struct")
    registry.drop("arch", force=True)
    registry.close_all()


async def test_dropping_the_active_model_promotes_another() -> None:
    registry = ModelRegistry()
    registry.add("arch", _fake("arch"), active=True)
    registry.add("struct", _fake("struct"), active=False)
    registry.drop("arch")
    assert registry.active_id == "struct"
    assert registry.sessions["struct"].read_only is False
    registry.close_all()


async def test_unknown_model_id_names_what_exists() -> None:
    registry = ModelRegistry()
    registry.add("arch", _fake("arch"), active=True)
    with pytest.raises(ToolError) as excinfo:
        registry.require("nope")
    assert excinfo.value.code == "MODEL_NOT_FOUND"
    assert "arch" in excinfo.value.hint
    registry.close_all()


async def test_meta_extras_stay_quiet_for_a_single_model() -> None:
    registry = ModelRegistry()
    registry.add("arch", _fake("arch"), active=True)
    assert registry.meta_extras() == {"model_id": "arch"}

    registry.add("struct", _fake("struct"), active=False)
    registry.attach_file(Attachment(alias="spec", path=Path("/a/x.ids"), kind="ids"))
    assert registry.meta_extras() == {"model_id": "arch", "models": 2, "attachments": 1}
    registry.close_all()


async def test_models_and_attachments_share_one_id_namespace() -> None:
    registry = ModelRegistry()
    registry.attach_file(Attachment(alias="spec", path=Path("/a/x.ids"), kind="ids"))
    assert registry.make_id(Path("/models/spec.ifc")) == "spec-2"

    registry.add("tower", _fake("tower"), active=True)
    assert registry.unique_attachment_alias(Path("/a/tower.ids")) == "tower-2"
    registry.close_all()


async def test_concurrent_auto_attach_keeps_both_models(
    core, work_model: Path, tmp_path: Path
) -> None:
    # "auto" resolves under the lifecycle lock: two racing opens must never
    # both decide to replace, or the first model silently vanishes.
    second = tmp_path / "second.ifc"
    shutil.copy2(work_model, second)
    await asyncio.gather(
        core.open_model(work_model, attach="auto"),
        core.open_model(second, attach="auto"),
    )
    assert len(core.models.sessions) == 2
    assert core.models.active_id is not None
