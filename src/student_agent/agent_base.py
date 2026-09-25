from __future__ import annotations

import json
import os
from typing import Any

from openai import AsyncOpenAI


class BaseAgent:
    def __init__(self, system_prompt: str):
        self.system_prompt = system_prompt
        # Base url set to Fireworks AI
        self.client = AsyncOpenAI(
            api_key=os.environ.get("FIREWORKS_API_KEY", ""),
            base_url="https://api.fireworks.ai/inference/v1"
        )
        self.model = "accounts/fireworks/models/llama-v3p1-70b-instruct" # We can use llama 3.1 70b

    async def generate_json(self, user_prompt: str, schema: dict[str, Any] | None = None) -> dict[str, Any]:
        """Calls the LLM and returns parsed JSON output."""
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        
        # We enforce JSON output
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.0,
        }
        
        if schema:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "response_schema",
                    "schema": schema,
                    "strict": True
                }
            }
        else:
            kwargs["response_format"] = {"type": "json_object"}

        response = await self.client.chat.completions.create(**kwargs)
        content = response.choices[0].message.content or "{}"
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError(f"LLM returned invalid JSON: {content}") from exc
