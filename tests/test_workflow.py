from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case

ROOT = Path(__file__).resolve().parents[1]
DOMAINS = {
    "get_order": "order",
    "get_order_items": "item",
    "get_order_payments": "payment",
    "get_payment_timeline": "payment",
    "get_policy": "policy",
    "get_refund_timeline": "refund",
    "get_sellers": "seller",
    "get_shipment_summary": "shipment",
}


class FakeGateway:
    def __init__(self, scenarios: dict[str, dict[str, Any]]) -> None:
        self.scenarios = scenarios
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    async def list_tools(self) -> list[str]:
        return sorted(self.scenarios)

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((tool_name, case_id, arguments))
        scenario = self.scenarios[tool_name]
        suffix = "a" * max(20, 88 - len(tool_name) - len(case_id))
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_{tool_name}_{case_id}_{suffix}",
            "result_hash": "sha256:" + "a" * 64,
            "domain": scenario["domain"],
            "data": scenario["data"],
        }


def build_case(
    topics: list[str],
    *,
    case_id: str = "L3A_CASE_001",
    order_id: str = "ORD-001",
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "opened_at": "2018-01-10T12:00:00-03:00",
        "customer_request": {
            "language": "vi",
            "message": "Check the order",
            "claimed_order_id": order_id,
            "claims": [
                {"claim_id": f"claim-{index:02d}", "topic": topic}
                for index, topic in enumerate(topics, 1)
            ],
        },
        "policy_version": "EC_POLICY_V1",
    }


def policy(
    issue: str,
    *,
    action: str,
    refund: float,
    party_type: str = "platform",
    party_id: str | None = "PLATFORM-1",
    case_status: str = "action_required",
) -> dict[str, Any]:
    return {
        "domain": "policy",
        "data": {
            "policy_version": "EC_POLICY_V1",
            "currency": "BRL",
            "rules": {
                issue: {
                    "case_status": case_status,
                    "recommended_action": action,
                    "refund_brl": refund,
                    "responsible_parties": [
                        {"party_type": party_type, "party_id": party_id}
                    ],
                }
            },
        },
    }


def order_scenarios(
    *,
    status: str = "canceled",
    delivered_at: str | None = None,
    estimated_at: str = "2018-01-08T00:00:00-03:00",
    capture: float | None = 199.9,
    refund_events: list[dict[str, Any]] | None = None,
    shipping_limit: str = "2018-01-05T00:00:00-03:00",
    shipment_actor: str | None = None,
) -> dict[str, dict[str, Any]]:
    order = {
        "order_id": "ORD-001",
        "order_status": status,
        "order_purchase_timestamp": "2018-01-01T00:00:00-03:00",
        "order_approved_at": "2018-01-02T00:00:00-03:00",
        "order_delivered_carrier_date": delivered_at,
        "order_delivered_customer_date": delivered_at,
        "order_estimated_delivery_date": estimated_at,
    }
    timeline_events: list[dict[str, Any]] = []
    payment_rows: list[dict[str, Any]] = []
    if capture is not None:
        timeline_events.append(
            {
                "order_id": "ORD-001",
                "event_at": "2018-01-02T01:00:00-03:00",
                "event_type": "captured",
                "amount_brl": capture,
                "status": "confirmed",
                "payment_reference": "PAY-001",
            }
        )
        payment_rows.append(
            {
                "order_id": "ORD-001",
                "payment_sequential": "SEQ-001",
                "payment_reference": "PAY-001",
                "payment_type": "credit_card",
                "payment_value": capture,
            }
        )
    shipment_events: list[dict[str, Any]] = []
    if delivered_at is not None and shipment_actor is not None:
        shipment_events.append(
            {
                "order_id": "ORD-001",
                "event_at": delivered_at,
                "event_type": "delivered_late",
                "actor": shipment_actor,
            }
        )
    return {
        "get_order": {"domain": "order", "data": order},
        "get_order_items": {
            "domain": "item",
            "data": {
                "order_id": "ORD-001",
                "order_item_id": "ITEM-001",
                "seller_id": "SELLER-001",
                "price": 189.9,
                "freight_value": 10.0,
                "shipping_limit_date": shipping_limit,
            },
        },
        "get_payment_timeline": {
            "domain": "payment",
            "data": {"events": timeline_events},
        },
        "get_order_payments": {"domain": "payment", "data": payment_rows},
        "get_refund_timeline": {
            "domain": "refund",
            "data": {"events": refund_events or []},
        },
        "get_shipment_summary": {
            "domain": "shipment",
            "data": {
                "shipment_id": "SHIP-001",
                "order_status": status,
                "delivered_carrier_at": delivered_at,
                "delivered_customer_at": delivered_at,
                "estimated_delivery_at": estimated_at,
                "events": shipment_events,
            },
        },
        "get_sellers": {
            "domain": "seller",
            "data": [{"order_id": "ORD-001", "seller_id": "SELLER-001"}],
        },
        "get_policy": policy(
            "canceled_order_paid",
            action="issue_refund",
            refund=199.9,
        ),
    }


def run_case(
    case: dict[str, Any],
    scenarios: dict[str, dict[str, Any]],
    tmp_path: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]], FakeGateway]:
    contracts = Contracts(ROOT / "contracts" / "schemas")
    trace_path = tmp_path / "traces" / "trace.jsonl"
    trace = TraceWriter(trace_path, contracts)
    gateway = FakeGateway(scenarios)
    trace.emit(case_id=case["case_id"], event_type="case_received", actor="coordinator")
    output = asyncio.run(solve_case(case, gateway, trace))
    trace.emit(case_id=case["case_id"], event_type="case_finalized", actor="coordinator")
    contracts.validate_output(output, "test output")
    events = [
        json.loads(line)
        for line in trace_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return output, events, gateway


def test_canceled_paid_case_refunds_only_captured_evidence(tmp_path: Path) -> None:
    case = build_case(["canceled_order_paid", "requested_full_refund"])
    output, events, gateway = run_case(case, order_scenarios(), tmp_path)

    assert output["assessment"]["primary_issue"] == "canceled_order_paid"
    assert output["financial_resolution"]["recommended_refund_brl"] == 199.9
    assert output["affected_entities"]["payment_references"] == ["PAY-001"]
    assert output["resolution_actions"] == ["issue_refund"]
    assert {item["verdict"] for item in output["claim_assessments"]} == {"supported"}
    assert any(call[0] == "get_order" for call in gateway.calls)

    event_types = {event["event_type"] for event in events}
    assert {
        "case_received",
        "task_assigned",
        "handoff",
        "policy_decided",
        "verification_completed",
        "case_finalized",
    } <= event_types
    traced_refs = {
        ref for event in events for ref in event.get("evidence_refs", [])
    }
    assert set(output["evidence_refs"]) <= traced_refs


def test_late_delivery_scopes_seller_and_shipment(tmp_path: Path) -> None:
    scenarios = order_scenarios(
        status="delivered",
        delivered_at="2018-01-09T00:00:00-03:00",
        capture=10.0,
        shipment_actor="seller",
    )
    scenarios["get_order_items"]["data"]["price"] = 0.0
    scenarios["get_order_payments"]["data"][0]["payment_value"] = 10.0
    scenarios["get_policy"] = policy(
        "late_delivery_seller",
        action="refund_freight",
        refund=10.0,
        party_type="seller",
        party_id="UNVERIFIED-POLICY-SELLER",
    )
    output, _, _ = run_case(
        build_case(["late_delivery_seller", "requested_full_refund"]),
        scenarios,
        tmp_path,
    )

    assert output["assessment"]["primary_issue"] == "late_delivery_seller"
    assert output["financial_resolution"]["recommended_refund_brl"] == 10.0
    assert output["affected_entities"]["seller_ids"] == ["SELLER-001"]
    assert output["affected_entities"]["shipment_ids"] == ["SHIP-001"]
    assert output["root_cause_analysis"]["responsible_parties"] == [
        {"party_type": "seller", "party_id": "SELLER-001"}
    ]


def test_completed_refund_does_not_recommend_another_refund(tmp_path: Path) -> None:
    scenarios = order_scenarios(
        refund_events=[
            {
                "order_id": "ORD-001",
                "event_at": "2018-01-03T01:00:00-03:00",
                "event_type": "refund_completed",
                "amount_brl": 199.9,
                "status": "completed",
                "payment_reference": "PAY-001",
            }
        ]
    )
    scenarios["get_policy"] = policy(
        "unsupported_claim",
        action="document_no_action",
        refund=0.0,
        case_status="no_action",
    )
    output, _, _ = run_case(
        build_case(["canceled_order_paid", "requested_full_refund"]),
        scenarios,
        tmp_path,
    )

    assert output["assessment"]["primary_issue"] == "unsupported_claim"
    assert output["financial_resolution"]["recommended_refund_brl"] == 0.0
    assert output["resolution_actions"] == ["document_no_action"]


def test_no_discovered_tools_produces_honest_insufficient_output(tmp_path: Path) -> None:
    output, events, _ = run_case(
        build_case(["payment_mismatch", "requested_full_refund"]),
        {},
        tmp_path,
    )

    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert output["assessment"]["confidence"] == 0.2
    assert output["evidence_refs"] == []
    assert output["financial_resolution"]["recommended_refund_brl"] == 0.0
    assert any(
        event["event_type"] == "verification_completed" for event in events
    )
