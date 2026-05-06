"""Thin Anthropic API client. Reused across all AI features."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import httpx


ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


@dataclass
class AIResponse:
    text: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    stop_reason: str
    model: str


class AIClientError(Exception):
    """Raised on API failures."""


class AIClient:
    """
    Synchronous Anthropic Messages API client.

    Supports prompt caching via cache_control on system blocks.
    Use cacheable_system=True when the system prompt is large and reused
    across many calls (e.g., NL-to-SQL schema + examples).
    """

    def __init__(
        self,
        api_key: str,
        model: str = "claude-haiku-4-5",
        timeout: float = 30.0,
    ):
        if not api_key:
            raise ValueError("anthropic_api_key is required")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 1024,
        stop_sequences: Optional[list[str]] = None,
        temperature: float = 0.0,
        cacheable_system: bool = False,
    ) -> AIResponse:
        """
        Single-turn completion.

        If cacheable_system is True, the system prompt is marked with
        cache_control={"type": "ephemeral"} so Anthropic caches it and
        subsequent identical calls within ~5 minutes pay reduced cost
        on the cached portion.
        """
        if cacheable_system:
            system_payload: Any = [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        else:
            system_payload = system

        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system_payload,
            "messages": [{"role": "user", "content": user}],
            "temperature": temperature,
        }
        if stop_sequences:
            payload["stop_sequences"] = stop_sequences

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

        try:
            with httpx.Client(timeout=self.timeout) as client:
                r = client.post(ANTHROPIC_URL, json=payload, headers=headers)
                r.raise_for_status()
                data = r.json()
        except httpx.HTTPStatusError as e:
            raise AIClientError(
                f"Anthropic API returned {e.response.status_code}: "
                f"{e.response.text[:500]}"
            ) from e
        except httpx.RequestError as e:
            raise AIClientError(f"Anthropic API request failed: {e}") from e

        text = "".join(
            block["text"] for block in data["content"] if block["type"] == "text"
        )
        usage = data.get("usage", {})
        return AIResponse(
            text=text,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cache_read_tokens=usage.get("cache_read_input_tokens", 0),
            cache_creation_tokens=usage.get("cache_creation_input_tokens", 0),
            stop_reason=data.get("stop_reason", ""),
            model=data.get("model", self.model),
        )
