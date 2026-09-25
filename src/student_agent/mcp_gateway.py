from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .contracts import Contracts


class EvidenceGateway:
    def __init__(self, contracts: Contracts) -> None:
        self._contracts = contracts
        self._endpoint: str | None = None
        self._headers: dict[str, str] | None = None
        self._timeout: httpx2.Timeout | None = None
        self._http: httpx2.AsyncClient | None = None
        self._read: Any = None
        self._write: Any = None
        self._session: ClientSession | None = None
        self._mcp_cm: Any = None
        self._session_cm: Any = None

    async def open(self, endpoint: str, team_api_key: str) -> None:
        self._endpoint = endpoint
        self._headers = {"Authorization": f"Bearer {team_api_key}"}
        self._timeout = httpx2.Timeout(300.0, connect=30.0, write=30.0, pool=30.0)
        await self._open_stream()

    async def _open_stream(self) -> None:
        self._http = httpx2.AsyncClient(headers=self._headers, timeout=self._timeout)
        self._mcp_cm = streamable_http_client(
            self._endpoint, http_client=self._http
        )
        self._read, self._write = await self._mcp_cm.__aenter__()
        self._session_cm = ClientSession(self._read, self._write)
        self._session = await self._session_cm.__aenter__()
        await self._session.initialize()

    async def close(self) -> None:
        for ctx in ("_session_cm", "_mcp_cm"):
            cm = getattr(self, ctx)
            if cm is not None:
                with suppress(BaseException):
                    await cm.__aexit__(None, None, None)
                setattr(self, ctx, None)
        if self._http is not None:
            with suppress(BaseException):
                await self._http.aclose()
            self._http = None
        self._session = None

    async def reconnect(self) -> None:
        await self.close()
        await self._open_stream()

    async def list_tools(self) -> list[str]:
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                assert self._session is not None
                async with asyncio.timeout(120):
                    response = await self._session.list_tools()
                return sorted(tool.name for tool in response.tools)
            except (httpx2.HTTPError, asyncio.CancelledError, TimeoutError) as exc:
                last_exc = exc
                await asyncio.sleep(0.5 * (attempt + 1))
                with suppress(Exception):
                    await self.reconnect()
        raise RuntimeError(f"MCP list_tools failed: {last_exc}")

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                return await self._do_call(tool_name, case_id=case_id, **arguments)
            except (httpx2.HTTPError, asyncio.CancelledError, TimeoutError) as exc:
                last_exc = exc
                await asyncio.sleep(0.5 * (attempt + 1))
                with suppress(Exception):
                    await self.reconnect()
        raise RuntimeError(f"MCP transport failed for {tool_name}: {last_exc}")

    async def _do_call(
        self, tool_name: str, *, case_id: str, **arguments: str
    ) -> dict[str, Any]:
        assert self._session is not None
        payload = {"case_id": case_id, **arguments}
        async with asyncio.timeout(120):
            result = await self._session.call_tool(tool_name, arguments=payload)
        try:
            is_error = getattr(result, "isError", None)
            if is_error is None:
                is_error = getattr(result, "is_error", False)
        except Exception:
            is_error = False
        if is_error:
            message = " ".join(
                block.text for block in result.content if getattr(block, "text", None)
            )
            raise RuntimeError(f"MCP tool {tool_name} failed: {message or 'unknown error'}")
        evidence = getattr(result, "structuredContent", None)
        if evidence is None:
            evidence = getattr(result, "structured_content", None)
        if evidence is None:
            text_blocks = [block.text for block in result.content if getattr(block, "text", None)]
            if len(text_blocks) != 1:
                raise ValueError(f"MCP tool {tool_name} did not return one evidence object")
            evidence = json.loads(text_blocks[0])
        self._contracts.validate_evidence(evidence, f"MCP tool {tool_name}")
        return evidence


@asynccontextmanager
async def connect_gateway(
    endpoint: str, team_api_key: str, contracts: Contracts, retries: int = 6
) -> AsyncIterator[EvidenceGateway]:
    gateway = EvidenceGateway(contracts)
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            await gateway.open(endpoint, team_api_key)
            break
        except (Exception, asyncio.CancelledError) as exc:
            last_exc = exc
            await asyncio.sleep(5.0 * (attempt + 1))
    else:
        raise RuntimeError(f"MCP connection failed after {retries} attempts: {last_exc}")
    try:
        yield gateway
    finally:
        await gateway.close()


class GatewayPool:
    """Bounded pool of open EvidenceGateway connections for concurrent cases.

    Each case borrows one gateway for its whole solve; a failed gateway is
    discarded and replaced to keep the pool at its configured size.
    """

    def __init__(
        self,
        size: int,
        endpoint: str,
        team_api_key: str,
        contracts: Contracts,
    ) -> None:
        self._spec = endpoint, team_api_key, contracts
        self._size = max(1, size)
        self._queue: asyncio.Queue[EvidenceGateway] = asyncio.Queue()

    async def open(self) -> None:
        await asyncio.gather(*(self._spawn() for _ in range(self._size)))

    async def _spawn(self) -> None:
        endpoint, team_api_key, contracts = self._spec
        gateway = EvidenceGateway(contracts)
        await gateway.open(endpoint, team_api_key)
        await self._queue.put(gateway)

    async def acquire(self) -> EvidenceGateway:
        return await self._queue.get()

    async def release(self, gateway: EvidenceGateway) -> None:
        await self._queue.put(gateway)

    async def discard_and_replace(self) -> None:
        await self._spawn()

    async def close(self) -> None:
        while True:
            try:
                gateway = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            with suppress(BaseException):
                await gateway.close()


async def open_gateway_pool(
    size: int, endpoint: str, team_api_key: str, contracts: Contracts
) -> GatewayPool:
    pool = GatewayPool(size, endpoint, team_api_key, contracts)
    await pool.open()
    return pool