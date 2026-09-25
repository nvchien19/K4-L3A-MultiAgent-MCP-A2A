from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx2
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

from .contracts import Contracts

EXPECTED_DOMAINS = {
    "get_customer_history": "customer",
    "get_order": "order",
    "get_order_items": "item",
    "get_order_payments": "payment",
    "get_payment_timeline": "payment",
    "get_policy": "policy",
    "get_product_context": "product",
    "get_refund_timeline": "refund",
    "get_sellers": "seller",
    "get_shipment_summary": "shipment",
}


class GatewayError(RuntimeError):
    def __init__(self, tool_name: str, code: str, message: str, retryable: bool) -> None:
        super().__init__(f"MCP tool {tool_name} failed [{code}]: {message}")
        self.tool_name = tool_name
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
        }


class EvidenceGateway:
    def __init__(
        self, session: ClientSession, contracts: Contracts, *, max_attempts: int = 3
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self._session = session
        self._contracts = contracts
        self._max_attempts = max_attempts
        self._tools: dict[str, ToolDefinition] = {}

    async def discover_tools(self) -> list[ToolDefinition]:
        response = await self._session.list_tools()
        self._tools = {
            tool.name: ToolDefinition(
                name=tool.name,
                description=tool.description or "",
                input_schema=dict(tool.input_schema or {}),
                output_schema=dict(tool.output_schema or {}),
            )
            for tool in response.tools
        }
        return sorted(self._tools.values(), key=lambda tool: tool.name)

    async def list_tools(self) -> list[str]:
        return [tool.name for tool in await self.discover_tools()]

    async def call(
        self, tool_name: str, *, case_id: str, **arguments: Any
    ) -> dict[str, Any]:
        if not case_id:
            raise ValueError("case_id must not be empty")
        if not self._tools:
            await self.discover_tools()
        tool = self._tools.get(tool_name)
        if tool is None:
            raise GatewayError(tool_name, "unknown_tool", "tool was not discovered", False)
        if "case_id" in arguments:
            raise ValueError("case_id is owned by EvidenceGateway and cannot be overridden")
        payload = dict(arguments)
        payload["case_id"] = case_id
        self._validate_arguments(tool, payload)
        result = await self._call_with_retry(tool_name, payload)
        evidence = self._extract_evidence(tool_name, result)
        self._contracts.validate_evidence(evidence, f"MCP tool {tool_name}")
        self._validate_evidence(tool_name, evidence)
        return evidence

    def _validate_arguments(self, tool: ToolDefinition, payload: dict[str, Any]) -> None:
        try:
            errors = sorted(
                Draft202012Validator(tool.input_schema).iter_errors(payload),
                key=lambda error: list(error.absolute_path),
            )
        except (SchemaError, TypeError, ValueError) as exc:
            raise GatewayError(
                tool.name, "invalid_tool_schema", "invalid argument schema", False
            ) from exc
        if errors:
            error = errors[0]
            location = ".".join(str(part) for part in error.absolute_path) or "$"
            raise ValueError(f"MCP tool {tool.name} arguments at {location}: {error.message}")

    async def _call_with_retry(self, tool_name: str, payload: dict[str, Any]) -> Any:
        last_error: GatewayError | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                result = await asyncio.wait_for(
                    self._session.call_tool(tool_name, arguments=payload), timeout=60.0
                )
                if getattr(result, "is_error", getattr(result, "isError", False)):
                    message = self._result_message(result) or "unknown tool error"
                    last_error = self._classify_error(tool_name, message)
                else:
                    return result
            except (TimeoutError, ConnectionError, httpx2.TransportError, MCPError) as exc:
                last_error = self._classify_error(tool_name, str(exc))
            if attempt < self._max_attempts and last_error.retryable:
                await asyncio.sleep(0.5 * attempt)
        if last_error is None:
            raise GatewayError(tool_name, "tool_error", "unknown tool error", False)
        raise last_error

    @staticmethod
    def _result_message(result: Any) -> str:
        return " ".join(
            str(block.text)
            for block in getattr(result, "content", [])
            if getattr(block, "text", None)
        )

    @staticmethod
    def _classify_error(tool_name: str, message: str) -> GatewayError:
        normalized = message.casefold()
        not_found = ("not found", "not_found", "no rows", "unknown order", "unknown payment")
        transient = ("timeout", "timed out", "temporarily", "unavailable", "connection")
        invalid = ("invalid", "validation", "malformed", "schema")
        if any(token in normalized for token in not_found):
            return GatewayError(tool_name, "not_found", message, False)
        if any(token in normalized for token in transient):
            return GatewayError(tool_name, "transient", message, True)
        if any(token in normalized for token in invalid):
            return GatewayError(tool_name, "invalid_response", message, False)
        return GatewayError(tool_name, "tool_error", message, False)

    @staticmethod
    def _extract_evidence(tool_name: str, result: Any) -> dict[str, Any]:
        evidence = getattr(result, "structured_content", None)
        if evidence is None:
            evidence = getattr(result, "structuredContent", None)
        if evidence is None:
            text_blocks = [
                str(block.text)
                for block in getattr(result, "content", [])
                if getattr(block, "text", None)
            ]
            if len(text_blocks) != 1:
                raise ValueError(f"MCP tool {tool_name} did not return one evidence object")
            try:
                evidence = json.loads(text_blocks[0])
            except json.JSONDecodeError as exc:
                raise ValueError(f"MCP tool {tool_name} returned invalid JSON") from exc
        if not isinstance(evidence, dict):
            raise ValueError(f"MCP tool {tool_name} returned a non-object evidence envelope")
        return evidence

    def _validate_evidence(self, tool_name: str, evidence: dict[str, Any]) -> None:
        expected_domain = EXPECTED_DOMAINS.get(tool_name)
        if expected_domain is not None and evidence["domain"] != expected_domain:
            raise ValueError(
                f"MCP tool {tool_name} returned domain {evidence['domain']!r}, "
                f"expected {expected_domain!r}"
            )


@asynccontextmanager
async def connect_gateway(
    endpoint: str, team_api_key: str, contracts: Contracts
) -> AsyncIterator[EvidenceGateway]:
    headers = {"Authorization": f"Bearer {team_api_key}"}
    timeout = httpx2.Timeout(300.0, connect=30.0, write=30.0, pool=30.0)
    async with (
        httpx2.AsyncClient(headers=headers, timeout=timeout) as http_client,
        streamable_http_client(endpoint, http_client=http_client) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        yield EvidenceGateway(session, contracts)
