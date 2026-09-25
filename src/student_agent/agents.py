"""Specialist agents. Each one owns a small tool allow-list and reports findings over A2A."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar
from weakref import WeakKeyDictionary

from mcp.shared.exceptions import MCPError

from . import rules
from .a2a import FAILED, A2AMessage
from .evidence import EvidenceLedger, EvidenceRecord, EvidenceRejected
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

COORDINATOR = "coordinator"
ORDER_AGENT = "order-agent"
PAYMENT_AGENT = "payment-agent"
SHIPMENT_AGENT = "shipment-agent"
POLICY_AGENT = "policy-agent"
VERIFIER = "verifier"

CALL_TIMEOUT_S = 60.0
CALL_ATTEMPTS = 2  # one retry, only for timeouts: every call is read-only and idempotent
NO_REFUND_ACTIONS = frozenset({"document_no_action", "monitor_refund"})

_CATALOGS: WeakKeyDictionary[EvidenceGateway, frozenset[str]] = WeakKeyDictionary()


async def discover_tools(gateway: EvidenceGateway) -> frozenset[str]:
    """Tool names advertised by the gateway, listed again at the start of every case.

    Discovery is the case's first request, so every tool a case calls was advertised within
    that case and a full round trip separates ``case_received`` from the first audited call.
    If a listing fails, the catalog this gateway session listed last is reused; tool names are
    never guessed, and without any catalog no tool is called.
    """
    try:
        catalog = frozenset(await asyncio.wait_for(gateway.list_tools(), CALL_TIMEOUT_S))
    except (TimeoutError, RuntimeError, MCPError):
        return _CATALOGS.get(gateway, frozenset())
    _CATALOGS[gateway] = catalog
    return catalog


@dataclass
class CaseContext:
    case_id: str
    order_id: str
    opened_at: datetime | None
    policy_version: str
    gateway: EvidenceGateway
    trace: TraceWriter
    ledger: EvidenceLedger
    catalog: frozenset[str]


def _row_count(data: Any) -> int:
    if isinstance(data, list):
        return len(data)
    if isinstance(data, Mapping):
        nested = [value for value in data.values() if isinstance(value, list)]
        return sum(len(value) for value in nested) if nested else 1
    return 0


class Specialist:
    name: ClassVar[str] = "specialist"
    tools: ClassVar[frozenset[str]] = frozenset()

    def __init__(self, ctx: CaseContext) -> None:
        self.ctx = ctx

    async def handle(self, task: A2AMessage) -> A2AMessage:
        raise NotImplementedError

    async def fetch(self, tool: str, **arguments: str) -> EvidenceRecord | None:
        """Call one allowed tool for the active case; failures become missing evidence.

        Specialists await each call before starting the next, so a case never has more than
        one MCP call in flight and the server audits its calls in the order they were made.
        """
        if tool not in self.tools:
            raise PermissionError(f"{self.name} is not allowed to call {tool}")
        outcome = "tool_not_discovered"
        attempts = 0
        while tool in self.ctx.catalog and attempts < CALL_ATTEMPTS:
            attempts += 1
            try:
                evidence = await asyncio.wait_for(
                    self.ctx.gateway.call(tool, case_id=self.ctx.case_id, **arguments),
                    CALL_TIMEOUT_S,
                )
            except TimeoutError:
                outcome = "timeout"
                continue
            except (RuntimeError, MCPError):
                outcome = "not_found"
            except ValueError:
                outcome = "invalid_evidence"
            else:
                try:
                    record = self.ctx.ledger.admit(tool, evidence, self.name)
                except EvidenceRejected:
                    outcome = "out_of_scope_evidence"
                else:
                    self._trace_consumed(record, attempts)
                    return record
            break
        self.ctx.ledger.record_failure(tool, outcome)
        self.ctx.trace.emit(
            case_id=self.ctx.case_id,
            event_type="tool_result_consumed",
            actor=self.name,
            tool_name=tool,
            decision_code=outcome,
            attributes={"outcome": outcome, "attempts": attempts},
        )
        return None

    def _trace_consumed(self, record: EvidenceRecord, attempts: int) -> None:
        self.ctx.trace.emit(
            case_id=self.ctx.case_id,
            event_type="tool_result_consumed",
            actor=self.name,
            tool_name=record.tool_name,
            decision_code="evidence_admitted",
            evidence_refs=[record.evidence_ref],
            attributes={
                "domain": record.domain,
                "rows": _row_count(record.data),
                "warnings": len(record.warnings),
                "attempts": attempts,
            },
        )


def _refs(*records: EvidenceRecord | None) -> list[str]:
    return [record.evidence_ref for record in records if record is not None]


def _data(record: EvidenceRecord | None) -> Any:
    return None if record is None else record.data


class OrderAgent(Specialist):
    """Owns the authoritative order row, its item lines and the seller registry."""

    name = ORDER_AGENT
    tools = frozenset({"get_order", "get_order_items", "get_sellers"})

    async def handle(self, task: A2AMessage) -> A2AMessage:
        if task.intent == "collect_order_context":
            order_record = await self.fetch("get_order", order_id=self.ctx.order_id)
            order = rules.order_facts(_data(order_record), self.ctx.order_id)
            items = rules.ItemView()
            items_record = None
            if order is not None:
                # Item rows are only read against a confirmed order row.
                items_record = await self.fetch("get_order_items", order_id=self.ctx.order_id)
            if order is not None and items_record is not None:
                items = rules.analyse_items(order, items_record.data)
            payload = {"order": order, "items": items, "items_available": items_record is not None}
            return task.reply("order_context_ready", payload, _refs(order_record, items_record))
        if task.intent == "confirm_responsible_seller":
            wanted = [str(seller) for seller in task.payload.get("seller_ids", ())]
            record = await self.fetch("get_sellers", order_id=self.ctx.order_id)
            confirmed = rules.known_sellers(_data(record), wanted)
            intent = "seller_confirmed" if confirmed else "seller_unconfirmed"
            return task.reply(intent, {"confirmed_seller_ids": confirmed}, _refs(record))
        return task.reply(FAILED, {"reason": "unsupported_intent"})


class PaymentAgent(Specialist):
    """Owns captures, payment rows, reconciliation and refund lifecycle evidence."""

    name = PAYMENT_AGENT
    tools = frozenset({"get_payment_timeline", "get_order_payments", "get_refund_timeline"})

    async def handle(self, task: A2AMessage) -> A2AMessage:
        order = task.payload.get("order")
        if task.intent != "collect_payment_context" or not isinstance(order, rules.OrderFacts):
            return task.reply(FAILED, {"reason": "missing_order_anchor"})
        timeline = await self.fetch("get_payment_timeline", order_id=self.ctx.order_id)
        rows = await self.fetch("get_order_payments", order_id=self.ctx.order_id)
        refunds = await self.fetch("get_refund_timeline", order_id=self.ctx.order_id)
        payments = rules.analyse_payments(order, _data(timeline), _data(rows), self.ctx.opened_at)
        refund_view = rules.analyse_refunds(
            order, _data(refunds), payments.captures, self.ctx.opened_at
        )
        payload = {
            "payments": payments,
            "refunds": refund_view,
            "timeline_available": timeline is not None,
        }
        return task.reply("payment_context_ready", payload, _refs(timeline, rows, refunds))


class ShipmentAgent(Specialist):
    """Owns delivery timestamps, seller handoff limits and carrier events."""

    name = SHIPMENT_AGENT
    tools = frozenset({"get_shipment_summary"})

    async def handle(self, task: A2AMessage) -> A2AMessage:
        order, items = task.payload.get("order"), task.payload.get("items")
        if (
            task.intent != "collect_shipment_context"
            or not isinstance(order, rules.OrderFacts)
            or not isinstance(items, rules.ItemView)
        ):
            return task.reply(FAILED, {"reason": "missing_order_anchor"})
        record = await self.fetch("get_shipment_summary", order_id=self.ctx.order_id)
        view = rules.analyse_shipment(order, items, _data(record))
        return task.reply("shipment_context_ready", {"shipment": view}, _refs(record))


@dataclass(frozen=True)
class PolicyDecision:
    issue: str
    case_status: str
    action: str
    refund_brl: Decimal
    policy_refund_brl: Decimal | None
    parties: tuple[tuple[str, str | None], ...]
    policy_applied: bool


INSUFFICIENT_DECISION = PolicyDecision(
    issue=rules.INSUFFICIENT,
    case_status="needs_investigation",
    action="manual_review",
    refund_brl=rules.ZERO,
    policy_refund_brl=None,
    parties=(("unknown", None),),
    policy_applied=False,
)


def policy_rule(data: Any, issue: str, version: str) -> Mapping[str, Any] | None:
    """Return the published rule for ``issue`` when the policy matches the requested version."""
    if not isinstance(data, Mapping) or data.get("policy_version") != version:
        return None
    if data.get("currency", "BRL") != "BRL":
        return None
    rules_by_issue = data.get("rules")
    rule = rules_by_issue.get(issue) if isinstance(rules_by_issue, Mapping) else None
    required = ("case_status", "recommended_action", "responsible_parties")
    if not isinstance(rule, Mapping) or any(key not in rule for key in required):
        return None
    return rule


def apply_policy(rule: Mapping[str, Any], diagnosis: rules.Diagnosis) -> PolicyDecision:
    """Combine the published rule with the evidence basis; never refund more than evidenced."""
    action = str(rule["recommended_action"])
    policy_refund = rules.parse_money(rule.get("refund_brl"))
    if action in NO_REFUND_ACTIONS:
        refund = rules.ZERO
    elif policy_refund is None:
        refund = diagnosis.evidence_basis_brl
    else:
        refund = min(policy_refund, diagnosis.evidence_basis_brl)
    parties = tuple(
        (str(party.get("party_type", "unknown")), party.get("party_id"))
        for party in rule["responsible_parties"]
        if isinstance(party, Mapping)
    )
    return PolicyDecision(
        issue=diagnosis.issue,
        case_status=str(rule["case_status"]),
        action=action,
        refund_brl=refund,
        policy_refund_brl=policy_refund,
        parties=parties or (("unknown", None),),
        policy_applied=True,
    )


class PolicyAgent(Specialist):
    """Turns verified specialist findings into a diagnosis and applies the published policy."""

    name = POLICY_AGENT
    tools = frozenset({"get_policy"})

    async def handle(self, task: A2AMessage) -> A2AMessage:
        findings = task.payload
        if task.intent != "decide_policy":
            return task.reply(FAILED, {"reason": "unsupported_intent"})
        order = findings.get("order")
        diagnosis = rules.diagnose(
            order if isinstance(order, rules.OrderFacts) else None,
            findings.get("items") or rules.ItemView(),
            findings.get("payments") or rules.PaymentView(),
            findings.get("refunds") or rules.RefundView(),
            findings.get("shipment") or rules.ShipmentView(),
        )
        if not findings.get("complete", False):
            diagnosis = rules.Diagnosis(
                rules.INSUFFICIENT, rules.ZERO, None, ("specialist_evidence_missing",),
                ("specialist_evidence_missing",),
            )
        record = await self.fetch("get_policy", policy_version=self.ctx.policy_version)
        rule = policy_rule(_data(record), diagnosis.issue, self.ctx.policy_version)
        decision = INSUFFICIENT_DECISION if rule is None else apply_policy(rule, diagnosis)
        if decision.issue != diagnosis.issue:
            diagnosis = rules.Diagnosis(
                rules.INSUFFICIENT, rules.ZERO, None, diagnosis.signals,
                (*diagnosis.doubts, "policy_rule_unavailable"),
            )
        self.ctx.trace.emit(
            case_id=self.ctx.case_id,
            event_type="policy_decided",
            actor=self.name,
            decision_code=decision.issue,
            evidence_refs=_refs(record) or None,
            attributes={
                "case_status": decision.case_status,
                "recommended_action": decision.action,
                "refund_brl": float(decision.refund_brl),
                "policy_version": self.ctx.policy_version,
                "policy_applied": decision.policy_applied,
                "signals": ",".join(diagnosis.signals)[:160],
            },
        )
        payload = {"decision": decision, "diagnosis": diagnosis}
        return task.reply("policy_decided", payload, _refs(record))
