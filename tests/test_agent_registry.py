"""Tests for hub.agent_registry."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from hub.agent_registry import (
    AgentRegistry,
    LocalAgent,
    _extract_capabilities,
    _get_listening_ports,
)
from hub.config import HubConfig, LocalAgentConfig


@pytest.fixture
def config():
    return HubConfig(
        api_key="test",
        agents=[
            LocalAgentConfig(name="Test Agent", url="http://localhost:9001"),
        ],
        auto_discover=False,
    )


@pytest.fixture
def config_autodiscover():
    return HubConfig(
        api_key="test",
        auto_discover=True,
    )


SAMPLE_CARD = {
    "name": "Sample Agent",
    "description": "A test agent",
    "url": "http://localhost:9001/",
    "version": "1.0.0",
    "capabilities": {"streaming": True},
    "skills": [{"id": "s1", "name": "Skill", "tags": ["chat"]}],
}


class TestDiscovery:
    @pytest.mark.asyncio
    async def test_discover_manual_agent(self, config):
        registry = AgentRegistry(config)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = SAMPLE_CARD

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        registry._client = mock_client

        agents = await registry.discover()
        assert len(agents) == 1
        assert agents[0].name == "Test Agent"
        assert agents[0].agent_card == SAMPLE_CARD
        await registry.close()

    @pytest.mark.asyncio
    async def test_discover_unreachable_agent(self, config):
        registry = AgentRegistry(config)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
        registry._client = mock_client

        agents = await registry.discover()
        assert len(agents) == 0
        await registry.close()

    @pytest.mark.asyncio
    async def test_auto_discover(self, config_autodiscover):
        registry = AgentRegistry(config_autodiscover)

        async def mock_get(url, **kwargs):
            if "9001" in url and "agent-card.json" in url:
                resp = MagicMock()
                resp.status_code = 200
                resp.json.return_value = SAMPLE_CARD
                return resp
            raise httpx.ConnectError("refused")

        mock_client = AsyncMock()
        mock_client.get = mock_get
        registry._client = mock_client

        with patch(
            "hub.agent_registry._get_listening_ports",
            return_value=[9001, 9002, 9003],
        ):
            agents = await registry.discover()

        assert len(agents) == 1
        assert agents[0].url == "http://localhost:9001"
        await registry.close()

    @pytest.mark.asyncio
    async def test_auto_discover_no_ports(self, config_autodiscover):
        """When no ports are listening, auto-discovery finds nothing."""
        registry = AgentRegistry(config_autodiscover)

        with patch(
            "hub.agent_registry._get_listening_ports",
            return_value=[],
        ):
            agents = await registry.discover()

        assert len(agents) == 0
        await registry.close()

    @pytest.mark.asyncio
    async def test_discover_fallback_to_second_path(self, config):
        """Agent only serves at /.well-known/agent.json (deprecated path)."""
        registry = AgentRegistry(config)

        async def mock_get(url, **kwargs):
            if "agent.json" in url and "agent-card" not in url:
                resp = MagicMock()
                resp.status_code = 200
                resp.json.return_value = SAMPLE_CARD
                return resp
            resp = MagicMock()
            resp.status_code = 404
            return resp

        mock_client = AsyncMock()
        mock_client.get = mock_get
        registry._client = mock_client

        agents = await registry.discover()
        assert len(agents) == 1
        assert agents[0].agent_card == SAMPLE_CARD
        await registry.close()


class TestSyncPayload:
    @pytest.mark.asyncio
    async def test_to_sync_payload(self, config):
        registry = AgentRegistry(config)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = SAMPLE_CARD
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        registry._client = mock_client

        await registry.discover()
        payload = registry.to_sync_payload()
        assert len(payload) == 1
        assert payload[0]["name"] == "Test Agent"
        assert payload[0]["agent_card"] == SAMPLE_CARD
        assert "local_agent_id" in payload[0]
        await registry.close()


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_health_check_marks_unhealthy(self, config):
        registry = AgentRegistry(config)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = SAMPLE_CARD
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        registry._client = mock_client
        await registry.discover()
        assert len(registry.get_healthy_agents()) == 1

        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("down"))
        await registry.health_check()
        assert len(registry.get_healthy_agents()) == 0
        await registry.close()


class TestGetListeningPorts:
    def test_returns_sorted_ports(self):
        """Mock psutil to verify port enumeration logic."""
        mock_conn_1 = MagicMock()
        mock_conn_1.status = "LISTEN"
        mock_conn_1.laddr = ("127.0.0.1", 8080)

        mock_conn_2 = MagicMock()
        mock_conn_2.status = "LISTEN"
        mock_conn_2.laddr = ("0.0.0.0", 3000)

        mock_conn_3 = MagicMock()
        mock_conn_3.status = "ESTABLISHED"
        mock_conn_3.laddr = ("127.0.0.1", 9999)

        mock_conn_4 = MagicMock()
        mock_conn_4.status = "LISTEN"
        mock_conn_4.laddr = ("192.168.1.5", 5000)

        mock_psutil = MagicMock()
        mock_psutil.CONN_LISTEN = "LISTEN"
        mock_psutil.net_connections.return_value = [
            mock_conn_1, mock_conn_2, mock_conn_3, mock_conn_4,
        ]

        with patch.dict("sys.modules", {"psutil": mock_psutil}):
            ports = _get_listening_ports()

        assert ports == [3000, 8080]

    def test_excludes_specified_ports(self):
        mock_conn_1 = MagicMock()
        mock_conn_1.status = "LISTEN"
        mock_conn_1.laddr = ("127.0.0.1", 22)

        mock_conn_2 = MagicMock()
        mock_conn_2.status = "LISTEN"
        mock_conn_2.laddr = ("127.0.0.1", 9001)

        mock_psutil = MagicMock()
        mock_psutil.CONN_LISTEN = "LISTEN"
        mock_psutil.net_connections.return_value = [mock_conn_1, mock_conn_2]

        with patch.dict("sys.modules", {"psutil": mock_psutil}):
            ports = _get_listening_ports(exclude={22})

        assert ports == [9001]


class TestExtractCapabilities:
    def test_extracts_streaming(self):
        caps = _extract_capabilities({"capabilities": {"streaming": True}})
        assert "streaming" in caps

    def test_extracts_skill_tags(self):
        caps = _extract_capabilities(
            {"skills": [{"tags": ["code", "review"]}]}
        )
        assert "code" in caps
        assert "review" in caps

    def test_empty_card(self):
        assert _extract_capabilities({}) == []
