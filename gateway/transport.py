"""Bounded OpenAI chat validation and SSE decoding."""

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .node import GatewayError


class Message(BaseModel):
    model_config = ConfigDict(extra="allow")
    role: Literal["system", "developer", "user", "assistant", "tool", "function"]
    content: str | list[dict] | None = None
    tool_calls: list[dict] | None = None
    tool_call_id: str | None = None

    @model_validator(mode="after")
    def has_content(self):
        if self.content is None and not self.tool_calls and not self.model_extra.get("function_call"):
            raise ValueError("Message must contain content or tool calls")
        if self.role == "tool" and not self.tool_call_id:
            raise ValueError("Tool messages require tool_call_id")
        return self


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str = Field(min_length=1, max_length=100)
    messages: list[Message] = Field(min_length=1, max_length=10000)
    stream: bool = False
    temperature: float | None = Field(default=None, ge=0, le=2, allow_inf_nan=False)
    top_p: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    max_tokens: int | None = Field(default=None, gt=0, strict=True)
    max_completion_tokens: int | None = Field(default=None, gt=0, strict=True)
    tools: list[dict] | None = None
    stream_options: dict | None = None


def validate_completion(value, stream=False):
    expected = "chat.completion.chunk" if stream else "chat.completion"
    if (
        not isinstance(value, dict)
        or "error" in value
        or value.get("object") != expected
        or not isinstance(value.get("id"), str)
        or not isinstance(value.get("choices"), list)
    ):
        raise GatewayError("provider_protocol", "Provider returned an invalid chat response", 502)
    if not stream and not value["choices"]:
        raise GatewayError("provider_protocol", "Provider returned no completion choices", 502)
    for choice in value["choices"]:
        if not isinstance(choice, dict) or not isinstance(choice.get("delta" if stream else "message"), dict):
            raise GatewayError("provider_protocol", "Provider returned an invalid choice", 502)
    return value


async def bounded_body(response, limit):
    body = bytearray()
    async for chunk in response.aiter_bytes():
        if len(body) + len(chunk) > limit:
            raise GatewayError(
                "provider_response_too_large", "Provider response exceeded the configured limit", 502
            )
        body.extend(chunk)
    return bytes(body)


async def chat_events(response, limit):
    buffer = b""
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > limit or len(buffer) + len(chunk) > 2 * 1024 * 1024:
            raise GatewayError(
                "provider_response_too_large", "Provider stream exceeded the configured limit", 502
            )
        buffer = (buffer + chunk).replace(b"\r\n", b"\n")
        while b"\n\n" in buffer:
            frame, buffer = buffer.split(b"\n\n", 1)
            values = [line[5:].lstrip(b" ") for line in frame.split(b"\n") if line.startswith(b"data:")]
            if not values:
                continue
            value = b"\n".join(values)
            if value == b"[DONE]":
                yield b"data: [DONE]\n\n"
                return
            try:
                parsed = json.loads(value)
            except (ValueError, UnicodeError):
                raise GatewayError(
                    "provider_protocol", "Provider stream contained an invalid event", 502
                ) from None
            validate_completion(parsed, stream=True)
            yield b"data: " + json.dumps(parsed, separators=(",", ":")).encode() + b"\n\n"
    raise GatewayError("stream_interrupted", "Provider stream ended before completion", 502)
