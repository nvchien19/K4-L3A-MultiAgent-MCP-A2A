from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from student_agent.contracts import ContractError, Contracts

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "contracts" / "schemas"
REQUIRED = {
    "l3a-output-v2.schema.json",
    "l3b-output-v2.schema.json",
    "mcp-evidence-response-v1.schema.json",
    "submission-manifest-v2.schema.json",
    "trace-event-v1.schema.json",
}


def test_packaged_contract_loader_reads_all_public_schemas() -> None:
    contracts = Contracts()

    assert set(contracts._schemas) >= REQUIRED
    contracts.validate_manifest(
        {
            "schema_version": "day09-submission-manifest-v2",
            "competition_id": "day09-multiagent-mcp-a2a",
            "variant_id": "l3a",
            "case_set_version": "contract-test-v1",
            "output_schema_version": "day09-l3a-output-v2",
            "trace_schema_version": "day09-trace-event-v1",
            "generated_at": "2018-01-01T00:00:00Z",
        }
    )


def test_contract_loader_rejects_missing_public_schema(tmp_path: Path) -> None:
    shutil.copy(SCHEMAS / "l3a-output-v2.schema.json", tmp_path)
    with pytest.raises(ContractError, match="missing public schemas"):
        Contracts(tmp_path)


def test_contract_loader_rejects_duplicate_schema_id(tmp_path: Path) -> None:
    for name in REQUIRED:
        shutil.copy(SCHEMAS / name, tmp_path / name)
    first = tmp_path / "l3a-output-v2.schema.json"
    second = tmp_path / "l3b-output-v2.schema.json"
    first_value = json.loads(first.read_text(encoding="utf-8"))
    second_value = json.loads(second.read_text(encoding="utf-8"))
    second_value["$id"] = first_value["$id"]
    second.write_text(json.dumps(second_value), encoding="utf-8")

    with pytest.raises(ContractError, match="duplicate \\$id"):
        Contracts(tmp_path)
