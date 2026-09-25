from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from . import OUTPUT_SCHEMA_VERSION, rules
from .a2a import A2ABus, A2AMessage
from .agents import (
    COORDINATOR,
    INSUFFICIENT_DECISION,
    NO_REFUND_ACTIONS,
    ORDER_AGENT,
    PAYMENT_AGENT,
    POLICY_AGENT,
    SHIPMENT_AGENT,
    VERIFIER,
    CaseContext,
    OrderAgent,
    PaymentAgent,
    PolicyAgent,
    PolicyDecision,
    ShipmentAgent,
    discover_tools,
)
from .contracts import ContractError
from .evidence import EvidenceLedger
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter
from .verifier import VerificationScope, Verifier, verify_output

CITATIONS: dict[str, tuple[str, ...]] = {
    "canceled_order_paid": (
        "get_order",
        "get_order_items",
        "get_payment_timeline",
        "get_order_payments",
        "get_policy",
    ),
    "unavailable_order_paid": (
        "get_order",
        "get_order_items",
        "get_sellers",
        "get_payment_timeline",
        "get_order_payments",
        "get_policy",
    ),
    "late_delivery_seller": (
        "get_order",
        "get_shipment_summary",
        "get_order_items",
        "get_sellers",
        "get_payment_timeline",
        "get_policy",
    ),
    "late_delivery_logistics": (
        "get_order",
        "get_shipment_summary",
        "get_order_items",
        "get_payment_timeline",
        "get_policy",
    ),
    "valid_split_payment": (
        "get_order",
        "get_order_items",
        "get_payment_timeline",
        "get_order_payments",
        "get_policy",
    ),
    "payment_mismatch": (
        "get_order",
        "get_order_items",
        "get_payment_timeline",
        "get_order_payments",
        "get_policy",
    ),
    "duplicate_charge": (
        "get_order",
        "get_order_items",
        "get_payment_timeline",
        "get_order_payments",
        "get_policy",
    ),
    "refund_pending": (
        "get_order",
        "get_order_items",
        "get_payment_timeline",
        "get_refund_timeline",
        "get_policy",
    ),
    "refund_failed": (
        "get_order",
        "get_order_items",
        "get_payment_timeline",
        "get_refund_timeline",
        "get_policy",
    ),
    "unsupported_claim": (
        "get_order",
        "get_order_items",
        "get_shipment_summary",
        "get_payment_timeline",
        "get_order_payments",
        "get_policy",
    ),
    rules.INSUFFICIENT: (
        "get_order",
        "get_order_items",
        "get_payment_timeline",
        "get_shipment_summary",
        "get_policy",
    ),
}
MONEY_TOOLS = frozenset(
    {
        "get_order_items",
        "get_payment_timeline",
        "get_order_payments",
        "get_refund_timeline",
        "get_policy",
    }
)
FULL_REFUND_TOPIC = "requested_full_refund"
FULL_REFUND_VERDICTS = {
    "issue_refund": "supported",
    "retry_refund": "supported",
    "refund_freight": "partially_supported",
    "refund_duplicate_charge": "partially_supported",
    "reconcile_payment": "partially_supported",
    "monitor_refund": "insufficient_evidence",
    "manual_review": "insufficient_evidence",
    "document_no_action": "unsupported",
}
PARTY_TYPES = frozenset(
    {"seller", "platform", "logistics_provider", "payment_provider", "customer", "unknown"}
)
BASE_CONFIDENCE = 0.95
DOUBT_PENALTY = 0.1
INSUFFICIENT_CONFIDENCE = 0.2
CLAIM_CONFIDENCE = 0.9
FULL_REFUND_CONFIDENCE = 0.8
UNKNOWN_CLAIM_CONFIDENCE = 0.5
MAX_CLAIMS = 5
MAX_CONFLICTS = 5
MAX_PARTIES = 5


@dataclass
class Findings:
    order: rules.OrderFacts | None = None
    items: rules.ItemView = field(default_factory=rules.ItemView)
    payments: rules.PaymentView = field(default_factory=rules.PaymentView)
    refunds: rules.RefundView = field(default_factory=rules.RefundView)
    shipment: rules.ShipmentView = field(default_factory=rules.ShipmentView)
    complete: bool = False
    refs: list[str] = field(default_factory=list)

    def absorb(self, reply: A2AMessage) -> None:
        self.refs.extend(ref for ref in reply.evidence_refs if ref not in self.refs)


@dataclass(frozen=True)
class SellerCheck:
    seller_ids: tuple[str, ...] = ()
    confirmed: bool | None = None


def _clean(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


async def _open_case(
    case: Mapping[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> CaseContext:
    case_id = str(case["case_id"])
    request = case.get("customer_request")
    request = request if isinstance(request, Mapping) else {}
    order_id = _clean(request.get("claimed_order_id"))
    return CaseContext(
        case_id=case_id,
        order_id=order_id,
        opened_at=rules.parse_time(case.get("opened_at")),
        policy_version=_clean(case.get("policy_version")),
        gateway=gateway,
        trace=trace,
        ledger=EvidenceLedger(case_id, order_id),
        catalog=await discover_tools(gateway),
    )


def _claims(case: Mapping[str, Any]) -> list[tuple[str, str]]:
    request = case.get("customer_request")
    raw = request.get("claims") if isinstance(request, Mapping) else None
    claims: list[tuple[str, str]] = []
    seen: set[str] = set()
    for claim in raw if isinstance(raw, list) else []:
        if not isinstance(claim, Mapping):
            continue
        claim_id, topic = _clean(claim.get("claim_id")), _clean(claim.get("topic"))
        if 0 < len(claim_id) <= 64 and claim_id not in seen:
            seen.add(claim_id)
            claims.append((claim_id, topic))
    return claims[:MAX_CLAIMS]


async def _collect_findings(ctx: CaseContext, bus: A2ABus) -> Findings:
    findings = Findings()
    if not ctx.order_id:
        return findings
    order_reply = await bus.request(COORDINATOR, ORDER_AGENT, "collect_order_context")
    findings.absorb(order_reply)
    order, items = order_reply.payload.get("order"), order_reply.payload.get("items")
    if not isinstance(order, rules.OrderFacts) or not isinstance(items, rules.ItemView):
        return findings
    findings.order, findings.items = order, items
    anchor = list(order_reply.evidence_refs)
    payment_reply = await bus.request(
        COORDINATOR,
        PAYMENT_AGENT,
        "collect_payment_context",
        {"order": order},
        anchor,
    )
    shipment_reply = await bus.request(
        COORDINATOR,
        SHIPMENT_AGENT,
        "collect_shipment_context",
        {"order": order, "items": items},
        anchor,
    )
    findings.absorb(payment_reply)
    findings.absorb(shipment_reply)
    payments = payment_reply.payload.get("payments")
    payments_ok = (
        payment_reply.intent == "payment_context_ready"
        and payment_reply.payload.get("timeline_available") is True
        and isinstance(payments, rules.PaymentView)
    )
    if payments_ok:
        findings.payments = payments
        findings.refunds = payment_reply.payload.get("refunds", rules.RefundView())
    shipment = shipment_reply.payload.get("shipment")
    shipment_ok = (
        shipment_reply.intent == "shipment_context_ready"
        and isinstance(shipment, rules.ShipmentView)
        and shipment.available
    )
    if shipment_ok:
        findings.shipment = shipment
    findings.complete = (
        order_reply.payload.get("items_available") is True
        and payments_ok
        and shipment_ok
    )
    return findings


def _policy_outcome(reply: A2AMessage) -> tuple[PolicyDecision, rules.Diagnosis]:
    decision = reply.payload.get("decision")
    diagnosis = reply.payload.get("diagnosis")
    if (
        reply.intent == "policy_decided"
        and isinstance(decision, PolicyDecision)
        and isinstance(diagnosis, rules.Diagnosis)
    ):
        return decision, diagnosis
    failed = ("policy_task_failed",)
    diagnosis = rules.Diagnosis(rules.INSUFFICIENT, rules.ZERO, None, failed, failed)
    return INSUFFICIENT_DECISION, diagnosis


async def _check_sellers(
    bus: A2ABus, findings: Findings, decision: PolicyDecision
) -> SellerCheck:
    if not any(party_type == "seller" for party_type, _ in decision.parties):
        return SellerCheck()
    candidates = rules.responsible_sellers(findings.order, findings.items, decision.issue)
    if not candidates:
        return SellerCheck(confirmed=False)
    reply = await bus.request(
        COORDINATOR,
        ORDER_AGENT,
        "confirm_responsible_seller",
        {"seller_ids": candidates},
    )
    confirmed = reply.payload.get("confirmed_seller_ids")
    if reply.intent == "seller_confirmed" and confirmed:
        return SellerCheck(tuple(confirmed), True)
    return SellerCheck(tuple(candidates), False)


def _parties(decision: PolicyDecision, sellers: SellerCheck) -> list[dict[str, Any]]:
    parties: list[dict[str, Any]] = []
    for party_type, policy_party_id in decision.parties:
        if party_type not in PARTY_TYPES:
            party_type, policy_party_id = "unknown", None
        if party_type == "seller":
            ids: tuple[str | None, ...] = sellers.seller_ids or (None,)
            parties.extend({"party_type": "seller", "party_id": seller} for seller in ids)
        else:
            party_id = policy_party_id if isinstance(policy_party_id, str) else None
            parties.append({"party_type": party_type, "party_id": party_id})
    unique = list({(party["party_type"], party["party_id"]): party for party in parties}.values())
    return unique[:MAX_PARTIES] or [{"party_type": "unknown", "party_id": None}]


def _confidence(
    decision: PolicyDecision, diagnosis: rules.Diagnosis, sellers: SellerCheck
) -> float:
    if decision.issue == rules.INSUFFICIENT:
        return INSUFFICIENT_CONFIDENCE
    score = BASE_CONFIDENCE - DOUBT_PENALTY * len(diagnosis.doubts)
    capped = (
        decision.action not in NO_REFUND_ACTIONS
        and decision.policy_refund_brl is not None
        and decision.refund_brl < decision.policy_refund_brl
    )
    if capped:
        score -= DOUBT_PENALTY
    if sellers.confirmed is False:
        score -= DOUBT_PENALTY
    return round(min(0.99, max(0.05, score)), 2)


def _claim_assessments(
    claims: list[tuple[str, str]],
    decision: PolicyDecision,
    ledger: EvidenceLedger,
    cited_tools: list[str],
) -> list[dict[str, Any]]:
    cited_refs = ledger.refs(cited_tools)
    money_refs = ledger.refs(tool for tool in cited_tools if tool in MONEY_TOOLS)
    insufficient = decision.issue == rules.INSUFFICIENT
    assessments: list[dict[str, Any]] = []
    for claim_id, topic in claims:
        if topic in rules.POLICY_ISSUES:
            if insufficient:
                verdict = "insufficient_evidence"
            elif topic == decision.issue and decision.case_status != "no_action":
                verdict = "supported"
            else:
                verdict = "unsupported"
            confidence, refs = CLAIM_CONFIDENCE, cited_refs
        elif topic == FULL_REFUND_TOPIC:
            verdict = FULL_REFUND_VERDICTS.get(decision.action, "insufficient_evidence")
            confidence, refs = FULL_REFUND_CONFIDENCE, money_refs
        else:
            verdict, confidence, refs = "insufficient_evidence", UNKNOWN_CLAIM_CONFIDENCE, []
        if insufficient:
            confidence = INSUFFICIENT_CONFIDENCE
        assessments.append(
            {
                "claim_id": claim_id,
                "verdict": verdict,
                "confidence": confidence,
                "evidence_refs": list(refs),
            }
        )
    return assessments


def _data_conflicts(
    cited_tools: list[str],
    findings: Findings,
    decision: PolicyDecision,
    diagnosis: rules.Diagnosis,
) -> list[dict[str, Any]]:
    cited = set(cited_tools)
    conflicts: list[dict[str, Any]] = []

    def add(source: str, field_name: str, other: str, code: str) -> None:
        if source in cited and len(conflicts) < MAX_CONFLICTS:
            conflicts.append(
                {
                    "field": field_name,
                    "sources": [source, other],
                    "selected_source": other,
                    "resolution_code": code,
                }
            )

    payments = findings.payments
    items = findings.items
    refunds = findings.refunds
    shipment = findings.shipment
    if payments.excluded_events:
        add(
            "get_payment_timeline",
            "payment_timeline.events",
            "get_order",
            "EXCLUDED_OUTSIDE_APPROVAL_WINDOW",
        )
    if payments.excluded_rows or payments.collapsed_rows:
        code = (
            "KEPT_ROWS_MATCHING_IN_WINDOW_CAPTURES"
            if payments.excluded_rows
            else "COLLAPSED_REPLICATED_ROWS"
        )
        add(
            "get_order_payments",
            "order_payments.payment_value",
            "get_payment_timeline",
            code,
        )
    if items.excluded_rows or items.collapsed_rows:
        code = (
            "EXCLUDED_OUTSIDE_ORDER_WINDOW"
            if items.excluded_rows
            else "COLLAPSED_REPLICATED_ROWS"
        )
        add(
            "get_order_items",
            "order_items.shipping_limit_date",
            "get_order",
            code,
        )
    if refunds.excluded_events:
        add(
            "get_refund_timeline",
            "refund_timeline.events",
            "get_order",
            "EXCLUDED_OUTSIDE_CASE_WINDOW",
        )
    if shipment.excluded_events:
        add(
            "get_shipment_summary",
            "shipment_summary.events",
            "get_order",
            "EXCLUDED_NOT_MATCHING_DELIVERY",
        )
    for name in shipment.conflicting_fields:
        add(
            "get_shipment_summary",
            f"shipment_summary.{name}",
            "get_order",
            "PREFERRED_AUTHORITATIVE_ORDER_ROW",
        )
    if "late_event_actor_disagrees" in diagnosis.doubts:
        add(
            "get_shipment_summary",
            "shipment_summary.events.actor",
            "get_order_items",
            "PREFERRED_HANDOFF_VS_SHIPPING_LIMIT",
        )
    capped = (
        decision.action not in NO_REFUND_ACTIONS
        and decision.policy_refund_brl is not None
        and decision.refund_brl < decision.policy_refund_brl
    )
    if capped:
        add(
            "get_policy",
            "financial_resolution.recommended_refund_brl",
            "get_payment_timeline",
            "CAPPED_TO_EVIDENCE",
        )
    return conflicts


def compose_output(
    ctx: CaseContext,
    claims: list[tuple[str, str]],
    findings: Findings,
    decision: PolicyDecision,
    diagnosis: rules.Diagnosis,
    sellers: SellerCheck,
) -> dict[str, Any]:
    issue = decision.issue
    citations = CITATIONS.get(issue, CITATIONS[rules.INSUFFICIENT])
    cited_tools = [tool for tool in citations if ctx.ledger.get(tool) is not None]
    refund = decision.refund_brl
    refund_lines = (
        [
            {
                "reason_code": issue,
                "amount_brl": float(refund),
                "entity_id": diagnosis.basis_entity,
            }
        ]
        if refund > 0
        else []
    )
    output: dict[str, Any] = {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": ctx.case_id,
        "assessment": {
            "primary_issue": issue,
            "case_status": decision.case_status,
            "confidence": _confidence(decision, diagnosis, sellers),
        },
        "affected_entities": {
            "order_ids": [findings.order.order_id] if findings.order else [],
            "item_ids": findings.items.item_ids[:20],
            "seller_ids": findings.items.seller_ids[:20],
            "payment_references": findings.payments.payment_references[:20],
            "shipment_ids": (
                [findings.shipment.shipment_id] if findings.shipment.shipment_id else []
            )[:20],
        },
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": issue.upper(), "rank": 1}],
            "responsible_parties": _parties(decision, sellers),
        },
        "evidence_refs": ctx.ledger.refs(cited_tools)[:30],
        "data_conflicts": _data_conflicts(cited_tools, findings, decision, diagnosis),
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": float(refund),
            "refund_lines": refund_lines,
        },
        "resolution_actions": [decision.action],
    }
    assessments = _claim_assessments(claims, decision, ctx.ledger, cited_tools)
    if assessments:
        output["claim_assessments"] = assessments
    return output


def _last_resort_output(ctx: CaseContext) -> dict[str, Any]:
    findings = Findings()
    diagnosis = rules.Diagnosis(
        rules.INSUFFICIENT,
        rules.ZERO,
        None,
        ("verification_failed",),
        ("verification_failed",),
    )
    return compose_output(ctx, [], findings, INSUFFICIENT_DECISION, diagnosis, SellerCheck())


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    ctx = await _open_case(case, gateway, trace)
    bus = A2ABus(ctx.case_id, trace)
    bus.register(
        OrderAgent(ctx),
        PaymentAgent(ctx),
        ShipmentAgent(ctx),
        PolicyAgent(ctx),
        Verifier(ctx),
    )

    findings = await _collect_findings(ctx, bus)
    policy_reply = await bus.request(
        COORDINATOR,
        POLICY_AGENT,
        "decide_policy",
        {
            "order": findings.order,
            "items": findings.items,
            "payments": findings.payments,
            "refunds": findings.refunds,
            "shipment": findings.shipment,
            "complete": findings.complete,
        },
        findings.refs,
    )
    decision, diagnosis = _policy_outcome(policy_reply)
    sellers = await _check_sellers(bus, findings, decision)
    draft = compose_output(ctx, _claims(case), findings, decision, diagnosis, sellers)
    policy_party_ids = frozenset(
        (party_type, party_id)
        for party_type, party_id in decision.parties
        if party_id is not None and party_type != "seller"
    )
    scope = VerificationScope(
        order_id=findings.order.order_id if findings.order else "",
        item_ids=frozenset(findings.items.item_ids),
        seller_ids=frozenset(findings.items.seller_ids),
        payment_references=frozenset(findings.payments.payment_references),
        shipment_ids=frozenset(
            [findings.shipment.shipment_id] if findings.shipment.shipment_id else []
        ),
        refund_ceiling=findings.refunds.outstanding_total
        if findings.refunds.available
        else findings.payments.captured_total,
        decision=decision,
        policy_party_ids=policy_party_ids,
    )
    reply = await bus.request(
        COORDINATOR,
        VERIFIER,
        "verify_output",
        {"draft": draft, "scope": scope},
        draft["evidence_refs"],
    )
    output = reply.payload.get("output")
    if reply.intent.startswith("verification_") and isinstance(output, dict):
        return output
    try:
        output, _ = verify_output(draft, scope, ctx)
    except ContractError:
        output = _last_resort_output(ctx)
        trace.contracts.validate_output(output, f"fallback output for {ctx.case_id}")
    return output
