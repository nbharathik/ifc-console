"""Agent conveniences exposed through the core runtime for one compatibility release."""

from __future__ import annotations

from pathlib import Path

import pytest
from ifc_console.runtime import LocalRuntime


class _AnswerModel:
    provider_id = "test"
    model_id = "answer"

    async def stream(self, **_kwargs):
        yield {"type": "content", "text": "Reviewed."}


@pytest.mark.asyncio
async def test_runtime_builds_agent_over_core_tools(
    tmp_path: Path,
    minimal_ifc4_path: Path,
):
    async with await LocalRuntime.open(
        minimal_ifc4_path,
        home=tmp_path / "home",
    ) as runtime:
        agent = await runtime.create_agent(
            name="reviewer",
            model=_AnswerModel(),
            instructions="Review the model.",
            tool_profile="inspect",
        )
        result = await agent.run("Review it")

        assert result.text == "Reviewed."
        assert "get_viewer_selection" in agent.tools
        assert "execute_ifc_code" not in agent.tools
