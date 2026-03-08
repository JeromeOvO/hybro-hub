"""Hub daemon main loop.

Orchestrates: config loading, relay registration, agent discovery,
SSE subscription, event dispatch, and periodic re-sync.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import Any

from .agent_registry import AgentRegistry
from .config import HubConfig
from .dispatcher import Dispatcher
from .privacy_router import PrivacyRouter
from .relay_client import RelayClient

logger = logging.getLogger(__name__)

HEALTH_CHECK_INTERVAL = 60
RESYNC_INTERVAL = 120


class HubDaemon:
    """The hub daemon orchestrator."""

    def __init__(self, config: HubConfig) -> None:
        self.config = config
        self.relay = RelayClient(
            gateway_url=config.gateway_url,
            hub_id=config.hub_id,
            api_key=config.api_key,
        )
        self.registry = AgentRegistry(config)
        self.dispatcher = Dispatcher()
        self.privacy = PrivacyRouter(
            sensitive_keywords=config.privacy_sensitive_keywords,
            sensitive_patterns=config.privacy_sensitive_patterns,
            default_routing=config.privacy_default_routing,
        )
        self._shutdown_event = asyncio.Event()
        self._last_sync_payload: list[dict] | None = None

    async def run(self) -> None:
        """Main entry point — run the hub daemon."""
        loop = asyncio.get_running_loop()
        try:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, self._signal_shutdown)
        except NotImplementedError:
            signal.signal(signal.SIGINT, lambda *_: self._signal_shutdown())

        try:
            await self._startup()
            await self._event_loop()
        except asyncio.CancelledError:
            pass
        finally:
            await self._shutdown()

    # ──── Startup ────

    async def _startup(self) -> None:
        logger.info("Starting hub daemon (hub_id=%s)", self.config.hub_id)

        # Register with relay
        await self.relay.register()

        # Discover local agents
        agents = await self.registry.discover()
        if not agents:
            logger.warning(
                "No local agents found. Start an A2A agent and it will be "
                "discovered automatically."
            )

        # Sync agents to cloud
        await self._sync_agents()

        logger.info(
            "Hub ready — %d agent(s) synced. Waiting for messages...",
            len(self.registry.get_healthy_agents()),
        )

    # ──── Event loop ────

    async def _event_loop(self) -> None:
        """Subscribe to relay SSE and dispatch events."""
        # Start background tasks
        health_task = asyncio.create_task(self._health_check_loop())
        resync_task = asyncio.create_task(self._resync_loop())

        try:
            async for event in self.relay.subscribe():
                if self._shutdown_event.is_set():
                    break
                try:
                    await self._handle_event(event)
                except Exception:
                    logger.exception("Failed to handle relay event")
        finally:
            health_task.cancel()
            resync_task.cancel()
            await asyncio.gather(health_task, resync_task, return_exceptions=True)

    async def _handle_event(self, event: dict[str, Any]) -> None:
        """Handle a single user_message relay event."""
        local_agent_id = event.get("local_agent_id")
        room_id = event.get("room_id")
        agent_message_id = event.get("agent_message_id")
        user_message_id = event.get("user_message_id")
        message_dict = event.get("message")

        if not all([local_agent_id, room_id, agent_message_id, message_dict]):
            logger.warning("Incomplete relay event: %s", event)
            return

        agent = self.registry.get_agent(local_agent_id)
        if not agent:
            logger.error("No agent found for local_agent_id=%s", local_agent_id)
            return

        # Privacy classification (log only in Phase 2b)
        text = _extract_text(message_dict)
        self.privacy.check_and_log(text, agent.name)

        # Dispatch to local agent
        logger.info(
            "Dispatching to %s (room=%s, msg=%s)",
            agent.name, room_id, agent_message_id[:8],
        )
        publish_events = await self.dispatcher.dispatch(
            agent=agent,
            message_dict=message_dict,
            agent_message_id=agent_message_id,
            user_message_id=user_message_id,
        )

        # Publish results back to relay
        await self.relay.publish(room_id, publish_events)

    # ──── Background tasks ────

    async def _health_check_loop(self) -> None:
        while not self._shutdown_event.is_set():
            await asyncio.sleep(HEALTH_CHECK_INTERVAL)
            try:
                await self.registry.health_check()
            except Exception:
                logger.exception("Health check failed")

    async def _resync_loop(self) -> None:
        while not self._shutdown_event.is_set():
            await asyncio.sleep(RESYNC_INTERVAL)
            try:
                await self.registry.discover()
                await self._sync_agents()
            except Exception:
                logger.exception("Agent re-sync failed")

    async def _sync_agents(self) -> None:
        payload = self.registry.to_sync_payload()
        if payload == self._last_sync_payload:
            return
        synced = await self.relay.sync_agents(payload)
        self._last_sync_payload = payload
        logger.info("Synced %d agents to cloud", len(synced))

    # ──── Shutdown ────

    def _signal_shutdown(self) -> None:
        logger.info("Shutdown signal received")
        self._shutdown_event.set()

    async def _shutdown(self) -> None:
        logger.info("Shutting down hub daemon...")
        await self.relay.close()
        await self.registry.close()
        await self.dispatcher.close()
        logger.info("Hub daemon stopped.")


def _extract_text(message_dict: dict) -> str:
    """Extract text from an A2A Message dict."""
    parts = message_dict.get("parts", [])
    texts = []
    for p in parts:
        root = p.get("root", p)
        if "text" in root:
            texts.append(root["text"])
    return " ".join(texts)
