from __future__ import annotations

import json
from typing import Any

from openai import AsyncOpenAI

from app.schemas import LLMRuntimeConfig


def _extract_json_payload(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    return json.loads(cleaned)


class LLMJsonClient:
    async def complete_json(
        self,
        runtime: LLMRuntimeConfig,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.1,
    ) -> dict[str, Any]:
        if not runtime.api_key:
            raise RuntimeError(f"Provider '{runtime.provider}' does not have a usable API key.")

        client_kwargs = {"api_key": runtime.api_key}
        if runtime.base_url:
            client_kwargs["base_url"] = runtime.base_url

        client = AsyncOpenAI(**client_kwargs)
        try:
            response = await client.chat.completions.create(
                model=runtime.model,
                temperature=temperature,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
        finally:
            await client.close()

        content = response.choices[0].message.content or "{}"
        return _extract_json_payload(content)

