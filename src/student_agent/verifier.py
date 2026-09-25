from __future__ import annotations

import copy
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, ClassVar

from . import OUTPUT_SCHEMA_VERSION, rules
from .a2a import FAILED, A2AMessage
from .agents import NO_REFUND_ACTIONS, VERIFIER, CaseContext, PolicyDecision

CHECKS = (
    "case_scope",
    "evidence_ownership",
    "claim_linkage",
    "entity_scope",
    "money_totals",
    "policy_consistency",
    "confidence_bounds",
    "schema",
)
REPAIR_PENALTY = 0.1
INSUFFICIENT_CONFIDENCE_CAP = 0.4


@dataclass(frozen=True)
class VerificationScope:
    order_id: str
    item_ids: frozenset[str]
    seller_ids: frozenset[str]
    payment_references: frozenset[str]
    shipment_ids: frozenset[str]
    refund_ceiling: Decimal
    decision: PolicyDecision
    policy_party_ids: frozenset[tuple[str, str]]


def _money(value: Any) -> Decimal:
    return rules.parse_money(value) or rules.ZERO


def _string_values(values: Any, allowed: frozenset[str]) -> list[str]:
    if not isinstance(values, list):
        return []
    return list(
        dict.fromkeys(
            value for value in values if isinstance(value, str) and value in allowed
        )
    )


def verify_output(
    draft: dict[str, Any], scope: VerificationScope, ctx: CaseContext
) -> tuple[dict[str, Any], list[str]]:
    output = copy.deepcopy(draft)
    repairs: list[str] = []

    scope_ids = (output.get("case_id"), output.get("schema_version"))
    if scope_ids != (ctx.case_id, OUTPUT_SCHEMA_VERSION):
        output["case_id"], output["schema_version"] = ctx.case_id, OUTPUT_SCHEMA_VERSION
        repairs.append("case_scope")

    raw_refs = output.get("evidence_refs", [])
    output_refs = raw_refs if isinstance(raw_refs, list) else []
    owned = list(
        dict.fromkeys(
            ref for ref in output_refs if isinstance(ref, str) and ctx.ledger.owns(ref)
        )
    )
    if owned != output_refs:
        output["evidence_refs"] = owned
        repairs.append("evidence_ownership")

    for claim in output.get("claim_assessments", []):
        linked = [ref for ref in claim["evidence_refs"] if ref in owned]
        if linked != claim["evidence_refs"]:
            claim["evidence_refs"] = linked
            repairs.append("claim_linkage")

    entities = output["affected_entities"]
    scoped = {
        "order_ids": [scope.order_id] if scope.order_id else [],
        "item_ids": _string_values(entities["item_ids"], scope.item_ids),
        "seller_ids": _string_values(entities["seller_ids"], scope.seller_ids),
        "payment_references": _string_values(
            entities["payment_references"], scope.payment_references
        ),
        "shipment_ids": _string_values(entities["shipment_ids"], scope.shipment_ids),
    }
    parties = output["root_cause_analysis"]["responsible_parties"]
    for party in parties:
        party_id = party["party_id"]
        if party_id is None:
            continue
        if party["party_type"] == "seller":
            allowed = party_id in scope.seller_ids
        else:
            allowed = (party["party_type"], party_id) in scope.policy_party_ids
        if not allowed:
            party["party_id"] = None
            repairs.append("entity_scope")
    if any(entities[key] != value for key, value in scoped.items()):
        entities.update(scoped)
        repairs.append("entity_scope")

    decision = scope.decision
    finance = output["financial_resolution"]
    refund = _money(finance["recommended_refund_brl"])
    ceiling = rules.ZERO if decision.action in NO_REFUND_ACTIONS else scope.refund_ceiling
    ceiling = min(ceiling, decision.refund_brl)
    lines_total = sum(
        (_money(line["amount_brl"]) for line in finance["refund_lines"]),
        rules.ZERO,
    )
    if refund > ceiling or not rules.money_equal(lines_total, refund):
        refund = min(refund, ceiling)
        finance["recommended_refund_brl"] = float(refund)
        finance["refund_lines"] = (
            [
                {
                    "reason_code": decision.issue,
                    "amount_brl": float(refund),
                    "entity_id": None,
                }
            ]
            if refund > 0
            else []
        )
        repairs.append("money_totals")

    assessment = output["assessment"]
    consistent = (
        assessment["primary_issue"] == decision.issue
        and assessment["case_status"] == decision.case_status
        and output["resolution_actions"] == [decision.action]
    )
    if not consistent:
        assessment["primary_issue"] = decision.issue
        assessment["case_status"] = decision.case_status
        output["resolution_actions"] = [decision.action]
        repairs.append("policy_consistency")

    confidence = min(1.0, max(0.0, float(assessment["confidence"])))
    if decision.issue == rules.INSUFFICIENT:
        confidence = min(confidence, INSUFFICIENT_CONFIDENCE_CAP)
    if repairs:
        confidence = max(0.0, confidence - REPAIR_PENALTY)
    if confidence != assessment["confidence"]:
        assessment["confidence"] = round(confidence, 2)
        if not repairs:
            repairs.append("confidence_bounds")

    ctx.trace.contracts.validate_output(output, f"verified output for {ctx.case_id}")
    return output, list(dict.fromkeys(repairs))


class Verifier:
    name: ClassVar[str] = VERIFIER
    tools: ClassVar[frozenset[str]] = frozenset()

    def __init__(self, ctx: CaseContext) -> None:
        self.ctx = ctx

    async def handle(self, task: A2AMessage) -> A2AMessage:
        draft, scope = task.payload.get("draft"), task.payload.get("scope")
        if (
            task.intent != "verify_output"
            or not isinstance(draft, dict)
            or not isinstance(scope, VerificationScope)
        ):
            return task.reply(FAILED, {"reason": "missing_draft"})
        output, repairs = verify_output(draft, scope, self.ctx)
        verdict = "repaired" if repairs else "passed"
        self.ctx.trace.emit(
            case_id=self.ctx.case_id,
            event_type="verification_completed",
            actor=self.name,
            target="coordinator",
            decision_code=verdict,
            evidence_refs=output["evidence_refs"][:20] or None,
            attributes={
                "checks_run": len(CHECKS),
                "repairs": len(repairs),
                "repair_codes": ",".join(repairs) or "none",
                "primary_issue": output["assessment"]["primary_issue"],
                "confidence": output["assessment"]["confidence"],
                "evidence_count": len(output["evidence_refs"]),
            },
        )
        return task.reply(f"verification_{verdict}", {"output": output}, output["evidence_refs"])
