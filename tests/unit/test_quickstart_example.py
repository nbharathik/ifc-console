"""Keep the quickstart example importable against the public SDK."""

from __future__ import annotations

import inspect

from examples.sdk import quickstart_agent


def test_quickstart_example_uses_the_public_sdk():
    assert inspect.iscoroutinefunction(quickstart_agent.run)
    source = inspect.getsource(quickstart_agent)
    assert "from ifc_console import" in source
    assert "set_mode" not in source
