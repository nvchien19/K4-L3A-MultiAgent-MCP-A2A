from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from student_agent.a2a import FAILED, A2ABus, A2AMessage, A2AProtocolError
from student_agent.contracts import Contracts
from student_agent.evidence import EvidenceLedger, EvidenceRejected
from student_agent.trace import TraceWriter

ROOT = Path(__file__).resolve().parents[1]


class EchoAgent:
    name = "echo-agent"

    def __init__(self, *, valid: bool = True) -> None:
        self.valid = valid
        self.calls = 0

    async def handle(self, task: A2AMessage) -> A2AMessage:
        self.calls += 1
        if not self.valid:
            return A2AMessage(
                message_id="invalid",
                case_id=task.case_id,
                sender=task.recipient,
                recipient=task.sender,
                intent="invalid",
                hop=task.hop + 1,
            )
        return task.reply("echo_done", {"value": 1})


def make_trace(tmp_path: Path) -> TraceWriter:
    return TraceWriter(tmp_path / "trace.jsonl", Contracts(ROOT / "contracts" / "schemas"))


def test_bus_only_accepts_coordinator_tasks(tmp_path: Path) -> None:
    bus = A2ABus("L3A_CASE_001", make_trace(tmp_path))
    bus.register(EchoAgent())

    with pytest.raises(A2AProtocolError, match="coordinator"):
        asyncio.run(bus.request("echo-agent", "echo-agent", "do_work"))


def test_bus_records_correlated_handoff(tmp_path: Path) -> None:
    trace = make_trace(tmp_path)
    bus = A2ABus("L3A_CASE_001", trace)
    agent = EchoAgent()
    bus.register(agent)
    reply = asyncio.run(bus.request("coordinator", "echo-agent", "do_work"))

    assert reply.intent == "echo_done"
    assert reply.reply_to == "a2a-L3A_CASE_001-01"
    assert agent.calls == 1
    events = trace.path.read_text(encoding="utf-8").splitlines()
    assert len(events) == 2
    assert "task_assigned" in events[0]
    assert "handoff" in events[1]


def test_bus_blocks_repeated_task_loop(tmp_path: Path) -> None:
    bus = A2ABus("L3A_CASE_001", make_trace(tmp_path))
    bus.register(EchoAgent())
    asyncio.run(bus.request("coordinator", "echo-agent", "do_work"))

    with pytest.raises(A2AProtocolError, match="loop"):
        asyncio.run(bus.request("coordinator", "echo-agent", "do_work"))


def test_bus_converts_invalid_reply_to_failure(tmp_path: Path) -> None:
    bus = A2ABus("L3A_CASE_001", make_trace(tmp_path))
    bus.register(EchoAgent(valid=False))
    reply = asyncio.run(bus.request("coordinator", "echo-agent", "do_work"))

    assert reply.intent == FAILED
    assert reply.payload == {"reason": "invalid_reply"}


def envelope(*, case_id: str = "L3A_CASE_001", order_id: str = "ORD-001") -> dict[str, object]:
    return {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": "ev_order_aaaaaaaaaaaaaaaaaaaa",
        "result_hash": "sha256:" + "a" * 64,
        "domain": "order",
        "data": {"case_id": case_id, "order_id": order_id},
    }


def test_ledger_admits_same_case_evidence() -> None:
    ledger = EvidenceLedger("L3A_CASE_001", "ORD-001")
    record = ledger.admit("get_order", envelope())

    assert record.evidence_ref in ledger.refs("get_order")
    assert ledger.owns(record.evidence_ref)
    assert len(ledger) == 1


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (envelope(case_id="L3A_CASE_002"), "case_mismatch"),
        (envelope(order_id="ORD-002"), "order_mismatch"),
        ({**envelope(), "data": {}}, "empty_data"),
    ],
)
def test_ledger_rejects_out_of_scope_or_empty_data(value: object, reason: str) -> None:
    ledger = EvidenceLedger("L3A_CASE_001", "ORD-001")
    with pytest.raises(EvidenceRejected, match=reason):
        ledger.admit("get_order", value)


def test_ledger_rejects_duplicate_evidence_reference() -> None:
    ledger = EvidenceLedger("L3A_CASE_001", "ORD-001")
    ledger.admit("get_order", envelope())
    with pytest.raises(EvidenceRejected, match="duplicate_evidence_ref"):
        ledger.admit("get_order", envelope())
