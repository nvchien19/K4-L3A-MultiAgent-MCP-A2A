from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .cases import load_case_set
from .config import Settings
from .contracts import Contracts
from .mcp_gateway import connect_gateway
from .submission import package_submission, validate_artifacts
from .trace import TraceWriter
from .workflow import solve_case


def _root(value: str) -> Path:
    return Path(value).resolve()


async def _show_tools(root: Path) -> None:
    settings = Settings.load(root)
    contracts = Contracts()
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        catalog = await gateway.discover_tools()
        print(json.dumps([tool.as_dict() for tool in catalog], ensure_ascii=False, indent=2))


async def _run(root: Path, selected_cases: list[str] | None = None) -> None:
    settings = Settings.load(root)
    case_set = load_case_set(root)
    selected = tuple(dict.fromkeys(selected_cases or case_set.case_ids))
    if selected_cases is not None and len(selected) != 1:
        raise ValueError("run accepts exactly one --case value")
    unknown = sorted(set(selected) - set(case_set.case_ids))
    if unknown:
        raise ValueError(f"unknown case IDs: {unknown}")
    contracts = Contracts()
    output_root = root / "outputs"
    trace_root = root / "traces"
    output_root.mkdir(parents=True, exist_ok=True)
    trace_root.mkdir(parents=True, exist_ok=True)
    if selected_cases is None:
        for stale in output_root.glob("*.json"):
            stale.unlink()
        trace_path = trace_root / "trace.jsonl"
        trace_path.unlink(missing_ok=True)
    else:
        trace_path = trace_root / f"{selected[0]}.jsonl"
        trace_path.unlink(missing_ok=True)
    trace = TraceWriter(trace_path, contracts)

    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        discovered_tools = await gateway.list_tools()
        if not discovered_tools:
            raise RuntimeError("MCP Gateway returned no tools")
        for case_id in selected:
            case = case_set.cases[case_id]
            trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
            output = await solve_case(case, gateway, trace)
            contracts.validate_output(output, f"outputs/{case_id}.json")
            if output.get("case_id") != case_id:
                raise ValueError(f"solver returned a mismatched case_id for {case_id}")
            target = output_root / f"{case_id}.json"
            temporary = target.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            temporary.replace(target)
            trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Day09 L3A student workflow")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs", help="validate case-set.json and all 100 inputs")
    commands.add_parser("mcp-tools", help="authenticate and list discovered MCP tools")
    run = commands.add_parser("run", help="run the workflow for all cases or one case")
    run.add_argument("--case", action="append", help="run exactly one case ID")
    commands.add_parser("validate", help="validate outputs and observable trace")
    package = commands.add_parser("package", help="validate and build the submission ZIP")
    package.add_argument("--output", default="dist/submission.zip")
    return result


def main() -> None:
    args = parser().parse_args()
    root = _root(args.root)
    try:
        if args.command == "validate-inputs":
            case_set = load_case_set(root)
            print(
                f"OK: {case_set.variant_id} / {case_set.version} / "
                f"{len(case_set.case_ids)} cases"
            )
        elif args.command == "mcp-tools":
            asyncio.run(_show_tools(root))
        elif args.command == "run":
            asyncio.run(_run(root, args.case))
        elif args.command == "validate":
            case_set = load_case_set(root)
            contracts = Contracts()
            _, trace = validate_artifacts(root, case_set, contracts)
            print(f"OK: {len(case_set.case_ids)} outputs / {len(trace)} trace events")
        elif args.command == "package":
            destination = package_submission(root, root / args.output)
            print(f"OK: {destination}")
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
