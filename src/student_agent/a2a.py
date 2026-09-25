from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from .trace import TraceWriter

MAX_HOPS = 12
TASK_TIMEOUT_S = 420.0
COORDINATOR = "coordinator"
FAILED = "task_failed"


class A2AProtocolError(RuntimeError):
    pass


@dataclass(frozen=True)
class A2AMessage:
    message_id: str
    case_id: str
    sender: str
    recipient: str
    intent: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()
    hop: int = 0
    reply_to: str | None = None

    def reply(
        self, intent: str, payload: Mapping[str, Any], evidence_refs: Iterable[str] = ()
    ) -> A2AMessage:
        return A2AMessage(
            message_id=f"{self.message_id}-reply",
            case_id=self.case_id,
            sender=self.recipient,
            recipient=self.sender,
            intent=intent,
            payload=payload,
            evidence_refs=tuple(dict.fromkeys(evidence_refs)),
            hop=self.hop + 1,
            reply_to=self.message_id,
        )


class Agent(Protocol):
    name: str

    async def handle(self, task: A2AMessage) -> A2AMessage: ...


class A2ABus:
    def __init__(
        self,
        case_id: str,
        trace: TraceWriter,
        *,
        max_hops: int = MAX_HOPS,
        timeout_s: float = TASK_TIMEOUT_S,
    ) -> None:
        if max_hops < 1:
            raise ValueError("max_hops must be positive")
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        self.case_id = case_id
        self._trace = trace
        self._max_hops = max_hops
        self._timeout_s = timeout_s
        self._agents: dict[str, Agent] = {}
        self._sent = 0
        self._seen: set[tuple[str, str]] = set()

    def register(self, *agents: Agent) -> None:
        for agent in agents:
            self._agents[agent.name] = agent

    async def request(
        self,
        sender: str,
        recipient: str,
        intent: str,
        payload: Mapping[str, Any] | None = None,
        evidence_refs: Iterable[str] = (),
    ) -> A2AMessage:
        if sender != COORDINATOR:
            raise A2AProtocolError("only the coordinator may assign tasks")
        if not intent:
            raise A2AProtocolError("task intent must not be empty")
        agent = self._agents.get(recipient)
        if agent is None:
            raise A2AProtocolError(f"unknown agent {recipient!r}")
        loop_key = (recipient, intent)
        if loop_key in self._seen:
            raise A2AProtocolError(f"loop detected: {recipient} already handled {intent}")
        if self._sent >= self._max_hops:
            raise A2AProtocolError(f"hop budget of {self._max_hops} exhausted")
        self._sent += 1
        self._seen.add(loop_key)
        task = A2AMessage(
            message_id=f"a2a-{self.case_id}-{self._sent:02d}",
            case_id=self.case_id,
            sender=sender,
            recipient=recipient,
            intent=intent,
            payload=payload or {},
            evidence_refs=tuple(dict.fromkeys(evidence_refs)),
            hop=self._sent,
        )
        self._trace.emit(
            case_id=self.case_id,
            event_type="task_assigned",
            actor=sender,
            target=recipient,
            decision_code=intent,
            evidence_refs=list(task.evidence_refs)[:20] or None,
            attributes={"message_id": task.message_id, "hop": task.hop},
        )
        try:
            reply = await asyncio.wait_for(agent.handle(task), self._timeout_s)
            self._check_reply(task, reply)
        except TimeoutError:
            reply = task.reply(FAILED, {"reason": "timeout"})
        except A2AProtocolError:
            reply = task.reply(FAILED, {"reason": "invalid_reply"})
        except Exception as exc:
            reply = task.reply(FAILED, {"reason": f"agent_error:{type(exc).__name__}"})
        attributes: dict[str, str | int] = {
            "message_id": reply.message_id,
            "reply_to": task.message_id,
            "hop": reply.hop,
            "evidence_count": len(reply.evidence_refs),
        }
        if reply.intent == FAILED:
            attributes["failure_reason"] = str(reply.payload.get("reason", "unknown"))[:80]
        self._trace.emit(
            case_id=self.case_id,
            event_type="handoff",
            actor=reply.sender,
            target=reply.recipient,
            decision_code=reply.intent,
            evidence_refs=list(reply.evidence_refs)[:20] or None,
            attributes=attributes,
        )
        return reply

    def _check_reply(self, task: A2AMessage, reply: object) -> None:
        if not isinstance(reply, A2AMessage):
            raise A2AProtocolError("specialist returned a non-A2A reply")
        if reply.case_id != task.case_id or reply.reply_to != task.message_id:
            raise A2AProtocolError("reply is not correlated with its task")
        if reply.sender != task.recipient or reply.recipient != task.sender:
            raise A2AProtocolError("reply routed to the wrong peer")
        if reply.hop != task.hop + 1 or reply.message_id == task.message_id:
            raise A2AProtocolError("reply has an invalid hop or message identifier")
