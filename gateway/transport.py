"""Bounded OpenAI chat validation and SSE decoding."""

import json
import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .node import GatewayError


def valid_json_tree(value):
    """Bound extension payloads too, before any wallet or provider work."""
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > 64:
            raise ValueError("JSON nesting exceeds 64 levels")
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError("Non-finite JSON number")
        if isinstance(item, str):
            item.encode("utf-8")  # Reject unpaired escaped surrogates.
        elif isinstance(item, dict):
            pending.extend((v, depth + 1) for pair in item.items() for v in pair)
        elif isinstance(item, list):
            pending.extend((v, depth + 1) for v in item)
    return value


def strict_json(raw):
    def reject(value):
        raise ValueError("Non-finite JSON number")

    def integer(value):
        if len(value) > 256:
            raise ValueError("JSON integer exceeds supported precision")
        return int(value)

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON member")
            result[key] = value
        return result

    return valid_json_tree(json.loads(raw, parse_constant=reject, parse_int=integer, object_pairs_hook=pairs))


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
    try:
        valid_json_tree(value)
    except (ValueError, UnicodeError):
        raise GatewayError("provider_protocol", "Provider returned invalid JSON values", 502) from None
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
        message = choice["delta" if stream else "message"]
        if not stream and message.get("role") != "assistant":
            raise GatewayError("provider_protocol", "Provider returned an invalid assistant role", 502)
        if "role" in message and message["role"] != "assistant":
            raise GatewayError("provider_protocol", "Provider returned an invalid assistant role", 502)
        for field in ("content", "refusal", "reasoning_content", "reasoning"):
            if message.get(field) is not None and not isinstance(message[field], str):
                raise GatewayError("provider_protocol", "Provider returned invalid assistant text", 502)
        if not stream and not any(
            message.get(k) is not None
            for k in ("content", "refusal", "reasoning_content", "reasoning", "tool_calls", "function_call")
        ):
            raise GatewayError("provider_protocol", "Provider returned an empty assistant message", 502)
        if "index" in choice and (type(choice["index"]) is not int or choice["index"] < 0):
            raise GatewayError("provider_protocol", "Provider returned an invalid choice index", 502)
        if choice.get("finish_reason") is not None and not isinstance(choice["finish_reason"], str):
            raise GatewayError("provider_protocol", "Provider returned an invalid finish reason", 502)
        calls = message.get("tool_calls")
        if calls is not None:
            if not isinstance(calls, list):
                raise GatewayError("provider_protocol", "Provider returned invalid tool calls", 502)
            for call in calls:
                if not isinstance(call, dict):
                    raise GatewayError("provider_protocol", "Provider returned an invalid tool call", 502)
                if stream and (type(call.get("index")) is not int or call["index"] < 0):
                    raise GatewayError("provider_protocol", "Provider returned an invalid tool index", 502)
                if not stream and (not isinstance(call.get("id"), str) or call.get("type") != "function"):
                    raise GatewayError("provider_protocol", "Provider returned an invalid tool identity", 502)
                fn = call.get("function")
                if (not stream or fn is not None) and (
                    not isinstance(fn, dict)
                    or any(
                        not isinstance(fn.get(k), str) for k in ("name", "arguments") if not stream or k in fn
                    )
                ):
                    raise GatewayError("provider_protocol", "Provider returned an invalid function call", 502)
        fn = message.get("function_call")
        if fn is not None and (
            not isinstance(fn, dict)
            or any(not isinstance(fn.get(k), str) for k in ("name", "arguments") if not stream or k in fn)
        ):
            raise GatewayError("provider_protocol", "Provider returned an invalid function call", 502)
    usage = value.get("usage")
    if usage is not None:
        if not isinstance(usage, dict):
            raise GatewayError("provider_protocol", "Provider returned invalid usage", 502)
        for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
            if field in usage and (type(usage[field]) is not int or usage[field] < 0):
                raise GatewayError("provider_protocol", "Provider returned invalid token counts", 502)
    if stream and not value["choices"] and not isinstance(usage, dict):
        raise GatewayError("provider_protocol", "Provider returned an empty stream event", 502)
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
                parsed = strict_json(value)
            except (ValueError, UnicodeError, RecursionError):
                raise GatewayError(
                    "provider_protocol", "Provider stream contained an invalid event", 502
                ) from None
            validate_completion(parsed, stream=True)
            yield b"data: " + json.dumps(parsed, separators=(",", ":"), allow_nan=False).encode() + b"\n\n"
    raise GatewayError("stream_interrupted", "Provider stream ended before completion", 502)
