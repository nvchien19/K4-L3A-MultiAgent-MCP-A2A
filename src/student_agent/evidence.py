"""Per-case ledger of validated MCP evidence.

The ledger is created inside ``solve_case`` and dropped when the case is finished, so an
``evidence_ref`` can never leak into another case. Refs are stored exactly as the gateway returned
them; the ledger never builds, edits or re-orders a ref string.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

# Domain each discovered tool must report; a mismatch means the envelope cannot be trusted.
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
    """The envelope is valid JSON but cannot be used for this case."""


@dataclass(frozen=True)
class EvidenceRecord:
    tool_name: str
    evidence_ref: str
    result_hash: str
    domain: str
    data: Any
    warnings: tuple[str, ...]
    consumed_by: str


@dataclass
class EvidenceLedger:
    case_id: str
    order_id: str
    _records: dict[str, EvidenceRecord] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)

    def admit(self, tool_name: str, evidence: Mapping[str, Any], actor: str) -> EvidenceRecord:
        """Store a gateway envelope (already schema-validated) after case-scope checks."""
        expected = TOOL_DOMAINS.get(tool_name)
        domain = evidence.get("domain")
        if expected is None or domain != expected:
            raise EvidenceRejected(f"{tool_name} returned domain {domain!r}, expected {expected!r}")
        ref = evidence.get("evidence_ref")
        if not isinstance(ref, str) or self.owns(ref):
            raise EvidenceRejected(f"{tool_name} returned a missing or repeated evidence_ref")
        data = evidence.get("data")
        if isinstance(data, Mapping) and data.get("order_id") not in (None, self.order_id):
            raise EvidenceRejected(f"{tool_name} returned evidence for another order")
        record = EvidenceRecord(
            tool_name=tool_name,
            evidence_ref=ref,
            result_hash=str(evidence.get("result_hash", "")),
            domain=domain,
            data=data,
            warnings=tuple(evidence.get("warnings") or ()),
            consumed_by=actor,
        )
        self._records[tool_name] = record
        self.failures.pop(tool_name, None)
        return record

    def record_failure(self, tool_name: str, outcome: str) -> None:
        self.failures[tool_name] = outcome

    def get(self, tool_name: str) -> EvidenceRecord | None:
        return self._records.get(tool_name)

    def data(self, tool_name: str) -> Any:
        record = self._records.get(tool_name)
        return None if record is None else record.data

    def owns(self, ref: str) -> bool:
        return any(record.evidence_ref == ref for record in self._records.values())

    def refs(self, tool_names: Iterable[str]) -> list[str]:
        """Refs for the given tools in the given order, skipping tools without evidence."""
        return [
            record.evidence_ref
            for name in dict.fromkeys(tool_names)
            if (record := self._records.get(name)) is not None
        ]

    @property
    def tools(self) -> list[str]:
        return list(self._records)
