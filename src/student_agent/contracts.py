from __future__ import annotations

import json
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError
from referencing import Registry, Resource

from . import VARIANT_ID

REQUIRED_SCHEMAS = {
    "l3a-output-v2.schema.json",
    "l3b-output-v2.schema.json",
    "mcp-evidence-response-v1.schema.json",
    "submission-manifest-v2.schema.json",
    "trace-event-v1.schema.json",
}


class ContractError(ValueError):
    pass


class Contracts:
    def __init__(self, root: Path | Traversable | None = None) -> None:
        if root is None:
            try:
                self.root: Path | Traversable = (
                    files("student_agent.contract_resources") / "schemas"
                )
            except ModuleNotFoundError:
                self.root = Path(__file__).resolve().parents[2] / "contracts" / "schemas"
            names = self._resource_names(self.root)
        else:
            path = Path(root).resolve()
            self.root = path
            names = [item.name for item in path.glob("*.schema.json") if item.is_file()]
        if not names:
            raise ContractError(f"no public schemas found under {self.root}")

        schemas: dict[str, dict[str, Any]] = {}
        registry = Registry()
        schema_ids: set[str] = set()
        for name in sorted(names):
            try:
                value = self._read_schema(self.root / name)
                schema = json.loads(value)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ContractError(f"{name}: invalid UTF-8 JSON") from exc
            if not isinstance(schema, dict):
                raise ContractError(f"{name}: expected a JSON object")
            try:
                Draft202012Validator.check_schema(schema)
            except (SchemaError, TypeError) as exc:
                raise ContractError(f"{name}: invalid JSON Schema") from exc
            schema_id = schema.get("$id")
            if not isinstance(schema_id, str) or not schema_id:
                raise ContractError(f"{name}: missing $id")
            if schema_id in schema_ids:
                raise ContractError(f"{name}: duplicate $id {schema_id!r}")
            schema_ids.add(schema_id)
            schemas[name] = schema
            registry = registry.with_resource(schema_id, Resource.from_contents(schema))

        missing = sorted(REQUIRED_SCHEMAS - schemas.keys())
        if missing:
            raise ContractError(f"missing public schemas: {missing}")
        self._schemas = schemas
        self._registry = registry

    @staticmethod
    def _resource_names(root: Path | Traversable) -> list[str]:
        try:
            return sorted(
                item.name
                for item in root.iterdir()
                if item.is_file() and item.name.endswith(".schema.json")
            )
        except OSError as exc:
            raise ContractError(f"cannot list contracts under {root}") from exc

    @staticmethod
    def _read_schema(path: Path | Traversable) -> str:
        return path.read_text(encoding="utf-8")

    def validate(self, schema_name: str, value: Any, label: str) -> None:
        schema = self._schemas.get(schema_name)
        if schema is None:
            raise ContractError(f"contract not found: {schema_name}")
        validator = Draft202012Validator(
            schema,
            registry=self._registry,
            format_checker=FormatChecker(),
        )
        errors = sorted(validator.iter_errors(value), key=lambda error: list(error.absolute_path))
        if errors:
            error = errors[0]
            location = ".".join(str(part) for part in error.absolute_path) or "$"
            raise ContractError(f"{label}:{location}: {error.message}")

    def validate_output(self, value: Any, label: str) -> None:
        self.validate(f"{VARIANT_ID}-output-v2.schema.json", value, label)

    def validate_trace(self, value: Any, label: str) -> None:
        self.validate("trace-event-v1.schema.json", value, label)

    def validate_manifest(self, value: Any, label: str = "manifest.json") -> None:
        self.validate("submission-manifest-v2.schema.json", value, label)

    def validate_evidence(self, value: Any, label: str = "MCP response") -> None:
        self.validate("mcp-evidence-response-v1.schema.json", value, label)
