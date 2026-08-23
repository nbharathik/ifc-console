"""Keyring-backed credentials and the key resolution order."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from ifc_console import credentials
from ifc_console.mcp.envelope import ToolError


@pytest.fixture
def fake_keyring(monkeypatch):
    store: dict[tuple[str, str], str] = {}
    module = types.ModuleType("keyring")

    def set_password(service, name, value):
        store[(service, name)] = value

    def get_password(service, name):
        return store.get((service, name))

    def delete_password(service, name):
        store.pop((service, name), None)

    module.set_password = set_password
    module.get_password = get_password
    module.delete_password = delete_password
    errors = types.ModuleType("keyring.errors")
    errors.KeyringError = RuntimeError
    module.errors = errors
    monkeypatch.setitem(sys.modules, "keyring", module)
    monkeypatch.setitem(sys.modules, "keyring.errors", errors)
    return store


@pytest.fixture
def no_keyring(monkeypatch):
    monkeypatch.setitem(sys.modules, "keyring", None)


class TestStore:
    def test_round_trip_and_index(self, tmp_path: Path, fake_keyring):
        credentials.set_api_key(tmp_path, "openai", "  sk-test  ")
        assert credentials.get_api_key("openai") == "sk-test"
        assert credentials.stored_providers(tmp_path) == ["openai"]
        index = (tmp_path / "keys.json").read_text(encoding="utf-8")
        assert "sk-test" not in index  # the index never holds secrets

        assert credentials.delete_api_key(tmp_path, "openai") is True
        assert credentials.get_api_key("openai") is None
        assert credentials.stored_providers(tmp_path) == []

    def test_empty_key_is_rejected(self, tmp_path: Path, fake_keyring):
        with pytest.raises(ToolError) as excinfo:
            credentials.set_api_key(tmp_path, "openai", "   ")
        assert excinfo.value.code == "INVALID_INPUT"

    def test_without_the_extra_everything_degrades(self, tmp_path: Path, no_keyring):
        assert credentials.keyring_available() is False
        assert credentials.get_api_key("openai") is None
        assert credentials.stored_providers(tmp_path) == []
        with pytest.raises(ToolError) as excinfo:
            credentials.set_api_key(tmp_path, "openai", "sk-x")
        assert excinfo.value.code == "EXTRA_NOT_INSTALLED"
        assert "ifc-console[keys]" in excinfo.value.hint


class TestResolutionOrder:
    def test_supplied_beats_keyring_beats_env(self, tmp_path: Path, fake_keyring, monkeypatch):
        from ifc_console.chat.providers import PROVIDERS, key_source, resolve_key

        provider = PROVIDERS["openai"]
        monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
        assert resolve_key(provider) == "sk-env"
        assert key_source(provider) == "OPENAI_API_KEY"

        credentials.set_api_key(tmp_path, "openai", "sk-ring")
        assert resolve_key(provider) == "sk-ring"
        assert key_source(provider) == "keyring"

        assert resolve_key(provider, "sk-pasted") == "sk-pasted"
