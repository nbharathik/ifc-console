"""Shared fixtures. Isolates IFC_CONSOLE_HOME per test so nothing touches ~/.ifc-console."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
GENERATED = FIXTURES / "generated"
sys.path.insert(0, str(FIXTURES))


def _ensure_fixtures() -> None:
    if not (GENERATED / "minimal_ifc4.ifc").exists():
        import make_fixtures

        make_fixtures.main()


@pytest.fixture(scope="session", autouse=True)
def _fixtures() -> None:
    _ensure_fixtures()


@pytest.fixture(autouse=True)
def no_browser(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """No test may open a real browser tab.

    The /viewer, /chat, and /agent commands call webbrowser.open. Without this
    a full run launched one Chrome tab per command test. Tests that care about
    the URL assert against the returned list.
    """
    import webbrowser

    opened: list[str] = []

    def record(url: str, *args: object, **kwargs: object) -> bool:
        opened.append(url)
        return True

    monkeypatch.setattr(webbrowser, "open", record)
    monkeypatch.setattr(webbrowser, "open_new", record, raising=False)
    monkeypatch.setattr(webbrowser, "open_new_tab", record, raising=False)
    return opened


@pytest.fixture
def minimal_ifc4_path() -> Path:
    _ensure_fixtures()
    return GENERATED / "minimal_ifc4.ifc"


@pytest.fixture
def ifc4(minimal_ifc4_path: Path):
    import ifcopenshell

    return ifcopenshell.open(str(minimal_ifc4_path))


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "ifc-home"
    monkeypatch.setenv("IFC_CONSOLE_HOME", str(d))
    return d


@pytest.fixture
def work_model(tmp_path: Path, minimal_ifc4_path: Path) -> Path:
    """A writable copy of the fixture, so save/mutation tests don't touch the original."""
    dest = tmp_path / "work.ifc"
    shutil.copy2(minimal_ifc4_path, dest)
    return dest


@pytest.fixture
def core(home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """An AppCore rooted at an isolated home, cwd inside tmp_path."""
    monkeypatch.chdir(tmp_path)
    from ifc_console.app import AppCore
    from ifc_console.settings import SettingsStore

    store = SettingsStore(home=home, project_dir=tmp_path, env={})
    app = AppCore(store)
    try:
        yield app
    finally:
        app.shutdown()
