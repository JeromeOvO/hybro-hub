"""Tests for hub.dispatcher."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from hub.agent_registry import LocalAgent
from hub.dispatcher import Dispatcher


@pytest.fixture
def agent():
    return LocalAgent(
        local_agent_id="test_001",
        name="Test Agent",
        url="http://localhost:9001",
        agent_card={"capabilities": {"streaming": False}},
    )


@pytest.fixture
def streaming_agent():
    return LocalAgent(
        local_agent_id="test_002",
        name="Streaming Agent",
        url="http://localhost:9002",
        agent_card={"capabilities": {"streaming": True}},
    )


SAMPLE_MESSAGE = {
    "messageId": "msg-123",
    "role": "user",
    "parts": [{"text": "Hello agent"}],
}


class TestDispatchSync:
    @pytest.mark.asyncio
    async def test_dispatch_sync_success(self, agent):
        dispatcher = Dispatcher()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "jsonrpc": "2.0",
            "id": "1",
            "result": {
                "status": {
                    "state": "completed",
                    "message": {
                        "role": "agent",
                        "parts": [{"text": "Hi there!"}],
                    },
                },
            },
        }
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        dispatcher._client = mock_client

        events = await dispatcher.dispatch(
            agent=agent,
            message_dict=SAMPLE_MESSAGE,
            agent_message_id="am-001",
            user_message_id="um-001",
        )

        assert len(events) == 3
        assert events[0]["type"] == "task_submitted"
        assert events[1]["type"] == "agent_response"
        assert events[1]["data"]["content"] == "Hi there!"
        assert events[2]["type"] == "processing_status"
        assert events[2]["data"]["status"] == "completed"

    @pytest.mark.asyncio
    async def test_dispatch_sync_error(self, agent):
        dispatcher = Dispatcher()
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=Exception("connection failed"))
        dispatcher._client = mock_client

        events = await dispatcher.dispatch(
            agent=agent,
            message_dict=SAMPLE_MESSAGE,
            agent_message_id="am-001",
        )

        assert len(events) == 3
        assert events[0]["type"] == "task_submitted"
        assert "Error" in events[1]["data"]["content"]
        assert events[2]["type"] == "processing_status"


class TestJsonRpcBuild:
    def test_build_jsonrpc(self):
        body = Dispatcher._build_jsonrpc(SAMPLE_MESSAGE, "message/send")
        assert body["jsonrpc"] == "2.0"
        assert body["method"] == "message/send"
        assert body["params"]["message"] == SAMPLE_MESSAGE
        assert "id" in body


class TestExtractText:
    def test_extract_from_status_message(self):
        result = {
            "result": {
                "status": {
                    "message": {
                        "parts": [{"text": "Response text"}],
                    },
                },
            },
        }
        assert Dispatcher._extract_text_from_response(result) == "Response text"

    def test_extract_from_artifacts(self):
        result = {
            "result": {
                "artifacts": [
                    {"parts": [{"text": "Artifact text"}]},
                ],
            },
        }
        assert Dispatcher._extract_text_from_response(result) == "Artifact text"

    def test_extract_from_parts(self):
        result = {"parts": [{"text": "Direct parts"}]}
        assert Dispatcher._extract_text_from_response(result) == "Direct parts"

    def test_extract_with_root_wrapper(self):
        result = {
            "result": {
                "status": {
                    "message": {
                        "parts": [{"root": {"text": "Wrapped"}}],
                    },
                },
            },
        }
        assert Dispatcher._extract_text_from_response(result) == "Wrapped"


class TestExtractChunkText:
    def test_artifact_chunk_raw(self):
        data = {"artifact": {"parts": [{"text": "chunk"}]}}
        assert Dispatcher._extract_chunk_text(data) == ("chunk", True)

    def test_artifact_chunk_jsonrpc_wrapped(self):
        data = {
            "jsonrpc": "2.0",
            "id": "req-1",
            "result": {"artifact": {"parts": [{"text": "wrapped chunk"}]}},
        }
        assert Dispatcher._extract_chunk_text(data) == ("wrapped chunk", True)

    def test_status_chunk_raw(self):
        data = {"status": {"message": {"parts": [{"text": "done"}]}}}
        assert Dispatcher._extract_chunk_text(data) == ("done", False)

    def test_status_chunk_jsonrpc_wrapped(self):
        data = {
            "jsonrpc": "2.0",
            "id": "req-2",
            "result": {
                "status": {"message": {"parts": [{"text": "done wrapped"}]}},
            },
        }
        assert Dispatcher._extract_chunk_text(data) == ("done wrapped", False)

    def test_status_chunk_with_root_wrapper(self):
        data = {
            "result": {
                "status": {"message": {"parts": [{"root": {"text": "nested"}}]}},
            },
        }
        assert Dispatcher._extract_chunk_text(data) == ("nested", False)

    def test_empty_chunk(self):
        assert Dispatcher._extract_chunk_text({}) == ("", False)
