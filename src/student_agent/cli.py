from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from contextlib import suppress
from pathlib import Path

from .cases import load_case_set
from .config import Settings
from .contracts import Contracts
from .mcp_gateway import GatewayPool, connect_gateway
from .submission import package_submission, validate_artifacts
from .trace import TraceWriter
from .workflow import solve_case


def _root(value: str) -> Path:
    return Path(value).resolve()


async def _show_tools(root: Path) -> None:
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        for tool in await gateway.list_tools():
            print(tool)


async def _solve_case_with_retries(
    case: dict,
    gateway: "GatewayPool",
    trace: TraceWriter,
) -> tuple[dict | None, Exception | None]:
    last_error: Exception | None = None
    for attempt in range(2):
        conn = None
        try:
            conn = await gateway.acquire()
            tools = await conn.list_tools()
            if not tools:
                raise RuntimeError("MCP Gateway returned no tools")
            output = await solve_case(case, conn, trace)
            await gateway.release(conn)
            return output, None
        except (Exception, asyncio.CancelledError) as exc:
            last_error = exc
            if conn is not None:
                with suppress(BaseException):
                    await conn.close()
                await gateway.discard_and_replace()
            await asyncio.sleep(1.0 * (attempt + 1))
    return None, last_error


async def _run(root: Path) -> None:
    settings = Settings.load(root)
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    output_root = root / "outputs"
    trace_path = root / "traces" / "trace.jsonl"
    output_root.mkdir(parents=True, exist_ok=True)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    for stale in output_root.glob("*.json"):
        stale.unlink()
    trace_path.unlink(missing_ok=True)
    trace = TraceWriter(trace_path, contracts)

    concurrency = int(os.getenv("RUN_CONCURRENCY", "10"))
    print(f"Running {len(case_set.case_ids)} cases with concurrency={concurrency}")
    pool = GatewayPool(
        concurrency, settings.mcp_endpoint, settings.team_api_key, contracts
    )
    await pool.open()

    async def handle_one(case_id: str) -> str | None:
        case = case_set.cases[case_id]
        trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
        output, last_error = await _solve_case_with_retries(case, pool, trace)
        if output is None:
            print(f"[{case_id}] FAILED: {last_error}", file=sys.stderr)
            return case_id
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
        print(f"[{case_id}] done")
        return None

    try:
        results = await asyncio.gather(
            *(handle_one(case_id) for case_id in case_set.case_ids)
        )
        failed = [case_id for case_id in results if case_id]
    finally:
        await pool.close()

    if failed:
        print(f"COMPLETED with {len(failed)} failed cases: {failed}", file=sys.stderr)
    else:
        print(f"COMPLETED all {len(case_set.case_ids)} cases")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Day09 L3A student workflow")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs", help="validate case-set.json and all 100 inputs")
    commands.add_parser("mcp-tools", help="authenticate and list discovered MCP tools")
    commands.add_parser("run", help="run the implemented workflow for all cases")
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
            asyncio.run(_run(root))
        elif args.command == "validate":
            case_set = load_case_set(root)
            contracts = Contracts(root / "contracts" / "schemas")
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
