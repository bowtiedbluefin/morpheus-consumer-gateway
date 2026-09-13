import asyncio
import json
import time
from pathlib import Path

import openai
from openai import AsyncOpenAI

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "data/production-readiness"


async def main():
    results = []
    async with AsyncOpenAI(
        api_key=(ROOT / "secrets/production-test-api-key").read_text().strip(),
        base_url="http://localhost:8000/v1",
        max_retries=0,
        timeout=180,
    ) as c:

        def record(test, passed, **kw):
            d = {"test": test, "passed": passed, **kw}
            results.append(d)
            print(json.dumps(d), flush=True)
            (OUT / "live-sdk.json").write_text(
                json.dumps({"sdk_version": openai.__version__, "results": results}, indent=2)
            )

        listed = await c.models.list()
        record(
            "sdk_model_listing",
            any(x.id == "deepseek-v4-flash" for x in listed.data),
            models=[x.id for x in listed.data],
        )
        cases = [
            (
                "unicode_multiturn",
                {
                    "messages": [
                        {"role": "system", "content": "Answer concisely."},
                        {"role": "user", "content": "Remember this exact phrase: café 🐟 東京"},
                        {"role": "assistant", "content": "I will remember it."},
                        {"role": "user", "content": "Repeat only the exact phrase."},
                    ],
                    "max_tokens": 256,
                },
                lambda r: (
                    "café" in (r.choices[0].message.content or "")
                    and "東京" in (r.choices[0].message.content or "")
                ),
            ),
            (
                "json_response_format",
                {
                    "messages": [
                        {
                            "role": "user",
                            "content": "Return a JSON object with a single key ok whose value is true.",
                        }
                    ],
                    "response_format": {"type": "json_object"},
                    "max_tokens": 256,
                },
                lambda r: json.loads(r.choices[0].message.content)["ok"] is True,
            ),
            (
                "tool_calling",
                {
                    "messages": [
                        {"role": "user", "content": "What is the weather in Paris? Use the weather tool."}
                    ],
                    "tools": [
                        {
                            "type": "function",
                            "function": {
                                "name": "get_weather",
                                "description": "Get the current weather",
                                "parameters": {
                                    "type": "object",
                                    "properties": {"city": {"type": "string"}},
                                    "required": ["city"],
                                },
                            },
                        }
                    ],
                    "tool_choice": {"type": "function", "function": {"name": "get_weather"}},
                    "max_tokens": 512,
                },
                lambda r: (
                    bool(r.choices[0].message.tool_calls)
                    and r.choices[0].message.tool_calls[0].function.name == "get_weather"
                ),
            ),
        ]
        for name, options, check in cases:
            try:
                t = time.monotonic()
                r = await c.chat.completions.create(model="deepseek-v4-flash", **options)
                data = r.model_dump()
                (OUT / ("sdk-" + name + ".json")).write_text(json.dumps(data, indent=2))
                record(
                    "sdk_" + name,
                    check(r),
                    seconds=time.monotonic() - t,
                    finish_reason=r.choices[0].finish_reason,
                    returned_model=r.model,
                )
            except Exception as e:
                record(
                    "sdk_" + name, False, error_type=type(e).__name__, status=getattr(e, "status_code", None)
                )
        try:
            stream = await c.chat.completions.create(
                model="deepseek-v4-flash",
                messages=[{"role": "user", "content": "Reply exactly SDK_STREAM_OK."}],
                max_tokens=256,
                stream=True,
                stream_options={"include_usage": True},
            )
            text = ""
            usage = None
            count = 0
            async for chunk in stream:
                count += 1
                if chunk.usage:
                    usage = chunk.usage.model_dump()
                for choice in chunk.choices:
                    text += choice.delta.content or ""
            record(
                "sdk_stream_with_usage",
                "SDK_STREAM_OK" in text and count > 0,
                chunks=count,
                usage=usage,
                content=text,
            )
        except Exception as e:
            record(
                "sdk_stream_with_usage",
                False,
                error_type=type(e).__name__,
                status=getattr(e, "status_code", None),
            )
        try:
            await c.chat.completions.create(
                model="definitely-wrong-model", messages=[{"role": "user", "content": "Hi"}]
            )
            record("sdk_wrong_model_typed_error", False)
        except openai.NotFoundError as e:
            record("sdk_wrong_model_typed_error", e.status_code == 404, status=e.status_code)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Funded acceptance test; modifies local policy and creates/closes sessions. Read ../README.md first."
    )
    parser.add_argument("--allow-wallet-transactions", action="store_true")
    parser.add_argument("--allow-service-interruption", action="store_true")
    args = parser.parse_args()
    if not args.allow_wallet_transactions:
        parser.error("This test spends gas and locks MOR; explicitly pass --allow-wallet-transactions")
    (ROOT / "data/production-readiness").mkdir(parents=True, exist_ok=True)
    asyncio.run(main())
