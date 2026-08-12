"""Built-in model adapter over IFC Console's provider-neutral chat transport."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from ifc_console.agents.models import AgentMessage
from ifc_console.chat.providers import (
    PROVIDERS,
    ProviderError,
    astream,
    resolve_key,
    validate_base_url,
)
from ifc_console.toolsets import ToolDefinition


class ProviderModel:
    """OpenAI, Anthropic, OpenRouter, or an OpenAI-compatible local model."""

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        local_only: bool = False,
        timeout_s: float = 300.0,
        options: Mapping[str, Any] | None = None,
    ) -> None:
        try:
            self.provider = PROVIDERS[provider.lower()]
        except KeyError:
            raise ValueError(
                f"unknown provider {provider!r}; choose one of {sorted(PROVIDERS)}"
            ) from None
        self.provider_id = self.provider.id
        self.model_id = model.strip()
        if not self.model_id:
            raise ValueError("model must not be empty")
        self.api_key = api_key
        self.base_url = base_url
        self.local_only = local_only
        self.timeout_s = timeout_s
        self.options = dict(options or {})

    async def stream(
        self,
        *,
        messages: Sequence[AgentMessage],
        tools: Sequence[ToolDefinition],
        system: str,
        options: Mapping[str, Any],
    ) -> AsyncIterator[Mapping[str, Any]]:
        try:
            key = resolve_key(self.provider, self.api_key)
            if self.provider.needs_key and not key:
                variables = " or ".join(self.provider.key_env) or "an API key"
                raise ProviderError(f"no API key for {self.provider.label}; set {variables}")
            base = validate_base_url(
                self.base_url or self.provider.base_url,
                local_only=self.local_only,
            )
            merged_options = {**self.options, **dict(options)}
            turns = [message.model_dump(exclude_none=True) for message in messages]
            async for event in astream(
                self.provider,
                base_url=base,
                key=key,
                model=self.model_id,
                system=system,
                turns=turns,
                tools=[tool.provider_schema() for tool in tools],
                options=merged_options,
                timeout=self.timeout_s,
                local_only=self.local_only,
            ):
                yield event
        except ProviderError as exc:
            yield {"type": "error", "text": str(exc)}


__all__ = ["ProviderModel"]
