"""Tests for hub.relay_client."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from hub.relay_client import RelayClient


@pytest.fixture
def relay():
    return RelayClient(
        gateway_url="https://api.hybro.ai",
        hub_id="hub-123",
        api_key="hba_test",
    )


def _attach_mock_client(relay, mock_client):
    """Wire mock_client into both _http_client and _sse_client slots."""
    relay._http_client = mock_client
    relay._sse_client = mock_client


def _make_mock_resp(status_code=200, json_data=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.raise_for_status = MagicMock()
    if json_data is not None:
        resp.json.return_value = json_data
    return resp


class TestRegister:
    @pytest.mark.asyncio
    async def test_register_success(self, relay):
        mock_resp = _make_mock_resp(200, {"hub_id": "hub-123", "user_id": "user-1"})
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.is_closed = False
        _attach_mock_client(relay, mock_client)

        result = await relay.register()
        assert result["hub_id"] == "hub-123"
        mock_client.post.assert_called_once()
        call_kwargs = mock_client.post.call_args
        assert "X-API-Key" in call_kwargs[1]["headers"]


class TestSyncAgents:
    @pytest.mark.asyncio
    async def test_sync_agents(self, relay):
        mock_resp = _make_mock_resp(200, {"synced": [{"agent_id": "a1", "local_agent_id": "l1"}]})
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.is_closed = False
        _attach_mock_client(relay, mock_client)

        synced = await relay.sync_agents([{"local_agent_id": "l1", "name": "Test"}])
        assert len(synced) == 1
        assert synced[0]["agent_id"] == "a1"


class TestDoPublish:
    @pytest.mark.asyncio
    async def test_do_publish_success(self, relay):
        relay._connection_token = "jwt-token"
        mock_resp = _make_mock_resp(204)
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.is_closed = False
        _attach_mock_client(relay, mock_client)

        ok = await relay._do_publish("room-1", [{"type": "test"}])
        assert ok is True
        call_kwargs = mock_client.post.call_args
        assert "Bearer jwt-token" in call_kwargs[1]["headers"]["Authorization"]

    @pytest.mark.asyncio
    async def test_do_publish_403_returns_false(self, relay):
        relay._connection_token = "expired-token"
        mock_resp = _make_mock_resp(403)
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.is_closed = False
        _attach_mock_client(relay, mock_client)

        ok = await relay._do_publish("room-1", [])
        assert ok is False
        assert relay._connection_token is None


class TestPublish:
    @pytest.mark.asyncio
    async def test_publish_success(self, relay):
        relay._connection_token = "jwt-token"
        mock_resp = _make_mock_resp(204)
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.is_closed = False
        _attach_mock_client(relay, mock_client)

        await relay.publish("room-1", [{"type": "agent_response", "agent_message_id": "m1", "data": {}}])
        mock_client.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_publish_no_token_queues_for_retry(self, relay):
        relay._connection_token = None
        events = [{"type": "agent_response"}]
        await relay.publish("room-1", events)
        assert len(relay._retry_queue) == 1
        assert relay._retry_queue[0] == ("room-1", events)

    @pytest.mark.asyncio
    async def test_publish_403_queues_for_retry(self, relay):
        relay._connection_token = "expired-token"
        mock_resp = _make_mock_resp(403)
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.is_closed = False
        _attach_mock_client(relay, mock_client)

        events = [{"type": "agent_response"}]
        await relay.publish("room-1", events)
        assert relay._connection_token is None
        assert len(relay._retry_queue) == 1

    @pytest.mark.asyncio
    async def test_publish_network_error_queues_for_retry(self, relay):
        relay._connection_token = "jwt-token"
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
        mock_client.is_closed = False
        _attach_mock_client(relay, mock_client)

        events = [{"type": "agent_response"}]
        await relay.publish("room-1", events)
        assert len(relay._retry_queue) == 1


class TestFlushRetryQueue:
    @pytest.mark.asyncio
    async def test_flush_all_success(self, relay):
        relay._retry_queue.append(("room-1", [{"type": "test"}]))
        relay._retry_queue.append(("room-2", [{"type": "test2"}]))
        relay._connection_token = "fresh-token"

        mock_resp = _make_mock_resp(204)
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.is_closed = False
        _attach_mock_client(relay, mock_client)

        await relay._flush_retry_queue()
        assert len(relay._retry_queue) == 0
        assert mock_client.post.call_count == 2

    @pytest.mark.asyncio
    async def test_flush_stops_on_403_and_requeues_remaining(self, relay):
        """If 403 hits mid-flush, the failed item + remaining are re-queued."""
        relay._retry_queue.append(("room-1", [{"type": "ev1"}]))
        relay._retry_queue.append(("room-2", [{"type": "ev2"}]))
        relay._retry_queue.append(("room-3", [{"type": "ev3"}]))
        relay._connection_token = "fresh-token"

        ok_resp = _make_mock_resp(204)
        fail_resp = _make_mock_resp(403)
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=[ok_resp, fail_resp])
        mock_client.is_closed = False
        _attach_mock_client(relay, mock_client)

        await relay._flush_retry_queue()
        assert relay._connection_token is None
        assert len(relay._retry_queue) == 2
        rooms = [r for r, _ in relay._retry_queue]
        assert rooms == ["room-2", "room-3"]

    @pytest.mark.asyncio
    async def test_flush_does_not_infinite_loop(self, relay):
        """Regression: flush must terminate even when all publishes 403."""
        relay._retry_queue.append(("room-1", [{"type": "ev1"}]))
        relay._retry_queue.append(("room-2", [{"type": "ev2"}]))
        relay._connection_token = "fresh-token"

        fail_resp = _make_mock_resp(403)
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=fail_resp)
        mock_client.is_closed = False
        _attach_mock_client(relay, mock_client)

        await relay._flush_retry_queue()
        assert mock_client.post.call_count == 1
        assert len(relay._retry_queue) == 2

    @pytest.mark.asyncio
    async def test_flush_skips_when_no_token(self, relay):
        relay._retry_queue.append(("room-1", [{"type": "ev1"}]))
        relay._connection_token = None

        await relay._flush_retry_queue()
        assert len(relay._retry_queue) == 1


class TestGetStatus:
    @pytest.mark.asyncio
    async def test_get_status(self, relay):
        mock_resp = _make_mock_resp(200, {"hubs": [{"hub_id": "hub-123", "is_online": True}]})
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.is_closed = False
        _attach_mock_client(relay, mock_client)

        data = await relay.get_status()
        assert data["hubs"][0]["is_online"] is True


class TestTimeoutConfig:
    def test_separate_clients_created(self, relay):
        """Verify that http and sse clients are distinct slots."""
        assert relay._http_client is None
        assert relay._sse_client is None

    def test_no_dead_code_attributes(self, relay):
        """Verify dead code was cleaned up."""
        assert not hasattr(relay, "_token_refreshed")
