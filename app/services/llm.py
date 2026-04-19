from __future__ import annotations

import json
from json import JSONDecodeError
from typing import Any

import httpx
from openai import APIStatusError, AsyncOpenAI

from app.config import Settings
from app.schemas import LLMRuntimeConfig


def _extract_json_payload(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()

    try:
        payload = json.loads(cleaned)
        if not isinstance(payload, dict):
            raise ValueError("Model output is valid JSON but not a JSON object.")
        return payload
    except JSONDecodeError:
        fragment = _find_first_json_object(cleaned)
        if not fragment:
            raise ValueError("Model did not return a valid JSON object.") from None

    payload = json.loads(fragment)
    if not isinstance(payload, dict):
        raise ValueError("Model output is valid JSON but not a JSON object.")
    return payload


def _find_first_json_object(text: str) -> str | None:
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _json_only_system_prompt(system_prompt: str) -> str:
    suffix = "你必须只输出一个 JSON 对象，不要输出 Markdown 代码块，不要输出任何 JSON 之外的解释。"
    if suffix in system_prompt:
        return system_prompt
    return f"{system_prompt}\n\n{suffix}"


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") == "text" and isinstance(item.get("text"), str):
                    parts.append(item["text"])
        return "".join(parts)
    return ""


def _should_retry_without_response_format(exc: APIStatusError) -> bool:
    body = getattr(exc, "body", None)
    message = ""
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            message = str(error.get("message", ""))
        elif error is not None:
            message = str(error)
    if not message:
        message = str(exc)
    lowered = message.lower()
    markers = [
        "response_format",
        "json_object",
        "unknown parameter",
        "unsupported parameter",
        "not supported",
        "invalid parameter",
    ]
    return any(marker in lowered for marker in markers)


class OpenAICompatibleChatAdapter:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def complete_json(
        self,
        runtime: LLMRuntimeConfig,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.1,
    ) -> dict[str, Any]:
        http_client_kwargs: dict[str, Any] = {
            "timeout": self.settings.network_timeout_seconds,
            "trust_env": False,
        }
        if self.settings.preferred_proxy:
            http_client_kwargs["proxy"] = self.settings.preferred_proxy

        http_client = httpx.AsyncClient(**http_client_kwargs)
        client = AsyncOpenAI(
            api_key=runtime.api_key,
            base_url=runtime.base_url,
            http_client=http_client,
        )
        try:
            if runtime.json_mode != "prompt_only":
                try:
                    content = await self._create_completion(
                        client,
                        runtime=runtime,
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        temperature=temperature,
                        use_response_format=True,
                    )
                    return _extract_json_payload(content)
                except APIStatusError as exc:
                    if runtime.json_mode == "response_format" or not _should_retry_without_response_format(exc):
                        raise
                except ValueError:
                    if runtime.json_mode == "response_format":
                        raise

            content = await self._create_completion(
                client,
                runtime=runtime,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=temperature,
                use_response_format=False,
            )
            return _extract_json_payload(content)
        finally:
            await client.close()

    @staticmethod
    async def _create_completion(
        client: AsyncOpenAI,
        runtime: LLMRuntimeConfig,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        use_response_format: bool,
    ) -> str:
        request_payload: dict[str, Any] = {
            "model": runtime.model,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": _json_only_system_prompt(system_prompt)},
                {"role": "user", "content": user_prompt},
            ],
        }
        if use_response_format:
            request_payload["response_format"] = {"type": "json_object"}

        response = await client.chat.completions.create(**request_payload)
        return _content_to_text(response.choices[0].message.content) or "{}"


class LLMJsonClient:
    def __init__(self, settings: Settings):
        self._adapters = {
            "openai_compatible_chat": OpenAICompatibleChatAdapter(settings),
        }

    async def complete_json(
        self,
        runtime: LLMRuntimeConfig,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.1,
    ) -> dict[str, Any]:
        if not runtime.api_key:
            raise RuntimeError(f"Provider '{runtime.provider}' does not have a usable API key.")
        adapter = self._adapters.get(runtime.adapter)
        if not adapter:
            raise RuntimeError(f"Provider '{runtime.provider}' uses unsupported adapter '{runtime.adapter}'.")
        return await adapter.complete_json(runtime, system_prompt, user_prompt, temperature)
