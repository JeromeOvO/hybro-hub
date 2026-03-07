"""Dispatcher — send A2A messages to local agents and translate responses.

Receives relay events, dispatches to local agents via A2A protocol,
and translates streaming responses into HubPublishEvent format for
the relay client to publish back.
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator
from uuid import uuid4

import httpx
from httpx_sse import aconnect_sse

from .agent_registry import LocalAgent

logger = logging.getLogger(__name__)


class DispatchEvent:
    """A translated event ready for relay publishing."""

    def __init__(self, type: str, agent_message_id: str, data: dict[str, Any]) -> None:
        self.type = type
        self.agent_message_id = agent_message_id
        self.data = data

    def to_publish_dict(self) -> dict:
        return {
            "type": self.type,
            "agent_message_id": self.agent_message_id,
            "data": self.data,
        }


class Dispatcher:
    """Dispatches A2A messages to local agents."""

    def __init__(self, timeout: int = 120) -> None:
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def dispatch(
        self,
        agent: LocalAgent,
        message_dict: dict,
        agent_message_id: str,
        user_message_id: str | None = None,
    ) -> list[dict]:
        """Dispatch an A2A message to a local agent and collect publish events.

        Args:
            agent: The target local agent.
            message_dict: Serialized a2a.types.Message dict from RelayToHubEvent.
            agent_message_id: The cloud-assigned agent message ID.
            user_message_id: The originating user message ID.

        Returns:
            List of HubPublishEvent dicts ready for relay.publish().
        """
        events: list[dict] = []
        accumulated_text = ""

        # Emit task_submitted
        events.append(DispatchEvent(
            type="task_submitted",
            agent_message_id=agent_message_id,
            data={"task_id": uuid4().hex, "agent_name": agent.name},
        ).to_publish_dict())

        try:
            if agent.agent_card.get("capabilities", {}).get("streaming"):
                async for chunk in self._dispatch_streaming(agent, message_dict):
                    accumulated_text += chunk
                    events.append(DispatchEvent(
                        type="agent_token",
                        agent_message_id=agent_message_id,
                        data={"token": chunk},
                    ).to_publish_dict())
            else:
                accumulated_text = await self._dispatch_sync(agent, message_dict)

        except Exception as exc:
            logger.error("Dispatch to %s failed: %s", agent.name, exc)
            accumulated_text = f"Error dispatching to agent: {exc}"

        # Emit agent_response
        events.append(DispatchEvent(
            type="agent_response",
            agent_message_id=agent_message_id,
            data={"content": accumulated_text},
        ).to_publish_dict())

        # Emit processing_status
        events.append(DispatchEvent(
            type="processing_status",
            agent_message_id=agent_message_id,
            data={
                "status": "completed",
                "user_message_id": user_message_id,
            },
        ).to_publish_dict())

        return events

    # ──── Sync dispatch (message/send) ────

    async def _dispatch_sync(self, agent: LocalAgent, message_dict: dict) -> str:
        """Send a synchronous A2A message/send request."""
        request_body = self._build_jsonrpc(message_dict, method="message/send")
        client = await self._get_client()

        resp = await client.post(
            agent.url,
            json=request_body,
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        result = resp.json()
        return self._extract_text_from_response(result)

    # ──── Streaming dispatch (message/stream) ────

    async def _dispatch_streaming(
        self, agent: LocalAgent, message_dict: dict
    ) -> AsyncIterator[str]:
        """Send a streaming A2A message/stream request, yield text chunks."""
        request_body = self._build_jsonrpc(message_dict, method="message/stream")
        client = await self._get_client()

        async with aconnect_sse(
            client, "POST", agent.url,
            json=request_body,
            headers={"Content-Type": "application/json"},
        ) as event_source:
            async for sse in event_source.aiter_sse():
                try:
                    data = json.loads(sse.data)
                except (json.JSONDecodeError, TypeError):
                    continue
                text = self._extract_chunk_text(data)
                if text:
                    yield text

    # ──── JSON-RPC construction ────
    # TODO(a2a-v1.0): method names change in v1.0

    @staticmethod
    def _build_jsonrpc(message_dict: dict, method: str) -> dict:
        """Build a JSON-RPC 2.0 envelope for an A2A message."""
        return {
            "jsonrpc": "2.0",
            "id": uuid4().hex,
            "method": method,
            "params": {
                "message": message_dict,
            },
        }

    # ──── Response extraction ────

    @staticmethod
    def _extract_text_from_response(result: dict) -> str:
        """Extract text from a JSON-RPC A2A response."""
        # result could be the JSON-RPC response wrapper
        inner = result.get("result", result)

        # Task response: look in status.message or artifacts
        if "status" in inner:
            msg = inner["status"].get("message", {})
            parts = msg.get("parts", [])
            texts = []
            for p in parts:
                root = p.get("root", p)
                if "text" in root:
                    texts.append(root["text"])
            if texts:
                return "".join(texts)

        if "artifacts" in inner:
            for artifact in inner["artifacts"]:
                for p in artifact.get("parts", []):
                    root = p.get("root", p)
                    if "text" in root:
                        return root["text"]

        # Message response
        if "parts" in inner:
            texts = []
            for p in inner["parts"]:
                root = p.get("root", p)
                if "text" in root:
                    texts.append(root["text"])
            if texts:
                return "".join(texts)

        return str(inner)

    @staticmethod
    def _extract_chunk_text(data: dict) -> str:
        """Extract text from an SSE streaming event.

        Each SSE event may be a full JSON-RPC response with the actual
        event nested under ``result``, or a raw event dict. Handle both.
        """
        inner = data.get("result", data)

        # TaskArtifactUpdateEvent
        if "artifact" in inner:
            for p in inner["artifact"].get("parts", []):
                root = p.get("root", p)
                if "text" in root:
                    return root["text"]

        # TaskStatusUpdateEvent with message
        if "status" in inner:
            msg = inner["status"].get("message", {})
            for p in msg.get("parts", []):
                root = p.get("root", p)
                if "text" in root:
                    return root["text"]

        return ""
