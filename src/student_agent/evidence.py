from __future__ import annotations

import copy
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

EVIDENCE_REF_PATTERN = re.compile(r"^ev_[A-Za-z0-9_-]{20,96}$")
RESULT_HASH_PATTERN = re.compile(r"^sha256:[a-f0-9]{64}$")
TOOL_DOMAINS = {
    "get_order": "order",
    "get_order_items": "item",
    "get_order_payments": "payment",
    "get_payment_timeline": "payment",
    "get_refund_timeline": "refund",
    "get_shipment_summary": "shipment",
    "get_sellers": "seller",
    "get_policy": "policy",
}


class EvidenceRejected(ValueError):
    pass


def _empty_payload(data: Any) -> bool:
    if data is None:
        return True
    if isinstance(data, Mapping):
        return not data
    if isinstance(data, (list, tuple, set, str, bytes)):
        return not data
    return False


@dataclass(frozen=True)
class EvidenceRecord:
    tool_name: str
    evidence_ref: str
    result_hash: str
    domain: str
    data: Any
    warnings: tuple[str, ...] = ()


@dataclass
class EvidenceLedger:
    case_id: str
    order_id: str
    _records: list[EvidenceRecord] = field(default_factory=list, init=False, repr=False)
    _by_ref: dict[str, EvidenceRecord] = field(default_factory=dict, init=False, repr=False)
    _latest_by_tool: dict[str, EvidenceRecord] = field(default_factory=dict, init=False, repr=False)
    failures: dict[str, str] = field(default_factory=dict)

    def admit(self, tool_name: str, evidence: Any, actor: str | None = None) -> EvidenceRecord:
        del actor
        expected_domain = TOOL_DOMAINS.get(tool_name)
        if expected_domain is None:
            raise EvidenceRejected("tool_not_allowed")
        if not isinstance(evidence, Mapping):
            raise EvidenceRejected("invalid_envelope")
        if evidence.get("schema_version") != "day09-mcp-evidence-v1":
            raise EvidenceRejected("invalid_schema_version")
        evidence_ref = evidence.get("evidence_ref")
        if not isinstance(evidence_ref, str) or not EVIDENCE_REF_PATTERN.fullmatch(evidence_ref):
            raise EvidenceRejected("invalid_evidence_ref")
        if evidence_ref in self._by_ref:
            raise EvidenceRejected("duplicate_evidence_ref")
        result_hash = evidence.get("result_hash")
        if not isinstance(result_hash, str) or not RESULT_HASH_PATTERN.fullmatch(result_hash):
            raise EvidenceRejected("invalid_result_hash")
        domain = evidence.get("domain")
        if domain != expected_domain:
            raise EvidenceRejected("domain_mismatch")
        if "data" not in evidence:
            raise EvidenceRejected("missing_data")
        warnings = evidence.get("warnings", [])
        if not isinstance(warnings, list) or any(
            not isinstance(warning, str) or not 1 <= len(warning) <= 160 for warning in warnings
        ):
            raise EvidenceRejected("invalid_warnings")
        if len(warnings) > 10 or len(set(warnings)) != len(warnings):
            raise EvidenceRejected("invalid_warnings")
        data = copy.deepcopy(evidence["data"])
        if _empty_payload(data):
            raise EvidenceRejected("empty_data")
        self._validate_scope(data)
        record = EvidenceRecord(
            tool_name=tool_name,
            evidence_ref=evidence_ref,
            result_hash=result_hash,
            domain=domain,
            data=data,
            warnings=tuple(warnings),
        )
        self._records.append(record)
        self._by_ref[evidence_ref] = record
        self._latest_by_tool[tool_name] = record
        return record

    def _validate_scope(self, data: Any) -> None:
        if isinstance(data, Mapping):
            case_id = data.get("case_id")
            if isinstance(case_id, str) and case_id and case_id != self.case_id:
                raise EvidenceRejected("case_mismatch")
            order_id = data.get("order_id")
            if isinstance(order_id, str) and order_id and order_id != self.order_id:
                raise EvidenceRejected("order_mismatch")
            for value in data.values():
                self._validate_scope(value)
        elif isinstance(data, list):
            for value in data:
                self._validate_scope(value)

    def record_failure(self, tool_name: str, outcome: str) -> None:
        self.failures[tool_name] = outcome

    def get(self, tool_name: str) -> EvidenceRecord | None:
        return self._latest_by_tool.get(tool_name)

    def owns(self, evidence_ref: str) -> bool:
        return evidence_ref in self._by_ref

    def refs(self, tools: Iterable[str] | str) -> list[str]:
        names = [tools] if isinstance(tools, str) else tools
        return [
            record.evidence_ref
            for tool_name in names
            for record in self._records
            if record.tool_name == tool_name
        ]

    def __len__(self) -> int:
        return len(self._records)
