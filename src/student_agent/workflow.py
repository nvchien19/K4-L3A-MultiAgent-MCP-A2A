from __future__ import annotations

from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    from .multi_agent import MultiAgentSystem
    system = MultiAgentSystem(gateway, trace)
    return await system.run(case)
