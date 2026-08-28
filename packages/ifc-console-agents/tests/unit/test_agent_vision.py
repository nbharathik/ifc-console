"""Vision in the agent path: image input, image tool results, adapters."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from ifc_console.core.operations import OperationAnnotations
from ifc_console.toolsets import FunctionToolSource, Toolset

from ifc_console_agents.agent import Agent
from ifc_console_agents.chat.providers import to_anthropic_messages, to_openai_messages
from ifc_console_agents.models import AgentImage
from ifc_console_agents.testing import ScriptedAgentModel, ok_envelope, text_round, tool_call_round

READ_ONLY = OperationAnnotations(readOnlyHint=True, destructiveHint=False)
PIXEL = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode("ascii")


def image_turn(text: str = "look") -> dict:
    return {
        "role": "user",
        "text": text,
        "images": [{"media_type": "image/png", "data": PIXEL}],
    }


class TestAgentImage:
    def test_from_file_maps_jpg_to_jpeg(self, tmp_path: Path):
        target = tmp_path / "sketch.jpg"
        target.write_bytes(b"\xff\xd8\xffdata")
        image = AgentImage.from_file(target)
        assert image.media_type == "image/jpeg"
        assert base64.b64decode(image.data) == b"\xff\xd8\xffdata"

    def test_unknown_media_type_is_rejected(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            AgentImage(media_type="image/tiff", data=PIXEL)


class TestAdapters:
    def test_openai_user_images_become_data_urls(self):
        out = to_openai_messages("sys", [image_turn()])
        content = out[-1]["content"]
        assert content[0] == {"type": "text", "text": "look"}
        assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")

    def test_openai_plain_turns_stay_strings(self):
        out = to_openai_messages("sys", [{"role": "user", "text": "hi", "images": []}])
        assert out[-1]["content"] == "hi"

    def test_anthropic_images_fold_into_the_open_user_turn(self):
        """Tool results and the image message must land in ONE user turn:
        Anthropic requires strict role alternation."""
        turns = [
            {"role": "tool", "tool_call_id": "c1", "text": "{}"},
            image_turn("[image content from get_viewer_screenshot]"),
        ]
        out = to_anthropic_messages(turns)
        assert len(out) == 1
        blocks = out[0]["content"]
        types = [block["type"] for block in blocks]
        assert types == ["tool_result", "image", "text"]
        assert blocks[1]["source"]["media_type"] == "image/png"

    def test_anthropic_standalone_image_message(self):
        out = to_anthropic_messages([image_turn()])
        assert out[0]["role"] == "user"
        assert [b["type"] for b in out[0]["content"]] == ["image", "text"]


async def build_image_tools() -> Toolset:
    source = FunctionToolSource(namespace="cam")

    @source.tool(annotations=READ_ONLY)
    async def snap(view: str = "iso") -> dict:
        return ok_envelope(
            {"images": [{"media_type": "image/png", "data": PIXEL}], "note": "80x60"}
        )

    @source.tool(annotations=READ_ONLY)
    async def peek() -> dict:
        return ok_envelope({"plain": True})

    return await Toolset.build(source)


class TestToolResultImages:
    async def test_images_become_vision_input_after_the_round(self):
        tools = await build_image_tools()
        model = ScriptedAgentModel(
            [
                tool_call_round({"name": "cam__snap", "arguments": "{}"}),
                text_round("seen"),
            ]
        )
        agent = Agent(name="t", model=model, tools=tools, instructions="i")
        result = await agent.run("show me")

        record = result.tool_calls[0]
        assert record.result["data"]["images"] == 1  # slimmed to a count
        roles = [(m.role, bool(m.images)) for m in result.messages]
        tool_index = roles.index(("tool", False))
        assert roles[tool_index + 1] == ("user", True)
        follow_up = result.messages[tool_index + 1]
        assert "cam__snap" in follow_up.text
        assert follow_up.images[0].data == PIXEL

    async def test_parallel_round_attaches_images_after_all_tool_replies(self):
        tools = await build_image_tools()
        model = ScriptedAgentModel(
            [
                tool_call_round(
                    {"name": "cam__snap", "arguments": "{}"},
                    {"name": "cam__peek", "arguments": "{}"},
                ),
                text_round("done"),
            ]
        )
        agent = Agent(name="t", model=model, tools=tools, instructions="i")
        result = await agent.run("go")
        roles = [m.role for m in result.messages]
        # user, assistant(calls), tool, tool, user(image), assistant
        assert roles == ["user", "assistant", "tool", "tool", "user", "assistant"]
        assert result.messages[4].images

    async def test_plain_results_are_untouched(self):
        tools = await build_image_tools()
        model = ScriptedAgentModel(
            [tool_call_round({"name": "cam__peek", "arguments": "{}"}), text_round("ok")]
        )
        agent = Agent(name="t", model=model, tools=tools, instructions="i")
        result = await agent.run("go")
        assert result.tool_calls[0].result["data"] == {"plain": True}
        assert all(not m.images for m in result.messages if m.role == "user")


class TestRunImages:
    async def test_prompt_images_ride_the_first_user_message(self):
        tools = await build_image_tools()
        model = ScriptedAgentModel([text_round("a wall detail")])
        agent = Agent(name="t", model=model, tools=tools, instructions="i")
        image = AgentImage(media_type="image/png", data=PIXEL)
        result = await agent.run("what is this drawing?", images=[image])
        assert result.text == "a wall detail"
        sent = model.turns[0]["messages"][-1]
        assert sent.images == (image,)


class TestTransportEnvelope:
    def test_operation_image_content_converts(self):
        from ifc_console.core.operations import OperationImage
        from ifc_console.sdk import _transport_envelope

        result = _transport_envelope(
            [OperationImage(data=b"\x89PNG", format="png"), "a note"], {"m": 1}
        )
        assert result is not None and result.ok
        image = result.data["images"][0]
        assert image["media_type"] == "image/png"
        assert base64.b64decode(image["data"]) == b"\x89PNG"
        assert result.data["note"] == "a note"
        assert result.meta["transport"] == "content"

    def test_unknown_content_stays_invalid(self):
        from ifc_console.sdk import _transport_envelope

        assert _transport_envelope([object()], {}) is None
        assert _transport_envelope("just text", {}) is None
