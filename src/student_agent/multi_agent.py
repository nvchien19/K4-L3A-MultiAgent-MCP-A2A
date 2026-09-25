from __future__ import annotations

import json
from typing import Any

from .agent_base import BaseAgent
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

TOOL_SYSTEM_PROMPT = """You are an AI Specialist Agent investigating e-commerce claims.
Your job is to analyze the case and gather all necessary evidence using the available tools.
You will be provided with the case details, the tools you can call, and the evidence gathered so far.

Output exactly one JSON object with ONE of the following formats:
To call a tool:
{"action": "call_tool", "tool_name": "<name>", "arguments": {"<arg1>": "<value1>"}}

To finish gathering evidence (when you have enough information to resolve the case):
{"action": "finish"}
"""

VERIFIER_SYSTEM_PROMPT = """You are the Verifier Agent. Your job is to output the final resolution for an e-commerce case.
You will receive the original case and all the evidence gathered.
You MUST output a valid JSON object matching the 'day09-l3a-output-v2' schema.
Ensure all evidence_refs used in your output actually exist in the provided evidence.
"""

class MultiAgentSystem:
    def __init__(self, gateway: EvidenceGateway, trace: TraceWriter):
        self.gateway = gateway
        self.trace = trace
        self.tool_agent = BaseAgent(system_prompt=TOOL_SYSTEM_PROMPT)
        self.verifier_agent = BaseAgent(system_prompt=VERIFIER_SYSTEM_PROMPT)
        
    async def run(self, case: dict[str, Any]) -> dict[str, Any]:
        tools = await self.gateway.list_tools()
        evidence_history = []
        
        # Phase 1: Tool calling loop
        for _ in range(15):  # Max 15 steps to prevent infinite loop
            prompt = f"Case: {json.dumps(case)}\nAvailable tools: {json.dumps(tools)}\nEvidence so far: {json.dumps(evidence_history)}\nWhat is your next action?"
            
            action_response = await self.tool_agent.generate_json(prompt)
            
            if action_response.get("action") == "finish":
                break
            elif action_response.get("action") == "call_tool":
                tool_name = action_response.get("tool_name")
                arguments = action_response.get("arguments", {})
                try:
                    evidence = await self.gateway.call(tool_name, case_id=case["case_id"], **arguments)
                    evidence_history.append(evidence)
                    
                    # Emit trace
                    self.trace.emit(
                        case_id=case["case_id"],
                        event_type="tool_result_consumed",
                        actor="specialist_agent",
                        tool_name=tool_name,
                        evidence_refs=[evidence.get("evidence_ref")]
                    )
                except Exception as e:
                    evidence_history.append({"error": str(e), "tool_name": tool_name, "arguments": arguments})
            else:
                evidence_history.append({"error": "Invalid action format."})

        # Phase 2: Verification and Output Generation
        with open("contracts/schemas/l3a-output-v2.schema.json", "r") as f:
            output_schema = json.load(f)
            
        prompt = f"Case: {json.dumps(case)}\nEvidence gathered: {json.dumps(evidence_history)}\nGenerate the final JSON resolution."
        final_output = await self.verifier_agent.generate_json(prompt, schema=output_schema)
        
        # Ensure schema_version and case_id are exactly as required
        final_output["schema_version"] = "day09-l3a-output-v2"
        final_output["case_id"] = case["case_id"]
        
        return final_output
