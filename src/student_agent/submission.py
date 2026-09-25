from __future__ import annotations

import json
import re
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from . import OUTPUT_SCHEMA_VERSION, VARIANT_ID
from .cases import CaseSet
from .contracts import Contracts

SECRET_PATTERN = re.compile(r"sk-team-[A-Za-z0-9_-]{8,}")
MAX_FILE_BYTES = 1024 * 1024
MAX_SUBMISSION_BYTES = 12 * 1024 * 1024
REQUIRED_EVENTS = {
    "case_received",
    "task_assigned",
    "handoff",
    "verification_completed",
    "case_finalized",
}
ALLOWED_ACTIONS = {
    "document_no_action",
    "issue_refund",
    "manual_review",
    "monitor_refund",
    "reconcile_payment",
    "refund_duplicate_charge",
    "refund_freight",
    "retry_refund",
}


@dataclass(frozen=True)
class PreparedSubmission:
    manifest: dict[str, Any]
    outputs: dict[str, dict[str, Any]]
    trace_lines: list[str]
    payloads: dict[str, bytes]


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"{path}: invalid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value


def _decimal(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("-1")
    return result if result.is_finite() else Decimal("-1")


def build_manifest(case_set: CaseSet) -> dict[str, Any]:
    return {
        "schema_version": "day09-submission-manifest-v2",
        "competition_id": "day09-multiagent-mcp-a2a",
        "variant_id": VARIANT_ID,
        "case_set_version": case_set.version,
        "output_schema_version": OUTPUT_SCHEMA_VERSION,
        "trace_schema_version": "day09-trace-event-v1",
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "client": {"name": "day09-student-starter", "version": "0.1.0"},
    }


def _validate_output_invariants(case_id: str, output: dict[str, Any]) -> None:
    finance = output["financial_resolution"]
    recommended = _decimal(finance["recommended_refund_brl"])
    lines_total = sum(
        (_decimal(line["amount_brl"]) for line in finance["refund_lines"]),
        Decimal("0.00"),
    )
    if recommended < 0 or lines_total < 0 or abs(recommended - lines_total) > Decimal("0.005"):
        raise ValueError(f"outputs/{case_id}.json: refund total does not match refund lines")
    actions = output["resolution_actions"]
    if len(actions) != 1:
        raise ValueError(f"outputs/{case_id}.json: exactly one resolution action is required")
    action = actions[0]
    if action not in ALLOWED_ACTIONS:
        raise ValueError(f"outputs/{case_id}.json: unknown resolution action {action!r}")
    status = output["assessment"]["case_status"]
    valid_no_refund = {
        "document_no_action": status == "no_action" and recommended == 0,
        "monitor_refund": status == "needs_investigation" and recommended == 0,
        "manual_review": status == "needs_investigation" and recommended == 0,
    }
    if action in valid_no_refund:
        if not valid_no_refund[action]:
            raise ValueError(f"outputs/{case_id}.json: no-refund action is inconsistent")
    elif status != "action_required" or recommended <= 0 or not finance["refund_lines"]:
        raise ValueError(f"outputs/{case_id}.json: refund action is inconsistent")
    issue = output["assessment"]["primary_issue"]
    if issue == "insufficient_evidence" and (
        action != "manual_review" or recommended != 0 or status != "needs_investigation"
    ):
        raise ValueError(f"outputs/{case_id}.json: insufficient evidence is inconsistent")


def _validate_trace(
    trace_lines: list[str],
    expected: set[str],
    contracts: Contracts,
) -> tuple[list[str], dict[str, set[str]]]:
    normalized_lines: list[str] = []
    seen_events: set[str] = set()
    events_by_case: dict[str, list[dict[str, Any]]] = {case_id: [] for case_id in expected}
    refs_by_case: dict[str, set[str]] = {case_id: set() for case_id in expected}
    for number, line in enumerate(trace_lines, 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"traces/trace.jsonl:{number}: invalid JSON") from exc
        if not isinstance(event, dict):
            raise ValueError(f"traces/trace.jsonl:{number}: expected a JSON object")
        contracts.validate_trace(event, f"traces/trace.jsonl:{number}")
        case_id = event["case_id"]
        if case_id not in expected:
            raise ValueError(f"traces/trace.jsonl:{number}: case is outside this case-set")
        if event["event_id"] in seen_events:
            raise ValueError(f"traces/trace.jsonl:{number}: duplicate event_id")
        seen_events.add(event["event_id"])
        events_by_case[case_id].append(event)
        if event["event_type"] == "tool_result_consumed":
            if not event.get("tool_name"):
                raise ValueError(
                    f"traces/trace.jsonl:{number}: consumed evidence is missing tool_name"
                )
            refs_by_case[case_id].update(event.get("evidence_refs", []))
        normalized_lines.append(
            json.dumps(
                event,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
        )

    for case_id, events in events_by_case.items():
        kinds = [event["event_type"] for event in events]
        missing = sorted(REQUIRED_EVENTS - set(kinds))
        if missing:
            raise ValueError(f"trace for {case_id} is missing events: {missing}")
        if kinds.count("case_received") != 1 or kinds[0] != "case_received":
            raise ValueError(f"trace for {case_id} must start with one case_received event")
        if kinds.count("case_finalized") != 1 or kinds[-1] != "case_finalized":
            raise ValueError(f"trace for {case_id} must end with one case_finalized event")
        if kinds.index("verification_completed") >= kinds.index("case_finalized"):
            raise ValueError(f"trace for {case_id} finalized before verification completed")
        if events[0].get("actor") != "coordinator" or events[-1].get("actor") != "coordinator":
            raise ValueError(f"trace for {case_id} has an invalid lifecycle actor")
    return normalized_lines, refs_by_case


def _validate_output_links(
    case_id: str,
    output: dict[str, Any],
    trace_refs: set[str],
) -> None:
    output_refs = set(output["evidence_refs"])
    unknown = sorted(output_refs - trace_refs)
    if unknown:
        raise ValueError(f"outputs/{case_id}.json has evidence absent from its trace: {unknown}")
    if not output_refs:
        assessment = output["assessment"]
        action = output["resolution_actions"][0]
        honest_degraded = (
            assessment["primary_issue"] == "insufficient_evidence"
            and assessment["case_status"] == "needs_investigation"
            and action == "manual_review"
            and output["financial_resolution"]["recommended_refund_brl"] == 0
            and not output["financial_resolution"]["refund_lines"]
            and all(
                claim["verdict"] == "insufficient_evidence" and not claim["evidence_refs"]
                for claim in output.get("claim_assessments", [])
            )
        )
        if not honest_degraded:
            raise ValueError(f"outputs/{case_id}.json must cite evidence or be explicitly degraded")
        return
    for claim in output.get("claim_assessments", []):
        missing = sorted(set(claim["evidence_refs"]) - output_refs)
        if missing:
            raise ValueError(f"outputs/{case_id}.json claim linkage is invalid: {missing}")


def _prepare_submission(
    root: Path, case_set: CaseSet, contracts: Contracts
) -> PreparedSubmission:
    root = root.resolve()
    outputs_root = root / "outputs"
    actual = {path.stem: path for path in outputs_root.glob("*.json") if path.is_file()}
    expected = set(case_set.case_ids)
    if set(actual) != expected:
        missing = sorted(expected - set(actual))
        extra = sorted(set(actual) - expected)
        raise ValueError(f"outputs do not match case-set; missing={missing}, extra={extra}")

    outputs: dict[str, dict[str, Any]] = {}
    for case_id in case_set.case_ids:
        path = actual[case_id]
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError(f"outputs/{case_id}.json exceeds 1 MB")
        output = _json_object(path)
        contracts.validate_output(output, f"outputs/{case_id}.json")
        if output.get("case_id") != case_id:
            raise ValueError(f"outputs/{case_id}.json has a mismatched case_id")
        _validate_output_invariants(case_id, output)
        outputs[case_id] = output

    trace_path = root / "traces" / "trace.jsonl"
    try:
        if trace_path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("traces/trace.jsonl exceeds 1 MB")
        source_lines = trace_path.read_text(encoding="utf-8").splitlines()
    except ValueError:
        raise
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError("traces/trace.jsonl is missing or not UTF-8") from exc
    trace_lines, refs_by_case = _validate_trace(source_lines, expected, contracts)
    for case_id, output in outputs.items():
        _validate_output_links(case_id, output, refs_by_case[case_id])

    manifest = build_manifest(case_set)
    contracts.validate_manifest(manifest)
    payloads = {
        "manifest.json": json.dumps(
            manifest,
            separators=(",", ":"),
            allow_nan=False,
        ).encode(),
        "trace.jsonl": ("\n".join(trace_lines) + ("\n" if trace_lines else "")).encode(),
        **{
            f"outputs/{case_id}.json": json.dumps(
                outputs[case_id],
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
            for case_id in case_set.case_ids
        },
    }
    serialized_parts = [payload.decode("utf-8") for payload in payloads.values()]
    serialized = "\n".join(serialized_parts)
    if SECRET_PATTERN.search(serialized):
        raise ValueError("a Team API Key appears in the submission payload")
    oversized = [name for name, payload in payloads.items() if len(payload) > MAX_FILE_BYTES]
    if oversized:
        raise ValueError(f"submission files exceed 1 MB: {oversized}")
    if sum(map(len, payloads.values())) > MAX_SUBMISSION_BYTES:
        raise ValueError("submission exceeds the 12 MB uncompressed limit")
    return PreparedSubmission(manifest, outputs, trace_lines, payloads)


def validate_artifacts(
    root: Path, case_set: CaseSet, contracts: Contracts
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    prepared = _prepare_submission(root, case_set, contracts)
    return prepared.outputs, prepared.trace_lines


def _safe_destination(root: Path, destination: Path) -> Path:
    root = root.resolve()
    dist = (root / "dist").resolve()
    if not dist.is_relative_to(root):
        raise ValueError("dist must remain inside the repository")
    candidate = destination if destination.is_absolute() else root / destination
    candidate = candidate.resolve()
    if candidate == dist or not candidate.is_relative_to(dist):
        raise ValueError("submission output must be a file inside dist")
    if candidate.suffix.lower() != ".zip":
        raise ValueError("submission output must use the .zip extension")
    if SECRET_PATTERN.search(candidate.name):
        raise ValueError("submission destination contains a Team API Key")
    return candidate


def package_submission(root: Path, destination: Path) -> Path:
    from .cases import load_case_set

    root = root.resolve()
    case_set = load_case_set(root)
    prepared = _prepare_submission(root, case_set, Contracts())
    destination = _safe_destination(root, destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent,
            prefix=f".{destination.stem}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
        with zipfile.ZipFile(temporary_name, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, payload in prepared.payloads.items():
                archive.writestr(name, payload)
        Path(temporary_name).replace(destination)
    except Exception:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)
        raise
    return destination
