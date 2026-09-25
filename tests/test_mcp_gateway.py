from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from student_agent.contracts import Contracts
from student_agent.mcp_gateway import EvidenceGateway

ROOT = Path(__file__).resolve().parents[1]


class FakeSession:
    def __init__(self, results: list[Any] | None = None) -> None:
        self.results = list(results or [])
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def list_tools(self) -> Any:
        return SimpleNamespace(
            tools=[
                SimpleNamespace(
                    name="get_order",
                    description="Get an order",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "case_id": {"type": "string"},
                            "order_id": {"type": "string"},
                        },
                        "required": ["case_id", "order_id"],
                        "additionalProperties": False,
                    },
                    output_schema={"type": "object"},
                )
            ]
        )

    async def call_tool(self, name: str, *, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        return self.results.pop(0)


def evidence(*, domain: str = "order") -> dict[str, Any]:
    return {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": "ev_order_aaaaaaaaaaaaaaaaaaaa",
        "result_hash": "sha256:" + "a" * 64,
        "domain": domain,
        "data": {"order_id": "ORD-001"},
    }


def result(value: dict[str, Any], *, is_error: bool = False, text: str = "") -> Any:
    return SimpleNamespace(
        is_error=is_error,
        structured_content=value if not is_error else None,
        content=[SimpleNamespace(text=text)] if is_error else [],
    )


def gateway(session: FakeSession, *, max_attempts: int = 1) -> EvidenceGateway:
    return EvidenceGateway(
        session,
        Contracts(ROOT / "contracts" / "schemas"),
        max_attempts=max_attempts,
    )


def test_gateway_discovers_typed_tool_catalog() -> None:
    discovered = asyncio.run(gateway(FakeSession()).discover_tools())

    assert len(discovered) == 1
    assert discovered[0].name == "get_order"
    assert discovered[0].input_schema["required"] == ["case_id", "order_id"]


def test_gateway_owns_case_scope_and_returns_evidence() -> None:
    session = FakeSession([result(evidence())])
    value = asyncio.run(
        gateway(session).call("get_order", case_id="L3A_CASE_001", order_id="ORD-001")
    )

    assert value == evidence()
    assert session.calls == [
        ("get_order", {"case_id": "L3A_CASE_001", "order_id": "ORD-001"})
    ]


def test_gateway_rejects_unexpected_arguments() -> None:
    session = FakeSession()
    with pytest.raises(ValueError, match="arguments"):
        asyncio.run(
            gateway(session).call(
                "get_order",
                case_id="L3A_CASE_001",
                order_id="ORD-001",
                extra="not-allowed",
            )
        )
    assert session.calls == []


def test_gateway_rejects_wrong_domain() -> None:
    session = FakeSession([result(evidence(domain="payment"))])
    with pytest.raises(ValueError, match="expected 'order'"):
        asyncio.run(
            gateway(session).call("get_order", case_id="L3A_CASE_001", order_id="ORD-001")
        )


def test_gateway_retries_only_retryable_tool_error() -> None:
    session = FakeSession(
        [
            result({}, is_error=True, text="service temporarily unavailable"),
            result(evidence()),
        ]
    )
    value = asyncio.run(
        gateway(session, max_attempts=2).call(
            "get_order", case_id="L3A_CASE_001", order_id="ORD-001"
        )
    )

    assert value == evidence()
    assert len(session.calls) == 2
