from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from student_agent import rules
from student_agent.agents import CaseContext, PolicyDecision, apply_policy, policy_rule
from student_agent.contracts import Contracts
from student_agent.evidence import EvidenceLedger
from student_agent.mcp_gateway import EvidenceGateway
from student_agent.trace import TraceWriter
from student_agent.verifier import VerificationScope, verify_output

ROOT = Path(__file__).resolve().parents[1]
CASE_ID = "L3A_CASE_001"
ORDER_ID = "ORD-001"
EVIDENCE_REF = "ev_order_aaaaaaaaaaaaaaaaaaaa"


class UnusedGateway(EvidenceGateway):
    def __init__(self) -> None:
        pass


def context(tmp_path: Path) -> CaseContext:
    contracts = Contracts(ROOT / "contracts" / "schemas")
    ledger = EvidenceLedger(CASE_ID, ORDER_ID)
    ledger.admit(
        "get_order",
        {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": EVIDENCE_REF,
            "result_hash": "sha256:" + "a" * 64,
            "domain": "order",
            "data": {"case_id": CASE_ID, "order_id": ORDER_ID},
        },
    )
    return CaseContext(
        case_id=CASE_ID,
        order_id=ORDER_ID,
        opened_at=datetime(2018, 1, 10, tzinfo=UTC),
        policy_version="EC_POLICY_V1",
        gateway=UnusedGateway(),
        trace=TraceWriter(tmp_path / "trace.jsonl", contracts),
        ledger=ledger,
        catalog=frozenset({"get_order"}),
    )


def test_policy_rejects_non_refund_action_with_money() -> None:
    data = {
        "policy_version": "EC_POLICY_V1",
        "currency": "BRL",
        "rules": {
            "duplicate_charge": {
                "case_status": "needs_investigation",
                "recommended_action": "manual_review",
                "refund_brl": 50.0,
                "responsible_parties": [{"party_type": "platform", "party_id": "PLATFORM-1"}],
            }
        },
    }

    assert policy_rule(data, "duplicate_charge", "EC_POLICY_V1") is None


def test_policy_application_caps_refund_to_evidence() -> None:
    rule = {
        "case_status": "action_required",
        "recommended_action": "refund_duplicate_charge",
        "refund_brl": 100.0,
        "responsible_parties": [{"party_type": "platform", "party_id": "PLATFORM-1"}],
    }
    diagnosis = rules.Diagnosis(
        issue="duplicate_charge",
        evidence_basis_brl=Decimal("60.00"),
        basis_entity=ORDER_ID,
        signals=(),
        doubts=(),
    )
    decision = apply_policy(rule, diagnosis)

    assert decision.refund_brl == Decimal("60.00")


def test_verifier_repairs_scope_money_and_policy(tmp_path: Path) -> None:
    ctx = context(tmp_path)
    decision = PolicyDecision(
        issue="duplicate_charge",
        case_status="action_required",
        action="refund_duplicate_charge",
        refund_brl=Decimal("60.00"),
        policy_refund_brl=Decimal("60.00"),
        parties=(("platform", "PLATFORM-1"),),
        policy_applied=True,
    )
    scope = VerificationScope(
        order_id=ORDER_ID,
        item_ids=frozenset(),
        seller_ids=frozenset(),
        payment_references=frozenset(),
        shipment_ids=frozenset(),
        refund_ceiling=Decimal("100.00"),
        decision=decision,
        policy_party_ids=frozenset({("platform", "PLATFORM-1")}),
    )
    draft = {
        "schema_version": "wrong",
        "case_id": "L3A_CASE_999",
        "assessment": {
            "primary_issue": "unsupported_claim",
            "case_status": "no_action",
            "confidence": 1.5,
        },
        "affected_entities": {
            "order_ids": [ORDER_ID],
            "item_ids": ["ITEM-FAKE"],
            "seller_ids": ["SELLER-FAKE"],
            "payment_references": ["PAY-FAKE"],
            "shipment_ids": ["SHIP-FAKE"],
        },
        "claim_assessments": [
            {
                "claim_id": "claim-01",
                "verdict": "supported",
                "confidence": 0.9,
                "evidence_refs": [EVIDENCE_REF, "ev_fake_bbbbbbbbbbbbbbbbbbbb"],
            }
        ],
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": "DUPLICATE_CHARGE", "rank": 1}],
            "responsible_parties": [
                {"party_type": "platform", "party_id": "PLATFORM-FAKE"},
                {"party_type": "seller", "party_id": "SELLER-FAKE"},
            ],
        },
        "evidence_refs": [EVIDENCE_REF, "ev_fake_bbbbbbbbbbbbbbbbbbbb"],
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 999.0,
            "refund_lines": [],
        },
        "resolution_actions": ["document_no_action"],
    }

    output, repairs = verify_output(draft, scope, ctx)

    assert output["case_id"] == CASE_ID
    assert output["evidence_refs"] == [EVIDENCE_REF]
    assert output["financial_resolution"]["recommended_refund_brl"] == 60.0
    lines_total = sum(
        line["amount_brl"] for line in output["financial_resolution"]["refund_lines"]
    )
    assert lines_total == 60.0
    assert output["affected_entities"] == {
        "order_ids": [ORDER_ID],
        "item_ids": [],
        "seller_ids": [],
        "payment_references": [],
        "shipment_ids": [],
    }
    assert output["resolution_actions"] == ["refund_duplicate_charge"]
    assert {
        "case_scope",
        "evidence_ownership",
        "claim_linkage",
        "entity_scope",
        "money_totals",
        "policy_consistency",
    } <= set(repairs)
