"""
Comprehensive unit tests for hub/a2a_compat.py
A2A v0.3/v1.0 protocol compatibility layer
"""

import pytest
from hub.a2a_compat import (
    # Task 1
    ResolvedInterface,
    JsonRpcError,
    A2AVersionFallbackError,
    FALLBACK_ELIGIBLE_CODES,
    CANONICAL_TERMINAL_STATES,
    # Task 2
    validate_agent_card,
    # Task 3
    select_interface,
    select_fallback_interface,
    # Task 4
    get_method_name,
    get_headers,
    normalize_task_state,
    normalize_role,
    # Task 5
    build_message_parts,
    normalize_inbound_parts,
    # Task 6
    build_request_params,
    # Task 7
    extract_response,
    # Task 8
    classify_stream_event,
    # Task 9
    extract_jsonrpc_error,
)


# ============================================================================
# Task 1: Core Data Types
# ============================================================================

class TestResolvedInterface:
    def test_resolved_interface_creation(self):
        ri = ResolvedInterface(
            binding="json-rpc",
            protocol_version="1.0",
            url="https://example.com/api"
        )
        assert ri.binding == "json-rpc"
        assert ri.protocol_version == "1.0"
        assert ri.url == "https://example.com/api"

    def test_resolved_interface_frozen(self):
        ri = ResolvedInterface(
            binding="json-rpc",
            protocol_version="0.3",
            url="https://example.com"
        )
        with pytest.raises(Exception):  # frozen dataclass
            ri.url = "https://new.com"


class TestJsonRpcError:
    def test_jsonrpc_error_creation(self):
        err = JsonRpcError(code=-32601, message="Method not found", data={"foo": "bar"})
        assert err.code == -32601
        assert err.message == "Method not found"
        assert err.data == {"foo": "bar"}

    def test_jsonrpc_error_frozen(self):
        err = JsonRpcError(code=-32009, message="Capability not supported")
        with pytest.raises(Exception):  # frozen dataclass
            err.code = 123


class TestConstants:
    def test_fallback_eligible_codes(self):
        assert FALLBACK_ELIGIBLE_CODES == {-32601, -32009}

    def test_canonical_terminal_states(self):
        assert CANONICAL_TERMINAL_STATES == {"completed", "failed", "canceled", "rejected"}


# ============================================================================
# Task 2: Card Validation
# ============================================================================

class TestValidateAgentCard:
    def test_validate_v10_card(self):
        card = {
            "name": "MyAgent",
            "capabilities": {
                "text": True
            },
            "supportedInterfaces": [
                {
                    "binding": "json-rpc",
                    "protocolVersion": "1.0",
                    "url": "https://example.com/v1"
                }
            ]
        }
        result = validate_agent_card(card)
        assert result == card

    def test_validate_v03_card(self):
        card = {
            "name": "MyAgent",
            "capabilities": {"text": True},
            "url": "https://example.com"
        }
        result = validate_agent_card(card)
        assert result == card

    def test_validate_missing_name(self):
        card = {"capabilities": {"text": True}}
        result = validate_agent_card(card)
        assert result is None

    def test_validate_missing_capabilities(self):
        card = {"name": "MyAgent"}
        result = validate_agent_card(card)
        assert result is None

    def test_validate_extra_fields_ok(self):
        card = {
            "name": "MyAgent",
            "capabilities": {"text": True},
            "description": "A helpful agent",
            "url": "https://example.com"
        }
        result = validate_agent_card(card)
        assert result == card

    def test_validate_invalid_structure(self):
        card = {"name": 123, "capabilities": "invalid"}
        result = validate_agent_card(card)
        assert result is None


# ============================================================================
# Task 3: Interface Selection
# ============================================================================

class TestSelectInterface:
    def test_select_v10_interface(self):
        card = {
            "name": "Agent",
            "supportedInterfaces": [
                {
                    "binding": "json-rpc",
                    "protocolVersion": "1.0",
                    "url": "https://example.com/v1"
                }
            ]
        }
        iface = select_interface(card)
        assert iface.protocol_version == "1.0"
        assert iface.binding == "json-rpc"
        assert iface.url == "https://example.com/v1"

    def test_select_v03_interface(self):
        card = {
            "name": "Agent",
            "supportedInterfaces": [
                {
                    "binding": "json-rpc",
                    "protocolVersion": "0.3",
                    "url": "https://example.com/v03"
                }
            ]
        }
        iface = select_interface(card)
        assert iface.protocol_version == "0.3"
        assert iface.url == "https://example.com/v03"

    def test_select_prefers_v10_over_v03(self):
        card = {
            "name": "Agent",
            "supportedInterfaces": [
                {
                    "binding": "json-rpc",
                    "protocolVersion": "0.3",
                    "url": "https://example.com/v03"
                },
                {
                    "binding": "json-rpc",
                    "protocolVersion": "1.0",
                    "url": "https://example.com/v1"
                }
            ]
        }
        iface = select_interface(card)
        assert iface.protocol_version == "1.0"

    def test_select_fallback_to_top_level_url_when_no_supported_interfaces(self):
        card = {
            "name": "Agent",
            "url": "https://example.com/legacy"
        }
        iface = select_interface(card)
        assert iface.protocol_version == "0.3"
        assert iface.url == "https://example.com/legacy"

    def test_select_raises_when_supported_interfaces_present_but_unusable(self):
        card = {
            "name": "Agent",
            "supportedInterfaces": [
                {
                    "binding": "json-rpc",
                    "protocolVersion": "2.0",
                    "url": "https://example.com/v2"
                }
            ],
            "url": "https://example.com/legacy"
        }
        with pytest.raises(ValueError, match="supportedInterfaces present but no usable JSON-RPC interface"):
            select_interface(card)

    def test_select_raises_when_no_url_and_no_supported_interfaces(self):
        card = {"name": "Agent"}
        with pytest.raises(ValueError, match="No JSON-RPC interface found"):
            select_interface(card)

    def test_select_skips_non_jsonrpc_binding(self):
        card = {
            "name": "Agent",
            "supportedInterfaces": [
                {
                    "binding": "grpc",
                    "protocolVersion": "1.0",
                    "url": "https://example.com/grpc"
                },
                {
                    "binding": "json-rpc",
                    "protocolVersion": "0.3",
                    "url": "https://example.com/jsonrpc"
                }
            ]
        }
        iface = select_interface(card)
        assert iface.binding == "json-rpc"
        assert iface.protocol_version == "0.3"

    def test_select_empty_supported_interfaces_uses_top_level_url(self):
        card = {
            "name": "Agent",
            "supportedInterfaces": [],
            "url": "https://example.com/legacy"
        }
        iface = select_interface(card)
        assert iface.protocol_version == "0.3"
        assert iface.url == "https://example.com/legacy"

    def test_select_raises_when_empty_supported_interfaces_and_no_url(self):
        card = {
            "name": "Agent",
            "supportedInterfaces": []
        }
        with pytest.raises(ValueError, match="No JSON-RPC interface found"):
            select_interface(card)

    def test_select_case_sensitive_binding(self):
        card = {
            "name": "Agent",
            "supportedInterfaces": [
                {
                    "binding": "JSON-RPC",
                    "protocolVersion": "1.0",
                    "url": "https://example.com/v1"
                }
            ]
        }
        with pytest.raises(ValueError):
            select_interface(card)

    def test_select_unsupported_version_in_supported_interfaces(self):
        card = {
            "name": "Agent",
            "supportedInterfaces": [
                {
                    "binding": "json-rpc",
                    "protocolVersion": "0.2",
                    "url": "https://example.com/v02"
                }
            ]
        }
        with pytest.raises(ValueError, match="supportedInterfaces present but no usable JSON-RPC interface"):
            select_interface(card)


class TestSelectFallbackInterface:
    def test_select_fallback_when_primary_v10(self):
        card = {
            "name": "Agent",
            "supportedInterfaces": [
                {
                    "binding": "json-rpc",
                    "protocolVersion": "1.0",
                    "url": "https://example.com/v1"
                },
                {
                    "binding": "json-rpc",
                    "protocolVersion": "0.3",
                    "url": "https://example.com/v03"
                }
            ]
        }
        primary = ResolvedInterface(
            binding="json-rpc",
            protocol_version="1.0",
            url="https://example.com/v1"
        )
        fallback = select_fallback_interface(card, primary)
        assert fallback is not None
        assert fallback.protocol_version == "0.3"
        assert fallback.url == "https://example.com/v03"

    def test_select_fallback_returns_none_when_no_alternative(self):
        card = {
            "name": "Agent",
            "supportedInterfaces": [
                {
                    "binding": "json-rpc",
                    "protocolVersion": "1.0",
                    "url": "https://example.com/v1"
                }
            ]
        }
        primary = ResolvedInterface(
            binding="json-rpc",
            protocol_version="1.0",
            url="https://example.com/v1"
        )
        fallback = select_fallback_interface(card, primary)
        assert fallback is None

    def test_select_fallback_skips_primary_version(self):
        card = {
            "name": "Agent",
            "supportedInterfaces": [
                {
                    "binding": "json-rpc",
                    "protocolVersion": "0.3",
                    "url": "https://example.com/v03-a"
                },
                {
                    "binding": "json-rpc",
                    "protocolVersion": "0.3",
                    "url": "https://example.com/v03-b"
                }
            ]
        }
        primary = ResolvedInterface(
            binding="json-rpc",
            protocol_version="0.3",
            url="https://example.com/v03-a"
        )
        fallback = select_fallback_interface(card, primary)
        assert fallback is None


# ============================================================================
# Task 4: Wire Format Mapping
# ============================================================================

class TestGetMethodName:
    def test_get_method_name_v03(self):
        assert get_method_name("SendMessage", "0.3") == "message/send"
        assert get_method_name("SendStreamingMessage", "0.3") == "message/stream"
        assert get_method_name("GetTask", "0.3") == "tasks/get"
        assert get_method_name("CancelTask", "0.3") == "tasks/cancel"

    def test_get_method_name_v10(self):
        assert get_method_name("SendMessage", "1.0") == "SendMessage"
        assert get_method_name("SendStreamingMessage", "1.0") == "SendStreamingMessage"
        assert get_method_name("GetTask", "1.0") == "GetTask"
        assert get_method_name("CancelTask", "1.0") == "CancelTask"

    def test_get_method_name_unsupported_version(self):
        with pytest.raises(ValueError, match="Unsupported protocol version: 2.0"):
            get_method_name("SendMessage", "2.0")

    def test_get_method_name_unsupported_method(self):
        # Unknown method should still work for known versions
        assert get_method_name("UnknownMethod", "1.0") == "UnknownMethod"
        assert get_method_name("UnknownMethod", "0.3") == "UnknownMethod"


class TestGetHeaders:
    def test_get_headers_v10(self):
        headers = get_headers("1.0")
        assert headers == {"A2A-Version": "1.0"}

    def test_get_headers_v03(self):
        headers = get_headers("0.3")
        assert headers == {}


class TestNormalizeTaskState:
    def test_normalize_task_state_screaming_snake(self):
        assert normalize_task_state("WORKING") == "working"
        assert normalize_task_state("TASK_STATE_COMPLETED") == "completed"
        assert normalize_task_state("TASK_STATE_FAILED") == "failed"

    def test_normalize_task_state_already_lowercase(self):
        assert normalize_task_state("working") == "working"
        assert normalize_task_state("completed") == "completed"

    def test_normalize_task_state_strips_prefix(self):
        assert normalize_task_state("TASK_STATE_CANCELED") == "canceled"
        assert normalize_task_state("TASK_STATE_REJECTED") == "rejected"


class TestNormalizeRole:
    def test_normalize_role_screaming(self):
        assert normalize_role("ROLE_USER") == "user"
        assert normalize_role("ROLE_AGENT") == "agent"

    def test_normalize_role_already_lowercase(self):
        assert normalize_role("user") == "user"
        assert normalize_role("agent") == "agent"


# ============================================================================
# Task 5: Part Conversion
# ============================================================================

class TestBuildMessageParts:
    def test_build_message_parts_v03_text(self):
        parts = [{"text": "Hello"}]
        result = build_message_parts(parts, "0.3")
        assert result == [{"kind": "text", "text": "Hello"}]

    def test_build_message_parts_v03_file(self):
        parts = [{"url": "https://example.com/file.pdf", "mediaType": "application/pdf"}]
        result = build_message_parts(parts, "0.3")
        assert result == [
            {
                "kind": "file",
                "file": {
                    "uri": "https://example.com/file.pdf",
                    "mimeType": "application/pdf"
                }
            }
        ]

    def test_build_message_parts_v03_file_with_raw(self):
        parts = [{"raw": "base64data", "mediaType": "image/png", "filename": "img.png"}]
        result = build_message_parts(parts, "0.3")
        assert result == [
            {
                "kind": "file",
                "file": {
                    "bytes": "base64data",
                    "mimeType": "image/png",
                    "name": "img.png"
                }
            }
        ]

    def test_build_message_parts_v03_mixed(self):
        parts = [
            {"text": "Check this out:"},
            {"url": "https://example.com/doc.pdf", "mediaType": "application/pdf"}
        ]
        result = build_message_parts(parts, "0.3")
        assert len(result) == 2
        assert result[0]["kind"] == "text"
        assert result[1]["kind"] == "file"

    def test_build_message_parts_v10_text(self):
        parts = [{"text": "Hello"}]
        result = build_message_parts(parts, "1.0")
        assert result == [{"text": "Hello"}]

    def test_build_message_parts_v10_file(self):
        parts = [{"url": "https://example.com/file.pdf", "mediaType": "application/pdf"}]
        result = build_message_parts(parts, "1.0")
        assert result == [{"url": "https://example.com/file.pdf", "mimeType": "application/pdf"}]

    def test_build_message_parts_v10_file_with_raw(self):
        parts = [{"raw": "base64data", "mediaType": "image/png", "filename": "img.png"}]
        result = build_message_parts(parts, "1.0")
        assert result == [{"raw": "base64data", "mimeType": "image/png", "filename": "img.png"}]

    def test_build_message_parts_v10_mixed(self):
        parts = [
            {"text": "Check this out:"},
            {"url": "https://example.com/doc.pdf", "mediaType": "application/pdf"}
        ]
        result = build_message_parts(parts, "1.0")
        assert len(result) == 2
        assert result[0] == {"text": "Check this out:"}
        assert result[1] == {"url": "https://example.com/doc.pdf", "mimeType": "application/pdf"}

    def test_build_message_parts_empty(self):
        assert build_message_parts([], "0.3") == []
        assert build_message_parts([], "1.0") == []

    def test_build_message_parts_preserves_extra_fields(self):
        parts = [{"text": "Hello", "extra": "field"}]
        result = build_message_parts(parts, "0.3")
        assert result[0]["text"] == "Hello"
        assert result[0]["kind"] == "text"

    def test_build_message_parts_file_url_only(self):
        parts = [{"url": "https://example.com/file.pdf"}]
        result_v03 = build_message_parts(parts, "0.3")
        assert result_v03 == [{"kind": "file", "file": {"uri": "https://example.com/file.pdf"}}]
        result_v10 = build_message_parts(parts, "1.0")
        assert result_v10 == [{"url": "https://example.com/file.pdf"}]

    def test_build_message_parts_file_raw_only(self):
        parts = [{"raw": "base64data"}]
        result_v03 = build_message_parts(parts, "0.3")
        assert result_v03 == [{"kind": "file", "file": {"bytes": "base64data"}}]
        result_v10 = build_message_parts(parts, "1.0")
        assert result_v10 == [{"raw": "base64data"}]


class TestNormalizeInboundParts:
    def test_normalize_inbound_parts_v03_text(self):
        parts = [{"kind": "text", "text": "Hello"}]
        result = normalize_inbound_parts(parts, "0.3")
        assert result == [{"text": "Hello"}]

    def test_normalize_inbound_parts_v03_file_with_uri(self):
        parts = [
            {
                "kind": "file",
                "file": {
                    "uri": "https://example.com/file.pdf",
                    "mimeType": "application/pdf"
                }
            }
        ]
        result = normalize_inbound_parts(parts, "0.3")
        assert result == [
            {
                "url": "https://example.com/file.pdf",
                "mediaType": "application/pdf"
            }
        ]

    def test_normalize_inbound_parts_v03_file_with_bytes(self):
        parts = [
            {
                "kind": "file",
                "file": {
                    "bytes": "base64data",
                    "mimeType": "image/png",
                    "name": "img.png"
                }
            }
        ]
        result = normalize_inbound_parts(parts, "0.3")
        assert result == [
            {
                "raw": "base64data",
                "mediaType": "image/png",
                "filename": "img.png"
            }
        ]

    def test_normalize_inbound_parts_v03_mixed(self):
        parts = [
            {"kind": "text", "text": "Check this:"},
            {
                "kind": "file",
                "file": {
                    "uri": "https://example.com/doc.pdf",
                    "mimeType": "application/pdf"
                }
            }
        ]
        result = normalize_inbound_parts(parts, "0.3")
        assert len(result) == 2
        assert result[0] == {"text": "Check this:"}
        assert result[1] == {"url": "https://example.com/doc.pdf", "mediaType": "application/pdf"}

    def test_normalize_inbound_parts_v10_text(self):
        parts = [{"text": "Hello"}]
        result = normalize_inbound_parts(parts, "1.0")
        assert result == [{"text": "Hello"}]

    def test_normalize_inbound_parts_v10_file(self):
        parts = [{"url": "https://example.com/file.pdf", "mimeType": "application/pdf"}]
        result = normalize_inbound_parts(parts, "1.0")
        assert result == [{"url": "https://example.com/file.pdf", "mediaType": "application/pdf"}]

    def test_normalize_inbound_parts_v10_file_with_raw(self):
        parts = [{"raw": "base64data", "mimeType": "image/png", "filename": "img.png"}]
        result = normalize_inbound_parts(parts, "1.0")
        assert result == [{"raw": "base64data", "mediaType": "image/png", "filename": "img.png"}]

    def test_normalize_inbound_parts_empty(self):
        assert normalize_inbound_parts([], "0.3") == []
        assert normalize_inbound_parts([], "1.0") == []

    def test_normalize_inbound_parts_v03_file_uri_only(self):
        parts = [{"kind": "file", "file": {"uri": "https://example.com/file.pdf"}}]
        result = normalize_inbound_parts(parts, "0.3")
        assert result == [{"url": "https://example.com/file.pdf"}]

    def test_normalize_inbound_parts_v03_file_bytes_only(self):
        parts = [{"kind": "file", "file": {"bytes": "base64data"}}]
        result = normalize_inbound_parts(parts, "0.3")
        assert result == [{"raw": "base64data"}]

    def test_normalize_inbound_parts_v10_stale_kind(self):
        # v1.0 parts should not have kind, but if they do, strip it
        parts = [{"kind": "text", "text": "Hello"}]
        result = normalize_inbound_parts(parts, "1.0")
        assert result == [{"text": "Hello"}]
        assert "kind" not in result[0]

    def test_normalize_inbound_parts_v10_file_only_url(self):
        parts = [{"url": "https://example.com/file.pdf"}]
        result = normalize_inbound_parts(parts, "1.0")
        assert result == [{"url": "https://example.com/file.pdf"}]

    def test_normalize_inbound_parts_v10_file_only_raw(self):
        parts = [{"raw": "base64data"}]
        result = normalize_inbound_parts(parts, "1.0")
        assert result == [{"raw": "base64data"}]

    def test_normalize_inbound_parts_preserves_extra_fields(self):
        parts = [{"kind": "text", "text": "Hello", "extra": "field"}]
        result = normalize_inbound_parts(parts, "0.3")
        assert result[0]["text"] == "Hello"
        assert "extra" in result[0]

    def test_normalize_inbound_parts_flat_v03_text(self):
        # Flat parts without kind (already canonical-ish)
        parts = [{"text": "Hello"}]
        result = normalize_inbound_parts(parts, "0.3")
        assert result == [{"text": "Hello"}]

    def test_normalize_inbound_parts_flat_v03_file(self):
        # Flat file part without kind
        parts = [{"uri": "https://example.com/file.pdf", "mimeType": "application/pdf"}]
        result = normalize_inbound_parts(parts, "0.3")
        assert result == [{"url": "https://example.com/file.pdf", "mediaType": "application/pdf"}]

    def test_normalize_inbound_parts_v03_file_both_uri_and_bytes(self):
        # Edge case: both uri and bytes
        parts = [
            {
                "kind": "file",
                "file": {
                    "uri": "https://example.com/file.pdf",
                    "bytes": "base64data",
                    "mimeType": "application/pdf"
                }
            }
        ]
        result = normalize_inbound_parts(parts, "0.3")
        # Should prefer uri over bytes
        assert result[0]["url"] == "https://example.com/file.pdf"
        assert result[0]["raw"] == "base64data"

    def test_normalize_inbound_parts_v10_file_both_url_and_raw(self):
        # Edge case: both url and raw
        parts = [
            {
                "url": "https://example.com/file.pdf",
                "raw": "base64data",
                "mimeType": "application/pdf"
            }
        ]
        result = normalize_inbound_parts(parts, "1.0")
        assert result[0]["url"] == "https://example.com/file.pdf"
        assert result[0]["raw"] == "base64data"
        assert result[0]["mediaType"] == "application/pdf"


# ============================================================================
# Task 6: Request Params
# ============================================================================

class TestBuildRequestParams:
    def test_build_request_params_v03_send_message(self):
        message = {
            "role": "user",
            "parts": [{"text": "Hello"}]
        }
        params = build_request_params(message, "0.3")
        assert params == {
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": "Hello"}]
            }
        }

    def test_build_request_params_v10_send_message(self):
        message = {
            "role": "user",
            "parts": [{"text": "Hello"}]
        }
        params = build_request_params(message, "1.0")
        assert params == {
            "message": {
                "role": "user",
                "parts": [{"text": "Hello"}]
            }
        }

    def test_build_request_params_with_configuration_v03(self):
        message = {
            "role": "user",
            "parts": [{"text": "Hello"}]
        }
        config = {"maxTokens": 100}
        params = build_request_params(message, "0.3", configuration=config)
        assert params == {
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": "Hello"}]
            },
            "configuration": {"maxTokens": 100}
        }

    def test_build_request_params_with_configuration_v10(self):
        message = {
            "role": "user",
            "parts": [{"text": "Hello"}]
        }
        config = {"maxTokens": 100}
        params = build_request_params(message, "1.0", configuration=config)
        assert params == {
            "message": {
                "role": "user",
                "parts": [{"text": "Hello"}]
            },
            "configuration": {"maxTokens": 100}
        }


# ============================================================================
# Task 7: Response Extraction
# ============================================================================

class TestExtractResponse:
    def test_extract_response_v03_message(self):
        raw = {
            "role": "agent",
            "parts": [{"kind": "text", "text": "Response"}]
        }
        result = extract_response(raw, "0.3")
        assert result["role"] == "agent"
        assert result["parts"] == [{"text": "Response"}]

    def test_extract_response_v03_with_result_wrapper(self):
        raw = {
            "result": {
                "role": "agent",
                "parts": [{"kind": "text", "text": "Response"}]
            }
        }
        result = extract_response(raw, "0.3")
        assert result["result"]["role"] == "agent"
        assert result["result"]["parts"] == [{"text": "Response"}]

    def test_extract_response_v10_message_wrapper(self):
        raw = {
            "message": {
                "role": "ROLE_AGENT",
                "parts": [{"text": "Response"}]
            }
        }
        result = extract_response(raw, "1.0")
        assert result["kind"] == "message"
        assert result["role"] == "agent"
        assert result["parts"] == [{"text": "Response"}]

    def test_extract_response_v10_task_wrapper(self):
        raw = {
            "task": {
                "state": "TASK_STATE_WORKING",
                "artifacts": [{"kind": "text", "text": "Artifact"}]
            }
        }
        result = extract_response(raw, "1.0")
        assert result["kind"] == "task"
        assert result["state"] == "working"
        assert result["artifacts"] == [{"text": "Artifact"}]

    def test_extract_response_v10_normalizes_role(self):
        raw = {
            "message": {
                "role": "ROLE_USER",
                "parts": [{"text": "Hello"}]
            }
        }
        result = extract_response(raw, "1.0")
        assert result["role"] == "user"

    def test_extract_response_v10_normalizes_state(self):
        raw = {
            "task": {
                "state": "TASK_STATE_COMPLETED"
            }
        }
        result = extract_response(raw, "1.0")
        assert result["state"] == "completed"

    def test_extract_response_v03_file_parts(self):
        raw = {
            "role": "agent",
            "parts": [
                {
                    "kind": "file",
                    "file": {
                        "uri": "https://example.com/file.pdf",
                        "mimeType": "application/pdf"
                    }
                }
            ]
        }
        result = extract_response(raw, "0.3")
        assert result["parts"] == [
            {
                "url": "https://example.com/file.pdf",
                "mediaType": "application/pdf"
            }
        ]

    def test_extract_response_v10_file_parts(self):
        raw = {
            "message": {
                "role": "ROLE_AGENT",
                "parts": [
                    {
                        "url": "https://example.com/file.pdf",
                        "mimeType": "application/pdf"
                    }
                ]
            }
        }
        result = extract_response(raw, "1.0")
        assert result["parts"] == [
            {
                "url": "https://example.com/file.pdf",
                "mediaType": "application/pdf"
            }
        ]


# ============================================================================
# Task 8: Stream Event Classification
# ============================================================================

class TestClassifyStreamEvent:
    def test_classify_stream_event_v03_message(self):
        data = {
            "event": "message",
            "role": "agent",
            "parts": [{"kind": "text", "text": "Hello"}]
        }
        event_type, payload = classify_stream_event(data, "0.3")
        assert event_type == "message"
        assert payload["role"] == "agent"
        assert payload["parts"] == [{"text": "Hello"}]

    def test_classify_stream_event_v03_artifact(self):
        data = {
            "event": "artifact",
            "parts": [{"kind": "text", "text": "Artifact"}]
        }
        event_type, payload = classify_stream_event(data, "0.3")
        assert event_type == "artifact"
        assert payload["parts"] == [{"text": "Artifact"}]

    def test_classify_stream_event_v03_status(self):
        data = {
            "event": "status",
            "state": "working",
            "parts": [{"kind": "text", "text": "Status"}]
        }
        event_type, payload = classify_stream_event(data, "0.3")
        assert event_type == "status"
        assert payload["state"] == "working"
        assert payload["parts"] == [{"text": "Status"}]

    def test_classify_stream_event_v03_unknown(self):
        data = {"event": "unknown"}
        result = classify_stream_event(data, "0.3")
        assert result is None

    def test_classify_stream_event_v10_message(self):
        data = {
            "message": {
                "role": "ROLE_AGENT",
                "parts": [{"text": "Hello"}]
            }
        }
        event_type, payload = classify_stream_event(data, "1.0")
        assert event_type == "message"
        assert payload["role"] == "agent"
        assert payload["parts"] == [{"text": "Hello"}]

    def test_classify_stream_event_v10_artifact(self):
        data = {
            "artifact": {
                "parts": [{"text": "Artifact"}]
            }
        }
        event_type, payload = classify_stream_event(data, "1.0")
        assert event_type == "artifact"
        assert payload["parts"] == [{"text": "Artifact"}]

    def test_classify_stream_event_v10_status(self):
        data = {
            "status": {
                "state": "WORKING",
                "message": "Processing"
            }
        }
        event_type, payload = classify_stream_event(data, "1.0")
        assert event_type == "status"
        assert payload["state"] == "working"
        assert payload["message"] == "Processing"

    def test_classify_stream_event_v10_status_terminal(self):
        data = {
            "status": {
                "state": "TASK_STATE_COMPLETED"
            }
        }
        event_type, payload = classify_stream_event(data, "1.0")
        assert event_type == "status"
        assert payload["state"] == "completed"
        assert payload["final"] is True

    def test_classify_stream_event_v10_status_non_terminal(self):
        data = {
            "status": {
                "state": "WORKING"
            }
        }
        event_type, payload = classify_stream_event(data, "1.0")
        assert event_type == "status"
        assert payload["state"] == "working"
        assert payload.get("final") is False

    def test_classify_stream_event_v10_unknown(self):
        data = {"unknown": {}}
        result = classify_stream_event(data, "1.0")
        assert result is None

    def test_classify_stream_event_v03_file_parts(self):
        data = {
            "event": "message",
            "role": "agent",
            "parts": [
                {
                    "kind": "file",
                    "file": {
                        "uri": "https://example.com/file.pdf",
                        "mimeType": "application/pdf"
                    }
                }
            ]
        }
        event_type, payload = classify_stream_event(data, "0.3")
        assert payload["parts"] == [
            {
                "url": "https://example.com/file.pdf",
                "mediaType": "application/pdf"
            }
        ]

    def test_classify_stream_event_v10_file_parts(self):
        data = {
            "message": {
                "role": "ROLE_AGENT",
                "parts": [
                    {
                        "url": "https://example.com/file.pdf",
                        "mimeType": "application/pdf"
                    }
                ]
            }
        }
        event_type, payload = classify_stream_event(data, "1.0")
        assert payload["parts"] == [
            {
                "url": "https://example.com/file.pdf",
                "mediaType": "application/pdf"
            }
        ]

    def test_classify_stream_event_v03_no_parts(self):
        data = {
            "event": "status",
            "state": "working"
        }
        event_type, payload = classify_stream_event(data, "0.3")
        assert event_type == "status"
        assert "parts" not in payload or payload.get("parts") == []


# ============================================================================
# Task 9: JSON-RPC Error Extraction
# ============================================================================

class TestExtractJsonrpcError:
    def test_extract_jsonrpc_error_method_not_found(self):
        raw = {
            "error": {
                "code": -32601,
                "message": "Method not found"
            }
        }
        err = extract_jsonrpc_error(raw)
        assert err is not None
        assert err.code == -32601
        assert err.message == "Method not found"
        assert err.data is None

    def test_extract_jsonrpc_error_with_data(self):
        raw = {
            "error": {
                "code": -32009,
                "message": "Capability not supported",
                "data": {"capability": "streaming"}
            }
        }
        err = extract_jsonrpc_error(raw)
        assert err is not None
        assert err.code == -32009
        assert err.data == {"capability": "streaming"}

    def test_extract_jsonrpc_error_no_error(self):
        raw = {"result": {"success": True}}
        err = extract_jsonrpc_error(raw)
        assert err is None

    def test_extract_jsonrpc_error_missing_code(self):
        raw = {
            "error": {
                "message": "Some error"
            }
        }
        err = extract_jsonrpc_error(raw)
        assert err is None

    def test_extract_jsonrpc_error_missing_message(self):
        raw = {
            "error": {
                "code": -32600
            }
        }
        err = extract_jsonrpc_error(raw)
        assert err is None

    def test_extract_jsonrpc_error_invalid_code_type(self):
        raw = {
            "error": {
                "code": "not-a-number",
                "message": "Invalid"
            }
        }
        err = extract_jsonrpc_error(raw)
        assert err is None

    def test_extract_jsonrpc_error_fallback_eligible(self):
        raw = {
            "error": {
                "code": -32601,
                "message": "Method not found"
            }
        }
        err = extract_jsonrpc_error(raw)
        assert err.code in FALLBACK_ELIGIBLE_CODES
