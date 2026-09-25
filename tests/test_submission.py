from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from student_agent.contracts import Contracts
from student_agent.submission import package_submission, validate_artifacts

ROOT = Path(__file__).resolve().parents[1]
CASE_IDS = tuple(f"CASE_{index:03d}" for index in range(1, 101))
EVIDENCE_REF = "ev_missing_aaaaaaaaaaaaaaaaaaaa"


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def output(case_id: str) -> dict[str, Any]:
    return {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": "insufficient_evidence",
            "case_status": "needs_investigation",
            "confidence": 0.2,
        },
        "affected_entities": {
            "order_ids": [],
            "item_ids": [],
            "seller_ids": [],
            "payment_references": [],
            "shipment_ids": [],
        },
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": "INSUFFICIENT_EVIDENCE", "rank": 1}],
            "responsible_parties": [{"party_type": "unknown", "party_id": None}],
        },
        "evidence_refs": [],
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 0.0,
            "refund_lines": [],
        },
        "resolution_actions": ["manual_review"],
    }


def event(
    case_id: str,
    suffix: str,
    event_type: str,
    actor: str,
    *,
    target: str | None = None,
    evidence_refs: list[str] | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": "day09-trace-event-v1",
        "event_id": f"evt_{case_id}_{suffix}",
        "case_id": case_id,
        "event_type": event_type,
        "occurred_at": "2018-01-01T00:00:00Z",
        "actor": actor,
    }
    if target is not None:
        value["target"] = target
    if evidence_refs is not None:
        value["evidence_refs"] = evidence_refs
    return value


def build_artifacts(root: Path) -> None:
    write_json(
        root / "case-set.json",
        {
            "case_set_version": "submission-test-v1",
            "variant_id": "l3a",
            "case_ids": list(CASE_IDS),
        },
    )
    for case_id in CASE_IDS:
        write_json(root / "inputs" / f"{case_id}.json", {"case_id": case_id})
        write_json(root / "outputs" / f"{case_id}.json", output(case_id))
    trace_path = root / "traces" / "trace.jsonl"
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for case_id in CASE_IDS:
        values = [
            event(case_id, "received", "case_received", "coordinator"),
            event(
                case_id,
                "assigned",
                "task_assigned",
                "coordinator",
                target="verifier",
            ),
            event(
                case_id,
                "handoff",
                "handoff",
                "verifier",
                target="coordinator",
            ),
            event(
                case_id,
                "verified",
                "verification_completed",
                "verifier",
                target="coordinator",
            ),
            event(case_id, "finalized", "case_finalized", "coordinator"),
        ]
        lines.extend(json.dumps(value, separators=(",", ":")) for value in values)
    trace_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_case_set(root: Path) -> Any:
    from student_agent.cases import load_case_set as loader

    return loader(root)


def test_validate_and_package_exact_artifact_allowlist(tmp_path: Path) -> None:
    build_artifacts(tmp_path)
    contracts = Contracts()
    case_set = load_case_set(tmp_path)
    outputs, trace = validate_artifacts(tmp_path, case_set, contracts)
    destination = package_submission(tmp_path, tmp_path / "dist" / "submission.zip")

    assert len(outputs) == 100
    assert len(trace) == 500
    with zipfile.ZipFile(destination) as archive:
        names = set(archive.namelist())
        assert names == {
            "manifest.json",
            "trace.jsonl",
            *(f"outputs/{case_id}.json" for case_id in CASE_IDS),
        }
        assert all(".." not in name for name in names)


@pytest.mark.parametrize(
    "destination",
    [
        "outputs/case.zip",
        "../outside.zip",
        "dist/submission.tar",
    ],
)
def test_package_rejects_unsafe_destination(tmp_path: Path, destination: str) -> None:
    build_artifacts(tmp_path)
    with pytest.raises(ValueError, match="dist|\\.zip"):
        package_submission(tmp_path, tmp_path / destination)


def test_validate_rejects_output_evidence_missing_from_same_case_trace(tmp_path: Path) -> None:
    build_artifacts(tmp_path)
    target = tmp_path / "outputs" / "CASE_001.json"
    value = output("CASE_001")
    value["evidence_refs"] = [EVIDENCE_REF]
    write_json(target, value)

    with pytest.raises(ValueError, match="absent from its trace"):
        validate_artifacts(tmp_path, load_case_set(tmp_path), Contracts())


def test_validate_rejects_cross_case_evidence_linkage(tmp_path: Path) -> None:
    build_artifacts(tmp_path)
    value = output("CASE_001")
    value["evidence_refs"] = [EVIDENCE_REF]
    write_json(tmp_path / "outputs" / "CASE_001.json", value)
    trace_path = tmp_path / "traces" / "trace.jsonl"
    lines = trace_path.read_text(encoding="utf-8").splitlines()
    other_case = json.loads(lines[5])
    other_case["evidence_refs"] = [EVIDENCE_REF]
    lines[5] = json.dumps(other_case, separators=(",", ":"))
    trace_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="absent from its trace"):
        validate_artifacts(tmp_path, load_case_set(tmp_path), Contracts())


def test_validate_rejects_duplicate_trace_event_id(tmp_path: Path) -> None:
    build_artifacts(tmp_path)
    trace_path = tmp_path / "traces" / "trace.jsonl"
    lines = trace_path.read_text(encoding="utf-8").splitlines()
    second = json.loads(lines[1])
    second["event_id"] = json.loads(lines[0])["event_id"]
    lines[1] = json.dumps(second, separators=(",", ":"))
    trace_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate event_id"):
        validate_artifacts(tmp_path, load_case_set(tmp_path), Contracts())


def test_validate_rejects_unbalanced_refund(tmp_path: Path) -> None:
    build_artifacts(tmp_path)
    value = output("CASE_001")
    value["financial_resolution"]["recommended_refund_brl"] = 10.0
    write_json(tmp_path / "outputs" / "CASE_001.json", value)

    with pytest.raises(ValueError, match="refund total"):
        validate_artifacts(tmp_path, load_case_set(tmp_path), Contracts())
