from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any

import jsonschema
from openai import APIConnectionError, APIStatusError, AsyncOpenAI, RateLimitError


def build_schema_skeleton(schema: dict[str, Any]) -> Any:
    """Build a placeholder JSON instance matching the given JSON Schema.

    Used to show an LLM the exact structure (key names/types) it must emit.
    """

    def resolve(node: Any) -> dict[str, Any] | None:
        seen = 0
        while isinstance(node, dict) and "$ref" in node and seen < 10:
            ref = node["$ref"]
            current: Any = schema
            for part in ref.lstrip("#/").split("/"):
                if not isinstance(current, dict) or part not in current:
                    return None
                current = current[part]
            node = current
            seen += 1
        return node if isinstance(node, dict) else None

    def build(node: Any) -> Any:
        sub = resolve(node)
        if sub is None:
            return {}
        choices = sub.get("enum")
        if choices is None and "const" in sub:
            choices = [sub["const"]]
        if choices:
            return choices[0]
        node_type = sub.get("type")
        if node_type == "object" or "properties" in sub:
            result: dict[str, Any] = {}
            for key, value in (sub.get("properties") or {}).items():
                result[key] = build(value)
            return result
        if node_type == "array":
            items = sub.get("items")
            return [build(items)] if items else []
        if node_type == "string":
            return "..."
        if node_type in ("integer", "number"):
            return 0
        if node_type == "boolean":
            return False
        return {}

    return build(schema)


class BaseAgent:
    def __init__(self, system_prompt: str):
        self.system_prompt = system_prompt
        base_url = (
            os.environ.get("LLM_BASE_URL")
            or os.environ.get("DEEPSEEK_BASE_URL")
            or "https://api.deepseek.com"
        )
        api_key = (
            os.environ.get("DEEPSEEK_API_KEY")
            or os.environ.get("GEMINI_API_KEY")
            or os.environ.get("FIREWORKS_API_KEY", "")
        )
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.model = (
            os.environ.get("LLM_MODEL")
            or os.environ.get("DEEPSEEK_MODEL")
            or "deepseek-chat"
        )

    async def generate_json(
        self, user_prompt: str, schema: dict[str, Any] | None = None, retries: int = 3
    ) -> dict[str, Any]:
        messages: list[dict[str, str]] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
        }

        last_error: str | None = None
        last_content: str | None = None
        for _ in range(retries):
            response = await self._create_with_retry(dict(kwargs))
            content = response.choices[0].message.content or "{}"
            last_content = content
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError as exc:
                last_error = f"not valid JSON: {exc}"
                kwargs["messages"] = messages + [
                    {"role": "assistant", "content": content},
                    {
                        "role": "user",
                        "content": "The previous output was not valid JSON. "
                        "Return only a JSON object.",
                    },
                ]
                continue
            if schema is not None:
                validator = jsonschema.Draft202012Validator(schema)
                errors = sorted(validator.iter_errors(parsed), key=lambda e: e.path)
                if errors and self._prune_additional_properties(parsed, schema, schema):
                    errors = sorted(
                        validator.iter_errors(parsed), key=lambda e: e.path
                    )
                if errors and self._fix_pattern_errors(parsed, schema, schema):
                    errors = sorted(
                        validator.iter_errors(parsed), key=lambda e: e.path
                    )
                if errors:
                    last_error = "; ".join(
                        f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}"
                        for e in errors[:5]
                    )
                    kwargs["messages"] = messages + [
                        {"role": "assistant", "content": content},
                        {
                            "role": "user",
                            "content": (
                                "The previous output does not match the required schema: "
                                f"{errors[0].message}. Fix it and return valid JSON."
                            ),
                        },
                    ]
                    continue
            return parsed

        detail = f"\nlast content: {last_content[:800]}" if last_content else ""
        raise ValueError(
            "LLM failed to produce schema-valid JSON after retries: "
            + str(last_error)
            + detail
        )

    @staticmethod
    def _resolve_ref(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
        candidate: Any = schema
        seen = 0
        while isinstance(candidate, dict) and "$ref" in candidate and seen < 10:
            ref = candidate["$ref"]
            node: Any = root
            for part in ref.lstrip("#/").split("/"):
                if not isinstance(node, dict) or part not in node:
                    return {}
                node = node[part]
            candidate = node
            seen += 1
        return candidate if isinstance(candidate, dict) else {}

    @classmethod
    def _prune_additional_properties(
        cls, value: Any, schema: dict[str, Any], root: dict[str, Any]
    ) -> bool:
        changed = False
        if isinstance(value, dict):
            props = schema.get("properties")
            if isinstance(props, dict):
                for key in list(value):
                    if key not in props:
                        if schema.get("additionalProperties") is False:
                            del value[key]
                            changed = True
                        continue
                    sub = cls._resolve_ref(props[key], root)
                    if sub:
                        changed = cls._prune_additional_properties(
                            value[key], sub, root
                        ) or changed
        elif isinstance(value, list):
            items = schema.get("items")
            if items:
                sub = cls._resolve_ref(items, root)
                if sub:
                    for item in value:
                        changed = cls._prune_additional_properties(
                            item, sub, root
                        ) or changed
        return changed

    @staticmethod
    def _pattern_applies(pattern: str, value: str) -> bool:
        if "A-Z" not in pattern:
            return False
        try:
            return re.fullmatch(pattern, value) is None
        except re.error:
            return False

    @staticmethod
    def _capitalize_identifier(value: str) -> str:
        return value.upper().replace("-", "_").replace(" ", "_")

    @classmethod
    def _fix_pattern_errors(
        cls, value: Any, schema: dict[str, Any], root: dict[str, Any]
    ) -> bool:
        changed = False
        if isinstance(value, dict):
            props = schema.get("properties")
            if isinstance(props, dict):
                for key in list(value):
                    if key not in props:
                        continue
                    sub = cls._resolve_ref(props[key], root)
                    if not sub:
                        continue
                    pattern = sub.get("pattern")
                    if isinstance(pattern, str) and isinstance(value[key], str):
                        if cls._pattern_applies(pattern, value[key]):
                            value[key] = cls._capitalize_identifier(value[key])
                            changed = True
                    else:
                        changed = cls._fix_pattern_errors(
                            value[key], sub, root
                        ) or changed
        elif isinstance(value, list):
            items = schema.get("items")
            if items:
                sub = cls._resolve_ref(items, root)
                if sub:
                    pattern = sub.get("pattern")
                    for i, item in enumerate(value):
                        if isinstance(pattern, str) and isinstance(item, str):
                            if cls._pattern_applies(pattern, item):
                                value[i] = cls._capitalize_identifier(item)
                                changed = True
                        else:
                            changed = cls._fix_pattern_errors(
                                item, sub, root
                            ) or changed
        return changed

    async def _create_with_retry(self, kwargs: dict[str, Any]) -> Any:
        for attempt in range(4):
            try:
                return await self.client.chat.completions.create(**kwargs)
            except RateLimitError:
                await asyncio.sleep(min(4.0, 0.5 * (2**attempt)))
                if attempt == 3:
                    raise
            except APIConnectionError:
                await asyncio.sleep(0.5 * (attempt + 1))
                if attempt == 3:
                    raise
            except APIStatusError as exc:
                if exc.status_code < 500:
                    raise
                await asyncio.sleep(min(4.0, 0.5 * (2**attempt)))
                if attempt == 3:
                    raise
        raise RuntimeError("LLM request failed after retries")