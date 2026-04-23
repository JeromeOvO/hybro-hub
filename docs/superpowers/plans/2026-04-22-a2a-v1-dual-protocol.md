# A2A v1.0 Dual-Protocol Client Upgrade — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade hybro-hub to speak A2A v1.0 wire format to v1.0 local agents while continuing to speak v0.3 to legacy agents, with runtime fallback on version mismatch.

**Architecture:** A central `hub/a2a_compat.py` module encapsulates all v0.3/v1.0 differences (method names, headers, state enums, part formats, stream event shapes). The dispatcher and agent registry route through this module based on per-agent `ResolvedInterface` metadata. Canonical internal format uses lowercase states and flattened parts (matching v1.0 wire format). The relay event format is unchanged.

**Tech Stack:** Python 3.11+, `a2a-sdk>=1.0.1` (protobuf types + `a2a.compat.v0_3` built-in layer), `google.protobuf.json_format.ParseDict`, `httpx`, `httpx-sse`, `pytest`

**Spec:** `docs/superpowers/specs/2026-04-22-a2a-v1-dual-protocol-design.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `hub/a2a_compat.py` | CREATE | Central compat layer: data types, card validation, interface selection, wire format translation, error extraction |
| `tests/test_a2a_compat.py` | CREATE | Unit tests for all compat functions |
| `hub/agent_registry.py` | MODIFY | Replace `AgentCard.model_validate()`, add `interface`/`fallback_interface` to `LocalAgent` |
| `tests/test_agent_registry.py` | MODIFY | Add v1.0 card fixtures, test interface selection during discovery |
| `hub/dispatcher.py` | MODIFY | Route methods/headers/parts/responses through compat layer, add fallback retry |
| `tests/test_dispatcher.py` | MODIFY | Add v1.0 dispatch paths, fallback retry tests |
| `hub/main.py` | MODIFY | Fix HITL reply parts to canonical flattened form |
| `tests/test_main.py` | MODIFY | Update `LocalAgent` fixtures |
| `tests/e2e_live_test.py` | MODIFY | Add v1.0 manual test path |

---

## Phase 1: Compat Layer Foundation

### Task 1: Core Data Types and Constants

**Files:**
- Create: `hub/a2a_compat.py`
- Create: `tests/test_a2a_compat.py`

- [ ] **Step 1: Write the initial test file with imports and data type tests**

```python
# tests/test_a2a_compat.py
"""Tests for hub.a2a_compat — A2A v0.3/v1.0 protocol compat layer."""

from hub.a2a_compat import (
    A2AVersionFallbackError,
    FALLBACK_ELIGIBLE_CODES,
    JsonRpcError,
    ResolvedInterface,
)


class TestResolvedInterface:
    def test_frozen(self):
        ri = ResolvedInterface(binding="JSONRPC", protocol_version="1.0", url="http://localhost:9001")
        assert ri.binding == "JSONRPC"
        assert ri.protocol_version == "1.0"
        assert ri.url == "http://localhost:9001"

    def test_equality(self):
        a = ResolvedInterface(binding="JSONRPC", protocol_version="0.3", url="http://localhost:9001")
        b = ResolvedInterface(binding="JSONRPC", protocol_version="0.3", url="http://localhost:9001")
        assert a == b


class TestJsonRpcError:
    def test_fields(self):
        err = JsonRpcError(code=-32601, message="Method not found")
        assert err.code == -32601
        assert err.message == "Method not found"
        assert err.data is None

    def test_with_data(self):
        err = JsonRpcError(code=-32009, message="Version not supported", data={"version": "1.0"})
        assert err.data == {"version": "1.0"}


class TestConstants:
    def test_fallback_eligible_codes(self):
        assert -32601 in FALLBACK_ELIGIBLE_CODES
        assert -32009 in FALLBACK_ELIGIBLE_CODES

    def test_fallback_error_is_exception(self):
        err = A2AVersionFallbackError("test")
        assert isinstance(err, Exception)
        assert str(err) == "test"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_a2a_compat.py::TestResolvedInterface -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'hub.a2a_compat'`

- [ ] **Step 3: Create `hub/a2a_compat.py` with data types**

```python
# hub/a2a_compat.py
"""A2A v0.3/v1.0 protocol compatibility layer.

Encapsulates all version-specific differences so that dispatcher.py
and agent_registry.py only call into this module for protocol decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ResolvedInterface:
    """A resolved JSON-RPC interface from an agent card."""

    binding: str
    protocol_version: str
    url: str


@dataclass(frozen=True)
class JsonRpcError:
    """A structured JSON-RPC error extracted from a response."""

    code: int
    message: str
    data: Any = None


class A2AVersionFallbackError(Exception):
    """Raised when a v1.0 dispatch gets a fallback-eligible JSON-RPC error."""


FALLBACK_ELIGIBLE_CODES: set[int] = {-32601, -32009}

CANONICAL_TERMINAL_STATES: set[str] = {
    "completed", "failed", "canceled", "rejected",
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_a2a_compat.py -v`
Expected: PASS (all 5 tests)

- [ ] **Step 5: Commit**

```bash
git add hub/a2a_compat.py tests/test_a2a_compat.py
git commit -m "feat(a2a-compat): add core data types and constants"
```

---

### Task 2: Agent Card Validation

**Files:**
- Modify: `hub/a2a_compat.py`
- Modify: `tests/test_a2a_compat.py`

- [ ] **Step 1: Write failing tests for validate_agent_card**

Add to `tests/test_a2a_compat.py`:

```python
from hub.a2a_compat import validate_agent_card


V03_CARD = {
    "name": "Legacy Agent",
    "url": "http://localhost:9001/",
    "version": "1.0.0",
    "capabilities": {"streaming": True},
    "skills": [{"id": "s1", "name": "Skill", "description": "A skill", "tags": ["chat"]}],
    "defaultInputModes": ["text"],
    "defaultOutputModes": ["text"],
}

V10_CARD = {
    "name": "Modern Agent",
    "description": "A v1.0 agent",
    "supportedInterfaces": [
        {
            "protocolBinding": "JSONRPC",
            "protocolVersion": "1.0",
            "url": "http://localhost:9002/a2a",
        },
    ],
    "capabilities": {"streaming": True},
    "skills": [{"id": "s1", "name": "Skill", "description": "A skill"}],
}

DUAL_MODE_CARD = {
    "name": "Dual Agent",
    "description": "Speaks both",
    "url": "http://localhost:9003/",
    "supportedInterfaces": [
        {
            "protocolBinding": "JSONRPC",
            "protocolVersion": "1.0",
            "url": "http://localhost:9003/v1",
        },
        {
            "protocolBinding": "JSONRPC",
            "protocolVersion": "0.3",
            "url": "http://localhost:9003/v03",
        },
    ],
    "capabilities": {},
    "skills": [],
}


class TestValidateAgentCard:
    def test_v03_card_accepted(self):
        result = validate_agent_card(V03_CARD)
        assert result is not None
        assert result["name"] == "Legacy Agent"

    def test_v10_card_accepted(self):
        result = validate_agent_card(V10_CARD)
        assert result is not None
        assert result["name"] == "Modern Agent"

    def test_dual_mode_card_accepted(self):
        result = validate_agent_card(DUAL_MODE_CARD)
        assert result is not None

    def test_invalid_card_returns_none(self):
        result = validate_agent_card({"error": "not an agent"})
        assert result is None

    def test_empty_dict_returns_none(self):
        result = validate_agent_card({})
        assert result is None

    def test_card_with_vendor_extensions_accepted(self):
        card = {**V10_CARD, "x-vendor-feature": {"enabled": True}}
        result = validate_agent_card(card)
        assert result is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_a2a_compat.py::TestValidateAgentCard -v`
Expected: FAIL with `ImportError: cannot import name 'validate_agent_card'`

- [ ] **Step 3: Implement validate_agent_card**

Add to `hub/a2a_compat.py`:

```python
def validate_agent_card(card_data: dict) -> dict | None:
    """Validate an agent card, trying v1.0 first, then falling back to v0.3.

    Returns the card dict if valid, None if neither schema accepts it.
    Both parsers tolerate unknown/vendor fields for forward compatibility.
    """
    # Try v1.0 (protobuf ParseDict)
    try:
        from google.protobuf.json_format import ParseDict
        from a2a.types import AgentCard

        ParseDict(card_data, AgentCard(), ignore_unknown_fields=True)
        return card_data
    except Exception:
        pass

    # Fall back to v0.3 (Pydantic model_validate)
    try:
        from a2a.compat.v0_3.types import AgentCard as V03AgentCard

        V03AgentCard.model_validate(card_data)
        return card_data
    except Exception:
        pass

    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_a2a_compat.py::TestValidateAgentCard -v`
Expected: PASS (all 6 tests)

- [ ] **Step 5: Commit**

```bash
git add hub/a2a_compat.py tests/test_a2a_compat.py
git commit -m "feat(a2a-compat): add validate_agent_card with try-both strategy"
```

---

### Task 3: Interface Selection

**Files:**
- Modify: `hub/a2a_compat.py`
- Modify: `tests/test_a2a_compat.py`

- [ ] **Step 1: Write failing tests for select_interface and select_fallback_interface**

Add to `tests/test_a2a_compat.py`:

```python
import pytest

from hub.a2a_compat import select_fallback_interface, select_interface


class TestSelectInterface:
    def test_v03_card_uses_top_level_url(self):
        ri = select_interface(V03_CARD)
        assert ri.binding == "JSONRPC"
        assert ri.protocol_version == "0.3"
        assert ri.url == "http://localhost:9001/"

    def test_v10_card_selects_jsonrpc_interface(self):
        ri = select_interface(V10_CARD)
        assert ri.binding == "JSONRPC"
        assert ri.protocol_version == "1.0"
        assert ri.url == "http://localhost:9002/a2a"

    def test_dual_mode_card_prefers_v10(self):
        ri = select_interface(DUAL_MODE_CARD)
        assert ri.protocol_version == "1.0"
        assert ri.url == "http://localhost:9003/v1"

    def test_card_with_only_v03_interface(self):
        card = {
            "name": "Old-style",
            "supportedInterfaces": [
                {"protocolBinding": "JSONRPC", "protocolVersion": "0.3", "url": "http://localhost:9004/"},
            ],
        }
        ri = select_interface(card)
        assert ri.protocol_version == "0.3"

    def test_missing_version_defaults_to_v03(self):
        card = {
            "name": "No-version",
            "supportedInterfaces": [
                {"protocolBinding": "JSONRPC", "url": "http://localhost:9005/"},
            ],
        }
        ri = select_interface(card)
        assert ri.protocol_version == "0.3"

    def test_skips_non_jsonrpc_bindings(self):
        card = {
            "name": "gRPC Agent",
            "supportedInterfaces": [
                {"protocolBinding": "gRPC", "protocolVersion": "1.0", "url": "grpc://localhost:50051"},
                {"protocolBinding": "JSONRPC", "protocolVersion": "1.0", "url": "http://localhost:9006/"},
            ],
        }
        ri = select_interface(card)
        assert ri.binding == "JSONRPC"
        assert ri.url == "http://localhost:9006/"

    def test_rejects_unsupported_version(self):
        card = {
            "name": "Future Agent",
            "supportedInterfaces": [
                {"protocolBinding": "JSONRPC", "protocolVersion": "2.0", "url": "http://localhost:9007/"},
            ],
        }
        with pytest.raises(ValueError, match="supportedInterfaces present but no usable"):
            select_interface(card)

    def test_rejects_future_versions_even_with_top_level_url(self):
        """Card with supportedInterfaces (all future) + top-level url must NOT silently degrade to v0.3."""
        card = {
            "name": "Future Agent With URL",
            "url": "http://localhost:9007/",
            "supportedInterfaces": [
                {"protocolBinding": "JSONRPC", "protocolVersion": "2.0", "url": "http://localhost:9007/v2"},
            ],
        }
        with pytest.raises(ValueError, match="supportedInterfaces present but no usable"):
            select_interface(card)

    def test_skips_unsupported_version_prefers_supported(self):
        card = {
            "name": "Mixed Future Agent",
            "supportedInterfaces": [
                {"protocolBinding": "JSONRPC", "protocolVersion": "2.0", "url": "http://localhost:9007/v2"},
                {"protocolBinding": "JSONRPC", "protocolVersion": "1.0", "url": "http://localhost:9007/v1"},
            ],
        }
        ri = select_interface(card)
        assert ri.protocol_version == "1.0"
        assert ri.url == "http://localhost:9007/v1"

    def test_raises_when_no_usable_interface(self):
        card = {"name": "Useless", "supportedInterfaces": [
            {"protocolBinding": "gRPC", "url": "grpc://localhost:50051"},
        ]}
        with pytest.raises(ValueError, match="No usable JSON-RPC interface"):
            select_interface(card)

    def test_raises_when_no_url(self):
        card = {"name": "Empty"}
        with pytest.raises(ValueError, match="No usable JSON-RPC interface"):
            select_interface(card)


class TestSelectFallbackInterface:
    def test_v03_primary_returns_none(self):
        primary = ResolvedInterface(binding="JSONRPC", protocol_version="0.3", url="http://localhost:9001/")
        assert select_fallback_interface(V03_CARD, primary) is None

    def test_dual_mode_returns_v03_url(self):
        primary = ResolvedInterface(binding="JSONRPC", protocol_version="1.0", url="http://localhost:9003/v1")
        fb = select_fallback_interface(DUAL_MODE_CARD, primary)
        assert fb is not None
        assert fb.protocol_version == "0.3"
        assert fb.url == "http://localhost:9003/v03"

    def test_single_v10_synthesizes_fallback_at_same_url(self):
        primary = ResolvedInterface(binding="JSONRPC", protocol_version="1.0", url="http://localhost:9002/a2a")
        fb = select_fallback_interface(V10_CARD, primary)
        assert fb is not None
        assert fb.protocol_version == "0.3"
        assert fb.url == "http://localhost:9002/a2a"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_a2a_compat.py::TestSelectInterface -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Implement select_interface and select_fallback_interface**

Add to `hub/a2a_compat.py`:

```python
_SUPPORTED_VERSIONS: set[str] = {"0.3", "1.0"}


def select_interface(card: dict) -> ResolvedInterface:
    """Select the best JSON-RPC interface from an agent card.

    For v1.0 cards with supportedInterfaces: filter to JSONRPC, prefer v1.0.
    Only accepts versions in _SUPPORTED_VERSIONS (0.3, 1.0); interfaces
    advertising unsupported versions (e.g. 1.1, 2.0) are skipped.
    For v0.3 cards (no supportedInterfaces at all): use top-level url as v0.3.
    Raises ValueError if no usable JSON-RPC interface found.

    Important: the top-level url fallback is ONLY used when supportedInterfaces
    is absent. If supportedInterfaces is present but contains no supported
    versions (e.g. all 2.0), this raises ValueError rather than silently
    degrading to v0.3 via top-level url.
    """
    interfaces = card.get("supportedInterfaces", None)

    if interfaces is not None:
        jsonrpc: list[ResolvedInterface] = []
        for iface in interfaces:
            if iface.get("protocolBinding", "") != "JSONRPC":
                continue
            url = iface.get("url", "")
            if not url:
                continue
            pv = iface.get("protocolVersion", "0.3")
            if pv not in _SUPPORTED_VERSIONS:
                continue
            jsonrpc.append(ResolvedInterface(binding="JSONRPC", protocol_version=pv, url=url))

        if jsonrpc:
            for target in ("1.0", "0.3"):
                for ri in jsonrpc:
                    if ri.protocol_version == target:
                        return ri
            return jsonrpc[0]

        raise ValueError(
            "supportedInterfaces present but no usable JSON-RPC interface "
            f"(supported versions: {_SUPPORTED_VERSIONS})"
        )

    url = card.get("url", "")
    if url:
        return ResolvedInterface(binding="JSONRPC", protocol_version="0.3", url=url)

    raise ValueError("No usable JSON-RPC interface in agent card")


def select_fallback_interface(
    card: dict, primary: ResolvedInterface,
) -> ResolvedInterface | None:
    """Select a v0.3 fallback interface for retry after v1.0 failure.

    Returns None if primary is already v0.3 (no fallback needed).
    For dual-mode cards: returns the v0.3 JSONRPC URL from the card.
    For single-interface v1.0 cards: synthesizes fallback at the same URL.
    """
    if primary.protocol_version == "0.3":
        return None

    for iface in card.get("supportedInterfaces", []):
        if iface.get("protocolBinding", "") != "JSONRPC":
            continue
        pv = iface.get("protocolVersion", "0.3")
        url = iface.get("url", "")
        if pv == "0.3" and url:
            return ResolvedInterface(binding="JSONRPC", protocol_version="0.3", url=url)

    return ResolvedInterface(binding="JSONRPC", protocol_version="0.3", url=primary.url)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_a2a_compat.py::TestSelectInterface tests/test_a2a_compat.py::TestSelectFallbackInterface -v`
Expected: PASS (all 14 tests)

- [ ] **Step 5: Commit**

```bash
git add hub/a2a_compat.py tests/test_a2a_compat.py
git commit -m "feat(a2a-compat): add interface selection and fallback logic"
```

---

### Task 4: Wire Format Mapping (Methods, Headers, States, Roles)

**Files:**
- Modify: `hub/a2a_compat.py`
- Modify: `tests/test_a2a_compat.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_a2a_compat.py`:

```python
from hub.a2a_compat import get_headers, get_method_name, normalize_role, normalize_task_state


class TestGetMethodName:
    def test_v03_methods(self):
        assert get_method_name("send", "0.3") == "message/send"
        assert get_method_name("stream", "0.3") == "message/stream"
        assert get_method_name("get_task", "0.3") == "tasks/get"
        assert get_method_name("cancel_task", "0.3") == "tasks/cancel"

    def test_v10_methods(self):
        assert get_method_name("send", "1.0") == "SendMessage"
        assert get_method_name("stream", "1.0") == "SendStreamingMessage"
        assert get_method_name("get_task", "1.0") == "GetTask"
        assert get_method_name("cancel_task", "1.0") == "CancelTask"

    def test_unknown_method_raises(self):
        with pytest.raises(KeyError):
            get_method_name("unknown", "1.0")

    def test_unsupported_version_raises(self):
        with pytest.raises(ValueError, match="Unsupported protocol version"):
            get_method_name("send", "2.0")


class TestGetHeaders:
    def test_v10_includes_version_header(self):
        h = get_headers("1.0")
        assert h == {"A2A-Version": "1.0"}

    def test_v03_returns_empty(self):
        assert get_headers("0.3") == {}


class TestNormalizeTaskState:
    def test_v10_screaming_snake_to_lowercase(self):
        assert normalize_task_state("TASK_STATE_COMPLETED") == "completed"
        assert normalize_task_state("TASK_STATE_FAILED") == "failed"
        assert normalize_task_state("TASK_STATE_CANCELED") == "canceled"
        assert normalize_task_state("TASK_STATE_SUBMITTED") == "submitted"
        assert normalize_task_state("TASK_STATE_WORKING") == "working"
        assert normalize_task_state("TASK_STATE_REJECTED") == "rejected"
        assert normalize_task_state("TASK_STATE_INPUT_REQUIRED") == "input-required"
        assert normalize_task_state("TASK_STATE_AUTH_REQUIRED") == "auth-required"

    def test_already_lowercase_passes_through(self):
        assert normalize_task_state("completed") == "completed"
        assert normalize_task_state("working") == "working"

    def test_unknown_state_passes_through(self):
        assert normalize_task_state("some-future-state") == "some-future-state"


class TestNormalizeRole:
    def test_v10_screaming_to_lowercase(self):
        assert normalize_role("ROLE_USER") == "user"
        assert normalize_role("ROLE_AGENT") == "agent"

    def test_already_lowercase_passes_through(self):
        assert normalize_role("user") == "user"
        assert normalize_role("agent") == "agent"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_a2a_compat.py::TestGetMethodName -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Implement mapping functions**

Add to `hub/a2a_compat.py`:

```python
_METHOD_MAP_V03: dict[str, str] = {
    "send": "message/send",
    "stream": "message/stream",
    "get_task": "tasks/get",
    "cancel_task": "tasks/cancel",
}

_METHOD_MAP_V10: dict[str, str] = {
    "send": "SendMessage",
    "stream": "SendStreamingMessage",
    "get_task": "GetTask",
    "cancel_task": "CancelTask",
}

_V10_STATE_MAP: dict[str, str] = {
    "TASK_STATE_SUBMITTED": "submitted",
    "TASK_STATE_WORKING": "working",
    "TASK_STATE_COMPLETED": "completed",
    "TASK_STATE_FAILED": "failed",
    "TASK_STATE_CANCELED": "canceled",
    "TASK_STATE_REJECTED": "rejected",
    "TASK_STATE_INPUT_REQUIRED": "input-required",
    "TASK_STATE_AUTH_REQUIRED": "auth-required",
}

_V10_ROLE_MAP: dict[str, str] = {
    "ROLE_USER": "user",
    "ROLE_AGENT": "agent",
}


def get_method_name(base_method: str, version: str) -> str:
    """Map a canonical method name to the versioned JSON-RPC method string."""
    if version == "1.0":
        return _METHOD_MAP_V10[base_method]
    if version == "0.3":
        return _METHOD_MAP_V03[base_method]
    raise ValueError(f"Unsupported protocol version: {version}")


def get_headers(version: str) -> dict[str, str]:
    """Return extra HTTP headers for the given protocol version."""
    if version == "1.0":
        return {"A2A-Version": "1.0"}
    return {}


def normalize_task_state(state: str) -> str:
    """Normalize a task state to canonical lowercase."""
    return _V10_STATE_MAP.get(state, state)


def normalize_role(role: str) -> str:
    """Normalize a role to canonical lowercase."""
    return _V10_ROLE_MAP.get(role, role)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_a2a_compat.py::TestGetMethodName tests/test_a2a_compat.py::TestGetHeaders tests/test_a2a_compat.py::TestNormalizeTaskState tests/test_a2a_compat.py::TestNormalizeRole -v`
Expected: PASS (all 15 tests)

- [ ] **Step 5: Commit**

```bash
git add hub/a2a_compat.py tests/test_a2a_compat.py
git commit -m "feat(a2a-compat): add method/header/state/role mapping functions"
```

---

### Task 5: Part Conversion — Outbound and Inbound (All Part Types)

**Files:**
- Modify: `hub/a2a_compat.py`
- Modify: `tests/test_a2a_compat.py`

- [ ] **Step 1: Write failing tests for build_message_parts (outbound) and normalize_inbound_parts (inbound)**

Add to `tests/test_a2a_compat.py`:

```python
from hub.a2a_compat import build_message_parts, normalize_inbound_parts


class TestBuildMessageParts:
    """Canonical (flattened) -> versioned wire format (outbound)."""

    # ── v1.0: canonical passes through ──

    def test_v10_text_passes_through(self):
        parts = [{"text": "hello"}]
        assert build_message_parts(parts, "1.0") == [{"text": "hello"}]

    def test_v10_file_url_passes_through(self):
        parts = [{"url": "https://example.com/f.pdf", "mediaType": "application/pdf", "filename": "f.pdf"}]
        assert build_message_parts(parts, "1.0") == parts

    def test_v10_data_passes_through(self):
        parts = [{"data": {"key": "val"}}]
        assert build_message_parts(parts, "1.0") == parts

    # ── v0.3: canonical -> v0.3 wire ──

    def test_v03_text_adds_kind(self):
        parts = [{"text": "hello"}]
        result = build_message_parts(parts, "0.3")
        assert result == [{"kind": "text", "text": "hello"}]

    def test_v03_text_preserves_metadata(self):
        parts = [{"text": "hello", "metadata": {"lang": "en"}}]
        result = build_message_parts(parts, "0.3")
        assert result == [{"kind": "text", "text": "hello", "metadata": {"lang": "en"}}]

    def test_v03_file_by_url(self):
        parts = [{"url": "https://example.com/doc.pdf", "mediaType": "application/pdf", "filename": "doc.pdf"}]
        result = build_message_parts(parts, "0.3")
        assert result == [{
            "kind": "file",
            "file": {"uri": "https://example.com/doc.pdf", "mimeType": "application/pdf", "name": "doc.pdf"},
        }]

    def test_v03_file_by_bytes(self):
        parts = [{"raw": "dGVzdA==", "mediaType": "image/png", "filename": "img.png"}]
        result = build_message_parts(parts, "0.3")
        assert result == [{
            "kind": "file",
            "file": {"bytes": "dGVzdA==", "mimeType": "image/png", "name": "img.png"},
        }]

    def test_v03_data_part(self):
        parts = [{"data": {"key": "val"}}]
        result = build_message_parts(parts, "0.3")
        assert result == [{"kind": "data", "data": {"key": "val"}}]

    def test_v03_data_preserves_metadata(self):
        parts = [{"data": {"key": "val"}, "metadata": {"source": "db"}}]
        result = build_message_parts(parts, "0.3")
        assert result == [{"kind": "data", "data": {"key": "val"}, "metadata": {"source": "db"}}]

    def test_v03_mixed_parts(self):
        parts = [
            {"text": "See attached:"},
            {"url": "https://example.com/file.pdf", "mediaType": "application/pdf"},
        ]
        result = build_message_parts(parts, "0.3")
        assert len(result) == 2
        assert result[0] == {"kind": "text", "text": "See attached:"}
        assert result[1]["kind"] == "file"
        assert result[1]["file"]["uri"] == "https://example.com/file.pdf"

    def test_v03_unknown_part_passes_through(self):
        parts = [{"custom_field": "val"}]
        result = build_message_parts(parts, "0.3")
        assert result == [{"custom_field": "val"}]

    def test_empty_parts(self):
        assert build_message_parts([], "0.3") == []
        assert build_message_parts([], "1.0") == []


class TestNormalizeInboundParts:
    """Inbound wire parts -> canonical (flattened) format."""

    # ── v0.3 inbound: strip kind, unnest file, rename fields ──

    def test_v03_text_strips_kind(self):
        parts = [{"kind": "text", "text": "hello"}]
        assert normalize_inbound_parts(parts, "0.3") == [{"text": "hello"}]

    def test_v03_file_by_uri_unnested(self):
        parts = [{"kind": "file", "file": {"uri": "https://example.com/doc.pdf", "mimeType": "application/pdf", "name": "doc.pdf"}}]
        result = normalize_inbound_parts(parts, "0.3")
        assert result == [{"url": "https://example.com/doc.pdf", "mediaType": "application/pdf", "filename": "doc.pdf"}]

    def test_v03_file_by_bytes_unnested(self):
        parts = [{"kind": "file", "file": {"bytes": "dGVzdA==", "mimeType": "image/png", "name": "img.png"}}]
        result = normalize_inbound_parts(parts, "0.3")
        assert result == [{"raw": "dGVzdA==", "mediaType": "image/png", "filename": "img.png"}]

    def test_v03_data_strips_kind(self):
        parts = [{"kind": "data", "data": {"key": "val"}}]
        result = normalize_inbound_parts(parts, "0.3")
        assert result == [{"data": {"key": "val"}}]

    def test_v03_data_preserves_metadata(self):
        parts = [{"kind": "data", "data": {"key": "val"}, "metadata": {"source": "db"}}]
        result = normalize_inbound_parts(parts, "0.3")
        assert result == [{"data": {"key": "val"}, "metadata": {"source": "db"}}]

    def test_v03_text_preserves_metadata(self):
        parts = [{"kind": "text", "text": "hi", "metadata": {"x": 1}}]
        result = normalize_inbound_parts(parts, "0.3")
        assert result == [{"text": "hi", "metadata": {"x": 1}}]

    def test_v03_already_flattened_passes_through(self):
        parts = [{"text": "already flat"}]
        assert normalize_inbound_parts(parts, "0.3") == [{"text": "already flat"}]

    def test_v03_unknown_part_passes_through(self):
        parts = [{"custom": "field"}]
        assert normalize_inbound_parts(parts, "0.3") == [{"custom": "field"}]

    # ── v1.0 inbound: already canonical, minor cleanup ──

    def test_v10_passes_through(self):
        parts = [{"text": "hello"}, {"url": "https://example.com/f.pdf"}]
        assert normalize_inbound_parts(parts, "1.0") == parts

    def test_v10_strips_stale_kind(self):
        parts = [{"kind": "text", "text": "hello"}]
        result = normalize_inbound_parts(parts, "1.0")
        assert result == [{"text": "hello"}]

    def test_v10_renames_mimeType_to_mediaType(self):
        parts = [{"url": "https://example.com/f.pdf", "mimeType": "application/pdf"}]
        result = normalize_inbound_parts(parts, "1.0")
        assert result == [{"url": "https://example.com/f.pdf", "mediaType": "application/pdf"}]

    # ── round-trip tests ──

    def test_roundtrip_v03_text(self):
        canonical = [{"text": "hello"}]
        wire = build_message_parts(canonical, "0.3")
        back = normalize_inbound_parts(wire, "0.3")
        assert back == canonical

    def test_roundtrip_v03_file_by_url(self):
        canonical = [{"url": "https://example.com/doc.pdf", "mediaType": "application/pdf", "filename": "doc.pdf"}]
        wire = build_message_parts(canonical, "0.3")
        back = normalize_inbound_parts(wire, "0.3")
        assert back == canonical

    def test_roundtrip_v03_file_by_bytes(self):
        canonical = [{"raw": "dGVzdA==", "mediaType": "image/png", "filename": "img.png"}]
        wire = build_message_parts(canonical, "0.3")
        back = normalize_inbound_parts(wire, "0.3")
        assert back == canonical

    def test_roundtrip_v03_data(self):
        canonical = [{"data": {"key": "val"}}]
        wire = build_message_parts(canonical, "0.3")
        back = normalize_inbound_parts(wire, "0.3")
        assert back == canonical

    def test_roundtrip_v10_text(self):
        canonical = [{"text": "hello"}]
        wire = build_message_parts(canonical, "1.0")
        back = normalize_inbound_parts(wire, "1.0")
        assert back == canonical

    def test_empty_parts(self):
        assert normalize_inbound_parts([], "0.3") == []
        assert normalize_inbound_parts([], "1.0") == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_a2a_compat.py::TestBuildMessageParts -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Implement build_message_parts (outbound) and normalize_inbound_parts (inbound)**

Add to `hub/a2a_compat.py`:

```python
def build_message_parts(parts: list[dict], version: str) -> list[dict]:
    """Convert canonical (flattened) parts to the target version's wire format.

    Canonical format matches v1.0: {"text": "..."}, {"url": "..."}, {"data": {...}}.
    For v0.3: adds `kind` discriminator, nests file fields under `file` key,
    renames mediaType->mimeType, filename->name, url->uri, raw->bytes.
    """
    if version != "0.3":
        return parts

    result: list[dict] = []
    for p in parts:
        if "text" in p:
            out: dict[str, Any] = {"kind": "text", "text": p["text"]}
            if "metadata" in p:
                out["metadata"] = p["metadata"]
            result.append(out)
        elif "url" in p:
            file_dict: dict[str, Any] = {"uri": p["url"]}
            if "mediaType" in p:
                file_dict["mimeType"] = p["mediaType"]
            if "filename" in p:
                file_dict["name"] = p["filename"]
            result.append({"kind": "file", "file": file_dict})
        elif "raw" in p:
            file_dict = {"bytes": p["raw"]}
            if "mediaType" in p:
                file_dict["mimeType"] = p["mediaType"]
            if "filename" in p:
                file_dict["name"] = p["filename"]
            result.append({"kind": "file", "file": file_dict})
        elif "data" in p:
            out = {"kind": "data", "data": p["data"]}
            if "metadata" in p:
                out["metadata"] = p["metadata"]
            result.append(out)
        else:
            result.append(p)
    return result


def normalize_inbound_parts(parts: list[dict], version: str) -> list[dict]:
    """Normalize inbound wire parts to canonical (flattened) format.

    For v0.3: strips `kind`, unnests file fields, renames mimeType->mediaType,
    name->filename, uri->url, bytes->raw.
    For v1.0: strips stale `kind` if present, renames mimeType->mediaType.
    """
    result: list[dict] = []
    for p in parts:
        kind = p.get("kind", "")
        if kind == "text":
            out: dict[str, Any] = {"text": p["text"]}
            if "metadata" in p:
                out["metadata"] = p["metadata"]
            result.append(out)
        elif kind == "file":
            f = p.get("file", {})
            out = {}
            if "uri" in f:
                out["url"] = f["uri"]
            if "bytes" in f:
                out["raw"] = f["bytes"]
            if "mimeType" in f:
                out["mediaType"] = f["mimeType"]
            if "name" in f:
                out["filename"] = f["name"]
            result.append(out)
        elif kind == "data":
            out = {"data": p["data"]}
            if "metadata" in p:
                out["metadata"] = p["metadata"]
            result.append(out)
        elif kind:
            result.append(p)
        else:
            out = dict(p)
            if "kind" in out:
                del out["kind"]
            if "mimeType" in out:
                out["mediaType"] = out.pop("mimeType")
            result.append(out)
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_a2a_compat.py::TestBuildMessageParts tests/test_a2a_compat.py::TestNormalizeInboundParts -v`
Expected: PASS (all 30 tests)

- [ ] **Step 5: Commit**

```bash
git add hub/a2a_compat.py tests/test_a2a_compat.py
git commit -m "feat(a2a-compat): add build_message_parts and normalize_inbound_parts for all part types"
```

---

### Task 6: Request Params Building

**Files:**
- Modify: `hub/a2a_compat.py`
- Modify: `tests/test_a2a_compat.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_a2a_compat.py`:

```python
from hub.a2a_compat import build_request_params


class TestBuildRequestParams:
    def test_v10_passes_parts_through(self):
        msg = {"role": "user", "parts": [{"text": "hi"}], "messageId": "m1"}
        params = build_request_params(msg, "1.0")
        assert params["message"]["parts"] == [{"text": "hi"}]
        assert "configuration" not in params

    def test_v03_adds_kind_to_parts(self):
        msg = {"role": "user", "parts": [{"text": "hi"}], "messageId": "m1"}
        params = build_request_params(msg, "0.3")
        assert params["message"]["parts"] == [{"kind": "text", "text": "hi"}]

    def test_with_configuration(self):
        msg = {"role": "user", "parts": [{"text": "hi"}]}
        params = build_request_params(msg, "1.0", configuration={"blocking": True})
        assert params["configuration"] == {"blocking": True}

    def test_does_not_mutate_original(self):
        msg = {"role": "user", "parts": [{"text": "hi"}]}
        original_parts = msg["parts"]
        build_request_params(msg, "0.3")
        assert msg["parts"] is original_parts
        assert "kind" not in msg["parts"][0]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_a2a_compat.py::TestBuildRequestParams -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Implement build_request_params**

Add to `hub/a2a_compat.py`:

```python
def build_request_params(
    message_dict: dict,
    version: str,
    configuration: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build JSON-RPC params for a message send/stream request."""
    msg = dict(message_dict)
    if "parts" in msg:
        msg["parts"] = build_message_parts(msg["parts"], version)
    params: dict[str, Any] = {"message": msg}
    if configuration:
        params["configuration"] = configuration
    return params
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_a2a_compat.py::TestBuildRequestParams -v`
Expected: PASS (all 4 tests)

- [ ] **Step 5: Commit**

```bash
git add hub/a2a_compat.py tests/test_a2a_compat.py
git commit -m "feat(a2a-compat): add build_request_params"
```

---

### Task 7: Response Extraction

**Files:**
- Modify: `hub/a2a_compat.py`
- Modify: `tests/test_a2a_compat.py`

- [ ] **Step 1: Write failing tests for extract_response**

Add to `tests/test_a2a_compat.py`:

```python
from hub.a2a_compat import extract_response


class TestExtractResponse:
    """extract_response normalizes responses to canonical format compatible
    with the existing _extract_response_content dispatcher method.
    Both v0.3 and v1.0 responses have parts normalized to canonical (flattened)."""

    def test_v03_normalizes_parts(self):
        raw = {
            "jsonrpc": "2.0",
            "id": "1",
            "result": {
                "kind": "task",
                "id": "t-1",
                "status": {
                    "state": "completed",
                    "message": {
                        "role": "agent",
                        "parts": [{"kind": "text", "text": "done"}],
                    },
                },
            },
        }
        result = extract_response(raw, "0.3")
        assert result["result"]["status"]["message"]["parts"] == [{"text": "done"}]

    def test_v03_normalizes_artifact_parts(self):
        raw = {
            "jsonrpc": "2.0",
            "id": "1",
            "result": {
                "kind": "task",
                "id": "t-1",
                "status": {"state": "completed"},
                "artifacts": [
                    {"parts": [{"kind": "file", "file": {"uri": "https://example.com/f.pdf", "mimeType": "application/pdf", "name": "doc.pdf"}}]},
                ],
            },
        }
        result = extract_response(raw, "0.3")
        assert result["result"]["artifacts"][0]["parts"] == [{"url": "https://example.com/f.pdf", "mediaType": "application/pdf", "filename": "doc.pdf"}]

    def test_v03_normalizes_message_kind_parts(self):
        raw = {
            "jsonrpc": "2.0",
            "id": "1",
            "result": {
                "kind": "message",
                "parts": [{"kind": "text", "text": "hello"}, {"kind": "data", "data": {"k": "v"}}],
            },
        }
        result = extract_response(raw, "0.3")
        assert result["result"]["parts"] == [{"text": "hello"}, {"data": {"k": "v"}}]

    def test_v10_task_unwrapped_and_normalized(self):
        raw = {
            "jsonrpc": "2.0",
            "id": "1",
            "result": {
                "task": {
                    "id": "t-1",
                    "status": {
                        "state": "TASK_STATE_COMPLETED",
                        "message": {
                            "role": "ROLE_AGENT",
                            "parts": [{"text": "done"}],
                        },
                    },
                },
            },
        }
        result = extract_response(raw, "1.0")
        inner = result["result"]
        assert inner["kind"] == "task"
        assert inner["id"] == "t-1"
        assert inner["status"]["state"] == "completed"
        assert inner["status"]["message"]["role"] == "agent"

    def test_v10_message_unwrapped_and_normalized(self):
        raw = {
            "jsonrpc": "2.0",
            "id": "1",
            "result": {
                "message": {
                    "role": "ROLE_AGENT",
                    "parts": [{"text": "hi"}],
                    "messageId": "m-1",
                },
            },
        }
        result = extract_response(raw, "1.0")
        inner = result["result"]
        assert inner["kind"] == "message"
        assert inner["role"] == "agent"
        assert inner["parts"] == [{"text": "hi"}]

    def test_v10_direct_task_normalized(self):
        """GetTask response: result IS the task directly (no oneof wrapper)."""
        raw = {
            "jsonrpc": "2.0",
            "id": "1",
            "result": {
                "id": "t-1",
                "status": {
                    "state": "TASK_STATE_WORKING",
                },
            },
        }
        result = extract_response(raw, "1.0")
        assert result["result"]["status"]["state"] == "working"

    def test_v10_task_with_artifacts(self):
        raw = {
            "jsonrpc": "2.0",
            "id": "1",
            "result": {
                "task": {
                    "id": "t-1",
                    "status": {"state": "TASK_STATE_COMPLETED"},
                    "artifacts": [
                        {"parts": [{"text": "artifact text"}]},
                    ],
                },
            },
        }
        result = extract_response(raw, "1.0")
        inner = result["result"]
        assert inner["kind"] == "task"
        assert inner["status"]["state"] == "completed"
        assert inner["artifacts"][0]["parts"] == [{"text": "artifact text"}]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_a2a_compat.py::TestExtractResponse -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Implement extract_response and helper**

Add to `hub/a2a_compat.py`:

```python
def extract_response(raw: dict, version: str) -> dict:
    """Normalize a JSON-RPC response to canonical format.

    Both v0.3 and v1.0 responses are normalized:
    - Parts in all messages/artifacts are converted to canonical (flattened) form.
    - v0.3: strips kind, unnests file fields, renames mimeType/name/uri/bytes.
    - v1.0: unwraps oneof wrapper (task/message), adds `kind` field,
      normalizes SCREAMING_SNAKE states/roles, normalizes parts.
    """
    if version != "1.0":
        _normalize_parts_in_result(raw.get("result", raw), version)
        return raw

    inner = raw.get("result", raw)

    if "task" in inner and isinstance(inner["task"], dict):
        task = inner["task"]
        task["kind"] = "task"
        _normalize_v10_task(task, version)
        return {"result": task}

    if "message" in inner and isinstance(inner["message"], dict) and "parts" in inner["message"]:
        msg = inner["message"]
        msg["kind"] = "message"
        if "role" in msg:
            msg["role"] = normalize_role(msg["role"])
        if "parts" in msg:
            msg["parts"] = normalize_inbound_parts(msg["parts"], version)
        return {"result": msg}

    if "status" in inner:
        _normalize_v10_task(inner, version)

    return raw


def _normalize_v10_task(task: dict, version: str) -> None:
    """Normalize task fields (states, roles, parts) to canonical form in place."""
    status = task.get("status", {})
    if "state" in status:
        status["state"] = normalize_task_state(status["state"])
    msg = status.get("message", {})
    if "role" in msg:
        msg["role"] = normalize_role(msg["role"])
    if "parts" in msg:
        msg["parts"] = normalize_inbound_parts(msg["parts"], version)
    for artifact in task.get("artifacts", []):
        if "parts" in artifact:
            artifact["parts"] = normalize_inbound_parts(artifact["parts"], version)


def _normalize_parts_in_result(inner: dict, version: str) -> None:
    """Normalize parts in a v0.3 result dict (task or message) in place."""
    if not isinstance(inner, dict):
        return
    # Message-kind response
    if "parts" in inner:
        inner["parts"] = normalize_inbound_parts(inner["parts"], version)
    # Task status message
    msg = inner.get("status", {}).get("message", {})
    if "parts" in msg:
        msg["parts"] = normalize_inbound_parts(msg["parts"], version)
    # Artifacts
    for artifact in inner.get("artifacts", []):
        if "parts" in artifact:
            artifact["parts"] = normalize_inbound_parts(artifact["parts"], version)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_a2a_compat.py::TestExtractResponse -v`
Expected: PASS (all 8 tests)

- [ ] **Step 5: Commit**

```bash
git add hub/a2a_compat.py tests/test_a2a_compat.py
git commit -m "feat(a2a-compat): add extract_response with inbound part normalization"
```

---

### Task 8: Stream Event Classification

**Files:**
- Modify: `hub/a2a_compat.py`
- Modify: `tests/test_a2a_compat.py`

- [ ] **Step 1: Write failing tests for classify_stream_event**

Add to `tests/test_a2a_compat.py`:

```python
from hub.a2a_compat import classify_stream_event


class TestClassifyStreamEvent:
    # ── v0.3 events ──

    def test_v03_status_update(self):
        data = {"kind": "status-update", "status": {"state": "working"}, "final": False}
        event_type, payload = classify_stream_event(data, "0.3")
        assert event_type == "status-update"
        assert payload["status"]["state"] == "working"

    def test_v03_artifact_update_normalizes_parts(self):
        data = {"kind": "artifact-update", "artifact": {"parts": [{"kind": "text", "text": "chunk"}]}}
        event_type, payload = classify_stream_event(data, "0.3")
        assert event_type == "artifact-update"
        assert payload["artifact"]["parts"] == [{"text": "chunk"}]

    def test_v03_task(self):
        data = {"kind": "task", "id": "t-1"}
        event_type, payload = classify_stream_event(data, "0.3")
        assert event_type == "task"
        assert payload["id"] == "t-1"

    def test_v03_message_normalizes_parts(self):
        data = {"kind": "message", "parts": [{"kind": "text", "text": "hi"}]}
        event_type, payload = classify_stream_event(data, "0.3")
        assert event_type == "message"
        assert payload["parts"] == [{"text": "hi"}]

    def test_v03_unknown_kind_returns_none(self):
        data = {"kind": "unknown-thing"}
        assert classify_stream_event(data, "0.3") is None

    # ── v1.0 events ──

    def test_v10_status_update(self):
        data = {"statusUpdate": {"status": {"state": "TASK_STATE_WORKING"}}}
        event_type, payload = classify_stream_event(data, "1.0")
        assert event_type == "status-update"
        assert payload["status"]["state"] == "working"

    def test_v10_status_update_terminal_sets_final(self):
        data = {"statusUpdate": {"status": {"state": "TASK_STATE_COMPLETED"}}}
        _, payload = classify_stream_event(data, "1.0")
        assert payload["final"] is True

    def test_v10_status_update_nonterminal_sets_final_false(self):
        data = {"statusUpdate": {"status": {"state": "TASK_STATE_WORKING"}}}
        _, payload = classify_stream_event(data, "1.0")
        assert payload["final"] is False

    def test_v10_artifact_update(self):
        data = {"artifactUpdate": {"artifact": {"parts": [{"text": "chunk"}]}, "append": True}}
        event_type, payload = classify_stream_event(data, "1.0")
        assert event_type == "artifact-update"
        assert payload["artifact"]["parts"] == [{"text": "chunk"}]

    def test_v10_task(self):
        data = {"task": {"id": "t-1", "status": {"state": "TASK_STATE_SUBMITTED"}}}
        event_type, payload = classify_stream_event(data, "1.0")
        assert event_type == "task"
        assert payload["id"] == "t-1"
        assert payload["status"]["state"] == "submitted"

    def test_v10_message(self):
        data = {"message": {"role": "ROLE_AGENT", "parts": [{"text": "hi"}]}}
        event_type, payload = classify_stream_event(data, "1.0")
        assert event_type == "message"
        assert payload["role"] == "agent"

    def test_v10_unknown_returns_none(self):
        data = {"somethingNew": {"value": 1}}
        assert classify_stream_event(data, "1.0") is None

    def test_v10_status_normalizes_role_in_message(self):
        data = {
            "statusUpdate": {
                "status": {
                    "state": "TASK_STATE_INPUT_REQUIRED",
                    "message": {"role": "ROLE_AGENT", "parts": [{"text": "need input"}]},
                },
            },
        }
        _, payload = classify_stream_event(data, "1.0")
        assert payload["status"]["state"] == "input-required"
        assert payload["status"]["message"]["role"] == "agent"
        assert payload["final"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_a2a_compat.py::TestClassifyStreamEvent -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Implement classify_stream_event**

Add to `hub/a2a_compat.py`:

```python
def classify_stream_event(
    data: dict, version: str,
) -> tuple[str, dict] | None:
    """Classify a streaming SSE event and normalize to canonical format.

    Returns (canonical_event_type, normalized_payload) or None if unrecognized.
    Both v0.3 and v1.0: parts in the payload are normalized to canonical flattened form.
    v0.3: uses `kind` discriminator.
    v1.0: uses ProtoJSON camelCase keys (statusUpdate, artifactUpdate, task, message).
    """
    if version == "1.0":
        return _classify_v10(data)

    kind = data.get("kind", "")
    if kind in ("status-update", "artifact-update", "task", "message"):
        _normalize_stream_event_parts(data, version)
        return (kind, data)
    return None


def _normalize_stream_event_parts(data: dict, version: str) -> None:
    """Normalize parts within a stream event payload in place."""
    if "parts" in data:
        data["parts"] = normalize_inbound_parts(data["parts"], version)
    artifact = data.get("artifact", {})
    if "parts" in artifact:
        artifact["parts"] = normalize_inbound_parts(artifact["parts"], version)
    msg = data.get("status", {}).get("message", {})
    if "parts" in msg:
        msg["parts"] = normalize_inbound_parts(msg["parts"], version)


def _classify_v10(data: dict) -> tuple[str, dict] | None:
    if "statusUpdate" in data:
        update = data["statusUpdate"]
        status = update.get("status", {})
        if "state" in status:
            status["state"] = normalize_task_state(status["state"])
        msg = status.get("message", {})
        if "role" in msg:
            msg["role"] = normalize_role(msg["role"])
        if "parts" in msg:
            msg["parts"] = normalize_inbound_parts(msg["parts"], "1.0")
        state = status.get("state", "")
        update["final"] = state in CANONICAL_TERMINAL_STATES
        update["status"] = status
        return ("status-update", update)

    if "artifactUpdate" in data:
        payload = data["artifactUpdate"]
        artifact = payload.get("artifact", {})
        if "parts" in artifact:
            artifact["parts"] = normalize_inbound_parts(artifact["parts"], "1.0")
        return ("artifact-update", payload)

    if "task" in data:
        task = data["task"]
        _normalize_v10_task(task, "1.0")
        return ("task", task)

    if "message" in data:
        msg = data["message"]
        if "role" in msg:
            msg["role"] = normalize_role(msg["role"])
        if "parts" in msg:
            msg["parts"] = normalize_inbound_parts(msg["parts"], "1.0")
        return ("message", msg)

    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_a2a_compat.py::TestClassifyStreamEvent -v`
Expected: PASS (all 13 tests)

- [ ] **Step 5: Commit**

```bash
git add hub/a2a_compat.py tests/test_a2a_compat.py
git commit -m "feat(a2a-compat): add classify_stream_event for v0.3 and v1.0"
```

---

### Task 9: JSON-RPC Error Extraction

**Files:**
- Modify: `hub/a2a_compat.py`
- Modify: `tests/test_a2a_compat.py`

- [ ] **Step 1: Write failing tests for extract_jsonrpc_error**

Add to `tests/test_a2a_compat.py`:

```python
from hub.a2a_compat import extract_jsonrpc_error


class TestExtractJsonrpcError:
    def test_no_error_returns_none(self):
        raw = {"jsonrpc": "2.0", "id": "1", "result": {"kind": "task"}}
        assert extract_jsonrpc_error(raw) is None

    def test_extracts_method_not_found(self):
        raw = {"jsonrpc": "2.0", "id": "1", "error": {"code": -32601, "message": "Method not found"}}
        err = extract_jsonrpc_error(raw)
        assert err is not None
        assert err.code == -32601
        assert err.message == "Method not found"
        assert err.data is None

    def test_extracts_version_not_supported(self):
        raw = {"jsonrpc": "2.0", "id": "1", "error": {"code": -32009, "message": "Version not supported", "data": {"supported": ["0.3"]}}}
        err = extract_jsonrpc_error(raw)
        assert err is not None
        assert err.code == -32009
        assert err.data == {"supported": ["0.3"]}

    def test_non_dict_error_returns_none(self):
        raw = {"jsonrpc": "2.0", "id": "1", "error": "string error"}
        assert extract_jsonrpc_error(raw) is None

    def test_missing_code_defaults_to_zero(self):
        raw = {"jsonrpc": "2.0", "id": "1", "error": {"message": "oops"}}
        err = extract_jsonrpc_error(raw)
        assert err is not None
        assert err.code == 0

    def test_empty_dict_returns_none(self):
        assert extract_jsonrpc_error({}) is None

    def test_fallback_eligible_check(self):
        err_32601 = JsonRpcError(code=-32601, message="Method not found")
        err_32009 = JsonRpcError(code=-32009, message="Version not supported")
        err_other = JsonRpcError(code=-32600, message="Invalid Request")
        assert err_32601.code in FALLBACK_ELIGIBLE_CODES
        assert err_32009.code in FALLBACK_ELIGIBLE_CODES
        assert err_other.code not in FALLBACK_ELIGIBLE_CODES
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_a2a_compat.py::TestExtractJsonrpcError -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Implement extract_jsonrpc_error**

Add to `hub/a2a_compat.py`:

```python
def extract_jsonrpc_error(raw: dict) -> JsonRpcError | None:
    """Extract a JSON-RPC error from a response dict, or None if no error."""
    err = raw.get("error")
    if not err or not isinstance(err, dict):
        return None
    return JsonRpcError(
        code=err.get("code", 0),
        message=err.get("message", ""),
        data=err.get("data"),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_a2a_compat.py::TestExtractJsonrpcError -v`
Expected: PASS (all 7 tests)

- [ ] **Step 5: Run all compat tests to verify nothing is broken**

Run: `uv run pytest tests/test_a2a_compat.py -v`
Expected: PASS (all tests)

- [ ] **Step 6: Commit**

```bash
git add hub/a2a_compat.py tests/test_a2a_compat.py
git commit -m "feat(a2a-compat): add extract_jsonrpc_error"
```

---

## Phase 2: Agent Discovery

### Task 10: Modify LocalAgent and Agent Card Handling

**Files:**
- Modify: `hub/agent_registry.py:29-31` (imports)
- Modify: `hub/agent_registry.py:59-70` (`LocalAgent` dataclass)
- Modify: `hub/agent_registry.py:136-167` (`_probe_and_register`)
- Modify: `hub/agent_registry.py:206-224` (`_fetch_agent_card`)
- Modify: `tests/test_agent_registry.py`

- [ ] **Step 1: Write failing tests for v1.0 card discovery**

Add to `tests/test_agent_registry.py` after the `SAMPLE_CARD` fixture:

```python
from hub.a2a_compat import ResolvedInterface


V10_SAMPLE_CARD = {
    "name": "Modern Agent",
    "description": "A v1.0 agent",
    "supportedInterfaces": [
        {
            "protocolBinding": "JSONRPC",
            "protocolVersion": "1.0",
            "url": "http://localhost:9001/a2a",
        },
    ],
    "capabilities": {"streaming": True},
    "skills": [{"id": "s1", "name": "Skill", "description": "A skill", "tags": ["chat"]}],
}

DUAL_MODE_SAMPLE_CARD = {
    "name": "Dual Agent",
    "description": "Speaks both protocols",
    "url": "http://localhost:9001/",
    "supportedInterfaces": [
        {"protocolBinding": "JSONRPC", "protocolVersion": "1.0", "url": "http://localhost:9001/v1"},
        {"protocolBinding": "JSONRPC", "protocolVersion": "0.3", "url": "http://localhost:9001/v03"},
    ],
    "capabilities": {"streaming": False},
    "skills": [],
}


class TestV10Discovery:
    @pytest.mark.asyncio
    async def test_discover_v10_agent(self, config):
        registry = AgentRegistry(config)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = V10_SAMPLE_CARD
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.get = AsyncMock(return_value=mock_resp)
        registry._client = mock_client

        agents = await registry.discover()
        assert len(agents) == 1
        agent = agents[0]
        assert agent.interface.protocol_version == "1.0"
        assert agent.interface.url == "http://localhost:9001/a2a"
        assert agent.fallback_interface is not None
        assert agent.fallback_interface.protocol_version == "0.3"
        await registry.close()

    @pytest.mark.asyncio
    async def test_discover_v03_agent_has_v03_interface(self, config):
        registry = AgentRegistry(config)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = SAMPLE_CARD
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.get = AsyncMock(return_value=mock_resp)
        registry._client = mock_client

        agents = await registry.discover()
        assert len(agents) == 1
        agent = agents[0]
        assert agent.interface.protocol_version == "0.3"
        assert agent.fallback_interface is None
        await registry.close()

    @pytest.mark.asyncio
    async def test_discover_dual_mode_agent(self, config):
        registry = AgentRegistry(config)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = DUAL_MODE_SAMPLE_CARD
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.get = AsyncMock(return_value=mock_resp)
        registry._client = mock_client

        agents = await registry.discover()
        assert len(agents) == 1
        agent = agents[0]
        assert agent.interface.protocol_version == "1.0"
        assert agent.interface.url == "http://localhost:9001/v1"
        assert agent.fallback_interface is not None
        assert agent.fallback_interface.url == "http://localhost:9001/v03"
        await registry.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_agent_registry.py::TestV10Discovery -v`
Expected: FAIL (LocalAgent has no `interface` attribute)

- [ ] **Step 3: Modify imports in agent_registry.py**

Replace the `a2a.types` import block at `hub/agent_registry.py:29-32`:

```python
# OLD:
from a2a.types import AgentCard
from a2a.utils.constants import (
    AGENT_CARD_WELL_KNOWN_PATH,
    PREV_AGENT_CARD_WELL_KNOWN_PATH,
)

# NEW:
from a2a.utils.constants import (
    AGENT_CARD_WELL_KNOWN_PATH,
    PREV_AGENT_CARD_WELL_KNOWN_PATH,
)

from . import a2a_compat
from .a2a_compat import ResolvedInterface
```

- [ ] **Step 4: Add interface fields to LocalAgent dataclass**

Modify `hub/agent_registry.py:59-70`:

```python
# OLD:
@dataclass
class LocalAgent:
    """A discovered local A2A agent."""

    local_agent_id: str
    name: str
    url: str
    description: str = ""
    capabilities: list[str] = field(default_factory=list)
    agent_card: dict = field(default_factory=dict)
    healthy: bool = True

# NEW:
@dataclass
class LocalAgent:
    """A discovered local A2A agent."""

    local_agent_id: str
    name: str
    url: str
    description: str = ""
    capabilities: list[str] = field(default_factory=list)
    agent_card: dict = field(default_factory=dict)
    healthy: bool = True
    interface: ResolvedInterface | None = None
    fallback_interface: ResolvedInterface | None = None

    def __post_init__(self) -> None:
        if self.interface is None:
            self.interface = ResolvedInterface(
                binding="JSONRPC",
                protocol_version="0.3",
                url=self.url,
            )
```

- [ ] **Step 5: Replace AgentCard.model_validate in _fetch_agent_card**

Modify `hub/agent_registry.py:206-224`:

```python
# OLD:
    async def _fetch_agent_card(self, url: str, source: str = "config") -> dict | None:
        """Try each well-known agent card path and return the first valid card.

        Validates the response with AgentCard.model_validate() to reject
        non-agent HTTP 200 responses (e.g. error JSON from unrelated servers).
        """
        client = await self._get_client()
        for path in AGENT_CARD_PATHS:
            try:
                resp = await client.get(f"{url}{path}")
                if resp.status_code == 200:
                    card_data = resp.json()
                    AgentCard.model_validate(card_data)
                    return card_data
            except Exception:
                continue
        if source == "config":
            logger.debug("Agent at %s not reachable", url)
        return None

# NEW:
    async def _fetch_agent_card(self, url: str, source: str = "config") -> dict | None:
        """Try each well-known agent card path and return the first valid card.

        Uses a2a_compat.validate_agent_card() which tries v1.0 protobuf
        then v0.3 Pydantic, tolerating unknown fields for forward compat.
        """
        client = await self._get_client()
        for path in AGENT_CARD_PATHS:
            try:
                resp = await client.get(f"{url}{path}")
                if resp.status_code == 200:
                    card_data = resp.json()
                    if a2a_compat.validate_agent_card(card_data) is not None:
                        return card_data
            except Exception:
                continue
        if source == "config":
            logger.debug("Agent at %s not reachable", url)
        return None
```

- [ ] **Step 6: Add interface selection to _probe_and_register**

Modify `hub/agent_registry.py:136-167`:

```python
# OLD:
    async def _probe_and_register(
        self, url: str, name: str | None = None, source: str = "config"
    ) -> LocalAgent | None:
        """Try to fetch an agent card from a URL and register it."""
        url = _normalize_url(url.rstrip("/"))
        card = await self._fetch_agent_card(url, source)
        if card is None:
            return None

        agent_name = name or card.get("name", f"Agent@{url}")
        existing = next(
            (a for a in self._agents.values() if a.url == url), None
        )
        if existing:
            existing.agent_card = card
            existing.healthy = True
            existing.name = agent_name
            return existing

        local_id = hashlib.sha256(url.encode()).hexdigest()[:12]
        agent = LocalAgent(
            local_agent_id=local_id,
            name=agent_name,
            url=url,
            description=card.get("description", ""),
            capabilities=_extract_capabilities(card),
            agent_card=card,
            healthy=True,
        )
        self._agents[local_id] = agent
        logger.info("Discovered agent: %s at %s (id=%s)", agent_name, url, local_id)
        return agent

# NEW:
    async def _probe_and_register(
        self, url: str, name: str | None = None, source: str = "config"
    ) -> LocalAgent | None:
        """Try to fetch an agent card from a URL and register it."""
        url = _normalize_url(url.rstrip("/"))
        card = await self._fetch_agent_card(url, source)
        if card is None:
            return None

        try:
            interface = a2a_compat.select_interface(card)
        except ValueError:
            logger.debug("Agent card at %s has no usable JSON-RPC interface", url)
            return None
        fallback = a2a_compat.select_fallback_interface(card, interface)

        agent_name = name or card.get("name", f"Agent@{url}")
        existing = next(
            (a for a in self._agents.values() if a.url == url), None
        )
        if existing:
            existing.agent_card = card
            existing.healthy = True
            existing.name = agent_name
            existing.interface = interface
            existing.fallback_interface = fallback
            return existing

        local_id = hashlib.sha256(url.encode()).hexdigest()[:12]
        agent = LocalAgent(
            local_agent_id=local_id,
            name=agent_name,
            url=url,
            description=card.get("description", ""),
            capabilities=_extract_capabilities(card),
            agent_card=card,
            healthy=True,
            interface=interface,
            fallback_interface=fallback,
        )
        self._agents[local_id] = agent
        logger.info(
            "Discovered agent: %s at %s (id=%s, protocol=%s)",
            agent_name, url, local_id, interface.protocol_version,
        )
        return agent
```

- [ ] **Step 7: Run all agent registry tests**

Run: `uv run pytest tests/test_agent_registry.py -v`
Expected: PASS (all existing tests + 3 new v1.0 tests)

- [ ] **Step 8: Commit**

```bash
git add hub/agent_registry.py tests/test_agent_registry.py
git commit -m "feat(agent-registry): dual-protocol discovery with interface selection"
```

---

## Phase 3: Dispatcher Dual Protocol

### Task 11: Modify _build_jsonrpc and HTTP Routing

**Files:**
- Modify: `hub/dispatcher.py:7` (add import)
- Modify: `hub/dispatcher.py:237-254` (`cancel_task`)
- Modify: `hub/dispatcher.py:258-289` (`_fetch_task`)
- Modify: `hub/dispatcher.py:388-412` (`_dispatch_sync`)
- Modify: `hub/dispatcher.py:416-506` (`_dispatch_streaming`)
- Modify: `hub/dispatcher.py:509-532` (`_build_jsonrpc`)

- [ ] **Step 1: Write failing tests for v1.0 method routing**

Add to `tests/test_dispatcher.py`:

```python
from hub.a2a_compat import ResolvedInterface


V10_INTERFACE = ResolvedInterface(binding="JSONRPC", protocol_version="1.0", url="http://localhost:9001/a2a")
V03_INTERFACE = ResolvedInterface(binding="JSONRPC", protocol_version="0.3", url="http://localhost:9001")


@pytest.fixture
def v10_agent():
    return LocalAgent(
        local_agent_id="test_v10",
        name="V1.0 Agent",
        url="http://localhost:9001",
        agent_card={"capabilities": {"streaming": False}},
        interface=V10_INTERFACE,
    )


class TestV10MethodRouting:
    @pytest.mark.asyncio
    async def test_v10_sync_uses_sendmessage(self, v10_agent):
        sent_body = {}

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.is_success = True
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "jsonrpc": "2.0",
            "id": "1",
            "result": {
                "task": {
                    "id": "t-1",
                    "status": {
                        "state": "TASK_STATE_COMPLETED",
                        "message": {"role": "ROLE_AGENT", "parts": [{"text": "done"}]},
                    },
                },
            },
        }

        async def fake_post(url, *, json, headers, **kwargs):
            sent_body.update(json)
            sent_body["_url"] = url
            sent_body["_headers"] = headers
            return mock_resp

        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post = fake_post
        dispatcher = Dispatcher()
        dispatcher._client = mock_client

        events = []
        async for batch in dispatcher.dispatch(
            agent=v10_agent,
            message_dict=SAMPLE_MESSAGE,
            agent_message_id="am-v10-001",
            user_message_id="um-001",
        ):
            events.extend(batch)

        assert sent_body["method"] == "SendMessage"
        assert sent_body["_url"] == "http://localhost:9001/a2a"
        assert sent_body["_headers"]["A2A-Version"] == "1.0"
        assert events[0]["type"] == "agent_response"
        assert events[0]["data"]["content"] == "done"

    @pytest.mark.asyncio
    async def test_v10_cancel_uses_canceltask(self, v10_agent):
        dispatcher = Dispatcher()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post = AsyncMock(return_value=mock_resp)
        dispatcher._client = mock_client

        await dispatcher.cancel_task(v10_agent, "task-xyz")

        call_kwargs = mock_client.post.call_args
        body = call_kwargs[1]["json"] if "json" in call_kwargs[1] else call_kwargs[0][1]
        assert body["method"] == "CancelTask"


class TestFetchTaskErrorHandling:
    @pytest.mark.asyncio
    async def test_fetch_task_raises_on_jsonrpc_error(self, v10_agent):
        """_fetch_task must detect JSON-RPC errors, not pass them as task data."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.is_success = True
        mock_resp.json.return_value = {
            "jsonrpc": "2.0",
            "id": "1",
            "error": {"code": -32600, "message": "Invalid params"},
        }
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post = AsyncMock(return_value=mock_resp)
        dispatcher = Dispatcher()
        dispatcher._client = mock_client

        with pytest.raises(RuntimeError, match="JSON-RPC error -32600"):
            await dispatcher._fetch_task(v10_agent, "task-xyz")

    @pytest.mark.asyncio
    async def test_fetch_task_raises_fallback_on_method_not_found(self, v10_agent):
        """_fetch_task with -32601 raises A2AVersionFallbackError."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.is_success = True
        mock_resp.json.return_value = {
            "jsonrpc": "2.0",
            "id": "1",
            "error": {"code": -32601, "message": "Method not found"},
        }
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post = AsyncMock(return_value=mock_resp)
        dispatcher = Dispatcher()
        dispatcher._client = mock_client

        with pytest.raises(A2AVersionFallbackError):
            await dispatcher._fetch_task(v10_agent, "task-xyz")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_dispatcher.py::TestV10MethodRouting tests/test_dispatcher.py::TestFetchTaskErrorHandling -v`
Expected: FAIL (method is still "message/send", _check_response doesn't exist)

- [ ] **Step 3: Add import to dispatcher.py**

Add at `hub/dispatcher.py` after the existing imports:

```python
from . import a2a_compat
from .a2a_compat import A2AVersionFallbackError, ResolvedInterface
```

- [ ] **Step 4: Modify _build_jsonrpc to accept version**

Replace `hub/dispatcher.py:509-532`:

```python
    @staticmethod
    def _build_jsonrpc(
        message_dict: dict,
        base_method: str,
        version: str,
        configuration: dict[str, Any] | None = None,
    ) -> dict:
        """Build a JSON-RPC 2.0 envelope for an A2A message."""
        method = a2a_compat.get_method_name(base_method, version)
        params = a2a_compat.build_request_params(message_dict, version, configuration)
        return {
            "jsonrpc": "2.0",
            "id": uuid4().hex,
            "method": method,
            "params": params,
        }
```

- [ ] **Step 5: Modify _dispatch_sync to use interface**

Replace `hub/dispatcher.py:388-412`:

```python
    async def _dispatch_sync(
        self, agent: LocalAgent, message_dict: dict,
        interface: ResolvedInterface | None = None,
    ) -> DispatchResult:
        """Send a synchronous A2A send request with blocking=True."""
        iface = interface or agent.interface
        version = iface.protocol_version
        configuration: dict[str, Any] = {"blocking": True}
        request_body = self._build_jsonrpc(
            message_dict, base_method="send", version=version, configuration=configuration,
        )
        client = await self._get_client()
        headers = {"Content-Type": "application/json", **a2a_compat.get_headers(version)}

        resp = await client.post(iface.url, json=request_body, headers=headers)
        raw = self._check_response(resp)
        normalized = a2a_compat.extract_response(raw, version)
        return self._extract_response_content(normalized)
```

- [ ] **Step 6: Modify _dispatch_streaming to use interface**

Replace `hub/dispatcher.py:416-506`:

```python
    async def _dispatch_streaming(
        self, agent: LocalAgent, message_dict: dict, agent_message_id: str,
        interface: ResolvedInterface | None = None,
    ) -> AsyncIterator[DispatchEvent]:
        """Send a streaming A2A stream request, yield classified events."""
        iface = interface or agent.interface
        version = iface.protocol_version
        request_body = self._build_jsonrpc(
            message_dict, base_method="stream", version=version,
        )
        client = await self._get_client()
        headers = {"Content-Type": "application/json", **a2a_compat.get_headers(version)}

        async with aconnect_sse(
            client, "POST", iface.url,
            json=request_body,
            headers=headers,
        ) as event_source:
            first_event = True
            async for sse in event_source.aiter_sse():
                try:
                    data = json.loads(sse.data)
                except (json.JSONDecodeError, TypeError):
                    continue

                if first_event:
                    first_event = False
                    err = a2a_compat.extract_jsonrpc_error(data)
                    if err and err.code in a2a_compat.FALLBACK_ELIGIBLE_CODES:
                        raise A2AVersionFallbackError(
                            f"JSON-RPC error {err.code}: {err.message}"
                        )

                inner = data.get("result", data)
                classified = a2a_compat.classify_stream_event(inner, version)

                if classified is None:
                    if inner.get("kind", ""):
                        logger.warning("Unknown streaming event kind: %s", inner.get("kind"))
                    continue

                event_type, payload = classified

                if event_type == "artifact-update":
                    text = self._extract_artifact_text(payload)
                    raw_parts = self._collect_non_text_parts_from_artifact(payload)
                    yield DispatchEvent(
                        type="artifact_update",
                        agent_message_id=agent_message_id,
                        data={
                            "raw": payload,
                            "text": text,
                            "parts": raw_parts,
                            "append": payload.get("append", False),
                            "last_chunk": payload.get("lastChunk", payload.get("last_chunk", False)),
                        },
                    )
                elif event_type == "status-update":
                    state = payload.get("status", {}).get("state")
                    text = self._extract_status_text(payload)
                    final = payload.get("final", False)
                    yield DispatchEvent(
                        type="task_status",
                        agent_message_id=agent_message_id,
                        data={
                            "state": state,
                            "status_text": text,
                            "final": final,
                            "task_id": payload.get("taskId", payload.get("task_id")),
                            "context_id": payload.get("contextId", payload.get("context_id")),
                            "raw": payload,
                        },
                    )
                elif event_type == "task":
                    yield DispatchEvent(
                        type="task_submitted",
                        agent_message_id=agent_message_id,
                        data={
                            "task_id": payload.get("id"),
                            "context_id": payload.get("contextId", payload.get("context_id")),
                        },
                    )
                elif event_type == "message":
                    text = self._extract_message_text(payload)
                    raw_parts = self._collect_non_text_parts_from_message(payload)
                    if text or raw_parts:
                        artifact_parts = []
                        if text:
                            artifact_parts.append({"kind": "text", "text": text})
                        artifact_parts.extend(raw_parts)
                        yield DispatchEvent(
                            type="artifact_update",
                            agent_message_id=agent_message_id,
                            data={
                                "raw": payload,
                                "text": text,
                                "parts": raw_parts,
                                "append": True,
                                "last_chunk": False,
                                "artifact": {
                                    "artifactId": f"{agent_message_id}-stream",
                                    "parts": artifact_parts,
                                },
                            },
                        )
```

- [ ] **Step 7: Modify cancel_task to use interface**

Replace `hub/dispatcher.py:237-254`:

```python
    async def cancel_task(self, agent: LocalAgent, task_id: str) -> None:
        """Best-effort cancellation of an in-flight task on a local agent."""
        iface = agent.interface
        version = iface.protocol_version
        method = a2a_compat.get_method_name("cancel_task", version)
        headers = {"Content-Type": "application/json", **a2a_compat.get_headers(version)}
        client = await self._get_client()
        body = {
            "jsonrpc": "2.0",
            "id": uuid4().hex,
            "method": method,
            "params": {"id": task_id},
        }
        resp = await client.post(iface.url, json=body, headers=headers)
        logger.info("Cancel response from %s: %d", agent.name, resp.status_code)
```

- [ ] **Step 8: Modify _fetch_task to use interface and normalize**

Replace `hub/dispatcher.py:258-289`:

```python
    async def _fetch_task(
        self, agent: LocalAgent, task_id: str,
        timeout: float | None = None,
        interface: ResolvedInterface | None = None,
    ) -> dict:
        """Fetch a task by ID via tasks/get JSON-RPC call."""
        iface = interface or agent.interface
        version = iface.protocol_version
        method = a2a_compat.get_method_name("get_task", version)
        headers = {"Content-Type": "application/json", **a2a_compat.get_headers(version)}
        client = await self._get_client()
        body = {
            "jsonrpc": "2.0",
            "id": uuid4().hex,
            "method": method,
            "params": {"id": task_id},
        }
        kwargs: dict[str, Any] = {"json": body, "headers": headers}
        if timeout is not None:
            kwargs["timeout"] = httpx.Timeout(
                connect=10.0, read=timeout, write=30.0, pool=5.0,
            )
        resp = await client.post(iface.url, **kwargs)
        raw = self._check_response(resp)
        normalized = a2a_compat.extract_response(raw, version)
        return normalized.get("result", normalized)
```

- [ ] **Step 9: Add _check_response helper**

Add to `hub/dispatcher.py` (after `_build_jsonrpc`):

```python
    @staticmethod
    def _check_response(resp: httpx.Response) -> dict:
        """Parse response JSON and check for JSON-RPC errors.

        Raises A2AVersionFallbackError for fallback-eligible error codes.
        Raises RuntimeError for other JSON-RPC errors or unparseable 2xx bodies.
        Falls through to raise_for_status() for non-JSON non-2xx.
        """
        try:
            raw = resp.json()
        except (json.JSONDecodeError, ValueError):
            if resp.is_success:
                raise RuntimeError(
                    f"Agent returned HTTP {resp.status_code} with unparseable body"
                )
            resp.raise_for_status()
            return {}  # unreachable, raise_for_status always throws for non-2xx

        err = a2a_compat.extract_jsonrpc_error(raw)
        if err:
            if err.code in a2a_compat.FALLBACK_ELIGIBLE_CODES:
                raise A2AVersionFallbackError(
                    f"JSON-RPC error {err.code}: {err.message}"
                )
            raise RuntimeError(f"JSON-RPC error {err.code}: {err.message}")

        if not resp.is_success:
            resp.raise_for_status()

        return raw
```

- [ ] **Step 10: Update _poll_until_terminal to pass interface**

Modify `hub/dispatcher.py:323-386` — add `interface` param:

```python
    async def _poll_until_terminal(
        self,
        agent: LocalAgent,
        result: DispatchResult,
        poll_interval: float = 2.0,
        max_attempts: int = 30,
        interface: ResolvedInterface | None = None,
    ) -> DispatchResult:
        """Poll tasks/get until the task reaches a terminal or interactive state."""
        logger.info(
            "Sync dispatch returned non-terminal state '%s' — polling task %s on %s",
            result.task_state, result.task_id, agent.name,
        )

        for attempt in range(max_attempts):
            await asyncio.sleep(poll_interval)

            task_data = await self._fetch_task(
                agent, result.task_id, timeout=POLL_REQUEST_TIMEOUT,
                interface=interface,
            )

            state = task_data.get("status", {}).get("state")
            if state:
                result.task_state = state

            text, non_text = self._collect_parts_from_task(task_data)
            if text:
                result.artifact_text = text
                result.text = text
            if non_text:
                result.raw_parts = non_text

            result.context_id = (
                task_data.get("contextId", task_data.get("context_id"))
                or result.context_id
            )

            if result.task_state in TERMINAL_STATES | INTERACTIVE_STATES:
                logger.info(
                    "Polling task %s reached state '%s' after %d attempt(s)",
                    result.task_id, result.task_state, attempt + 1,
                )
                break
        else:
            logger.error(
                "Polling task %s on %s exhausted %d attempts (still '%s')",
                result.task_id, agent.name, max_attempts, result.task_state,
            )
            result.error = (
                f"Agent task {result.task_id} did not reach a terminal state "
                f"within {max_attempts} polling attempts"
            )
            result.error_type = "PollingTimeout"

        return result
```

- [ ] **Step 10b: Update _refetch_final_task to pass interface**

Modify `hub/dispatcher.py:291-321` — add `interface` param so fallback streaming doesn't re-fetch against the wrong endpoint:

```python
    async def _refetch_final_task(
        self, agent: LocalAgent, result: DispatchResult,
        interface: ResolvedInterface | None = None,
    ) -> DispatchResult:
        """Re-fetch the completed task from the agent to get definitive response text."""
        logger.info(
            "Streaming produced no text — re-fetching task %s from %s",
            result.task_id, agent.name,
        )
        try:
            task_data = await self._fetch_task(agent, result.task_id, interface=interface)

            text, non_text = self._collect_parts_from_task(task_data)
            if text:
                logger.info("Re-fetch recovered %d chars from task %s", len(text), result.task_id)
                result.artifact_text = text
            if non_text:
                result.raw_parts = non_text

            state = task_data.get("status", {}).get("state")
            if state:
                result.task_state = state
        except Exception as exc:
            logger.warning(
                "Failed to re-fetch task %s from %s: %s (best-effort)",
                result.task_id, agent.name, exc,
            )
        return result
```

- [ ] **Step 11: Update existing _build_jsonrpc tests**

Modify `tests/test_dispatcher.py` `TestJsonRpcBuild` and `TestBuildJsonRpc`:

```python
class TestJsonRpcBuild:
    def test_build_jsonrpc(self):
        body = Dispatcher._build_jsonrpc(SAMPLE_MESSAGE, "send", "0.3")
        assert body["jsonrpc"] == "2.0"
        assert body["method"] == "message/send"
        assert body["params"]["message"]["parts"] == [{"kind": "text", "text": "Hello agent"}]
        assert "id" in body

    def test_build_jsonrpc_v10(self):
        body = Dispatcher._build_jsonrpc(SAMPLE_MESSAGE, "send", "1.0")
        assert body["method"] == "SendMessage"
        assert body["params"]["message"]["parts"] == [{"text": "Hello agent"}]


class TestBuildJsonRpc:
    def test_no_configuration_omits_params_key(self):
        result = Dispatcher._build_jsonrpc(SAMPLE_MESSAGE, "send", "0.3")
        assert "configuration" not in result["params"]
        assert result["method"] == "message/send"

    def test_with_configuration_included_in_params(self):
        cfg = {"blocking": True}
        result = Dispatcher._build_jsonrpc(SAMPLE_MESSAGE, "send", "0.3", configuration=cfg)
        assert result["params"]["configuration"] == {"blocking": True}

    def test_streaming_call_omits_configuration(self):
        result = Dispatcher._build_jsonrpc(SAMPLE_MESSAGE, "stream", "0.3")
        assert "configuration" not in result["params"]
        assert result["method"] == "message/stream"

    @pytest.mark.asyncio
    async def test_dispatch_sync_sends_blocking_true(self, agent):
        sent_body = {}

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.is_success = True
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "jsonrpc": "2.0",
            "id": "1",
            "result": {
                "kind": "message",
                "parts": [{"text": "blocking response"}],
            },
        }

        async def fake_post(url, *, json, headers, **kwargs):
            sent_body.update(json)
            return mock_resp

        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post = fake_post
        dispatcher = Dispatcher()
        dispatcher._client = mock_client

        events = []
        async for batch in dispatcher.dispatch(
            agent=agent,
            message_dict=SAMPLE_MESSAGE,
            agent_message_id="am-block-001",
            user_message_id="um-block-001",
        ):
            events.extend(batch)

        assert sent_body["params"]["configuration"]["blocking"] is True
        assert events[0]["type"] == "agent_response"

    @pytest.mark.asyncio
    async def test_dispatch_streaming_does_not_send_blocking(self, streaming_agent):
        import hub.dispatcher as dispatcher_mod

        sent_body = {}

        @asynccontextmanager
        async def fake_sse(client, method, url, *, json, headers):
            sent_body.update(json)

            class _FakeEventSource:
                async def aiter_sse(self):
                    sse = MagicMock()
                    sse.data = '{"result": {"kind": "status-update", "status": {"state": "completed"}, "final": true}}'
                    yield sse

            yield _FakeEventSource()

        original = dispatcher_mod.aconnect_sse
        dispatcher_mod.aconnect_sse = fake_sse
        try:
            dispatcher = Dispatcher()
            mock_client = AsyncMock()
            mock_client.is_closed = False
            dispatcher._client = mock_client

            batches = []
            async for batch in dispatcher.dispatch(
                agent=streaming_agent,
                message_dict=SAMPLE_MESSAGE,
                agent_message_id="am-stream-block",
                user_message_id="um-stream-block",
            ):
                batches.append(batch)
        finally:
            dispatcher_mod.aconnect_sse = original

        assert "configuration" not in sent_body.get("params", {})
```

- [ ] **Step 12: Run tests to verify they pass**

Run: `uv run pytest tests/test_dispatcher.py -v`
Expected: PASS (all existing tests + 2 new v1.0 tests)

- [ ] **Step 13: Commit**

```bash
git add hub/dispatcher.py tests/test_dispatcher.py
git commit -m "feat(dispatcher): route methods/headers/urls through a2a_compat"
```

---

### Task 12: Dispatcher Fallback Retry

**Files:**
- Modify: `hub/dispatcher.py:101-154` (`dispatch` method)
- Modify: `tests/test_dispatcher.py`

- [ ] **Step 1: Write failing tests for fallback retry**

Add to `tests/test_dispatcher.py`:

```python
from hub.a2a_compat import A2AVersionFallbackError


@pytest.fixture
def v10_agent_with_fallback():
    return LocalAgent(
        local_agent_id="test_v10_fb",
        name="V1.0 Agent (fallback)",
        url="http://localhost:9001",
        agent_card={"capabilities": {"streaming": False}},
        interface=ResolvedInterface(binding="JSONRPC", protocol_version="1.0", url="http://localhost:9001/v1"),
        fallback_interface=ResolvedInterface(binding="JSONRPC", protocol_version="0.3", url="http://localhost:9001/v03"),
    )


class TestFallbackRetry:
    @pytest.mark.asyncio
    async def test_sync_fallback_on_method_not_found(self, v10_agent_with_fallback):
        """v1.0 dispatch gets -32601, retry with v0.3 succeeds."""
        agent = v10_agent_with_fallback
        call_count = 0

        v10_error_resp = MagicMock()
        v10_error_resp.status_code = 200
        v10_error_resp.is_success = True
        v10_error_resp.json.return_value = {
            "jsonrpc": "2.0",
            "id": "1",
            "error": {"code": -32601, "message": "Method not found"},
        }

        v03_success_resp = MagicMock()
        v03_success_resp.status_code = 200
        v03_success_resp.is_success = True
        v03_success_resp.raise_for_status = MagicMock()
        v03_success_resp.json.return_value = {
            "jsonrpc": "2.0",
            "id": "2",
            "result": {
                "kind": "task",
                "id": "t-1",
                "status": {
                    "state": "completed",
                    "message": {"role": "agent", "parts": [{"text": "fallback ok"}]},
                },
            },
        }

        urls_called = []

        async def fake_post(url, *, json, headers, **kwargs):
            urls_called.append(url)
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return v10_error_resp
            return v03_success_resp

        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post = fake_post
        dispatcher = Dispatcher()
        dispatcher._client = mock_client

        events = []
        async for batch in dispatcher.dispatch(
            agent=agent,
            message_dict=SAMPLE_MESSAGE,
            agent_message_id="am-fb-001",
            user_message_id="um-001",
        ):
            events.extend(batch)

        assert urls_called[0] == "http://localhost:9001/v1"
        assert urls_called[1] == "http://localhost:9001/v03"
        assert events[0]["type"] == "agent_response"
        assert events[0]["data"]["content"] == "fallback ok"

    @pytest.mark.asyncio
    async def test_no_fallback_without_fallback_interface(self):
        """Agent with no fallback_interface: error propagates."""
        agent = LocalAgent(
            local_agent_id="test_nofb",
            name="No Fallback Agent",
            url="http://localhost:9001",
            agent_card={"capabilities": {"streaming": False}},
            interface=ResolvedInterface(binding="JSONRPC", protocol_version="1.0", url="http://localhost:9001/v1"),
            fallback_interface=None,
        )

        error_resp = MagicMock()
        error_resp.status_code = 200
        error_resp.is_success = True
        error_resp.json.return_value = {
            "jsonrpc": "2.0",
            "id": "1",
            "error": {"code": -32601, "message": "Method not found"},
        }

        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post = AsyncMock(return_value=error_resp)
        dispatcher = Dispatcher()
        dispatcher._client = mock_client

        events = []
        async for batch in dispatcher.dispatch(
            agent=agent,
            message_dict=SAMPLE_MESSAGE,
            agent_message_id="am-nofb",
            user_message_id="um-nofb",
        ):
            events.extend(batch)

        assert events[0]["type"] == "agent_error"
        assert events[1]["data"]["status"] == "failed"

    @pytest.mark.asyncio
    async def test_non_fallback_jsonrpc_error_propagates(self):
        """Non-fallback-eligible JSON-RPC errors are not retried."""
        agent = LocalAgent(
            local_agent_id="test_other_err",
            name="Other Error Agent",
            url="http://localhost:9001",
            agent_card={"capabilities": {"streaming": False}},
            interface=V10_INTERFACE,
            fallback_interface=V03_INTERFACE,
        )

        error_resp = MagicMock()
        error_resp.status_code = 200
        error_resp.is_success = True
        error_resp.json.return_value = {
            "jsonrpc": "2.0",
            "id": "1",
            "error": {"code": -32600, "message": "Invalid Request"},
        }

        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post = AsyncMock(return_value=error_resp)
        dispatcher = Dispatcher()
        dispatcher._client = mock_client

        events = []
        async for batch in dispatcher.dispatch(
            agent=agent,
            message_dict=SAMPLE_MESSAGE,
            agent_message_id="am-other",
            user_message_id="um-other",
        ):
            events.extend(batch)

        assert mock_client.post.call_count == 1
        assert events[0]["type"] == "agent_error"


class TestCheckResponse:
    def test_unparseable_2xx_raises(self):
        """HTTP 200 with non-JSON body must not silently succeed."""
        resp = MagicMock()
        resp.status_code = 200
        resp.is_success = True
        resp.json.side_effect = ValueError("No JSON")
        with pytest.raises(RuntimeError, match="unparseable body"):
            Dispatcher._check_response(resp)

    def test_unparseable_4xx_raises_for_status(self):
        """HTTP 4xx/5xx with non-JSON body delegates to raise_for_status."""
        resp = MagicMock()
        resp.status_code = 502
        resp.is_success = False
        resp.json.side_effect = ValueError("No JSON")
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Bad Gateway", request=MagicMock(), response=resp,
        )
        with pytest.raises(httpx.HTTPStatusError):
            Dispatcher._check_response(resp)

    def test_valid_json_rpc_success(self):
        """Normal success path returns parsed dict."""
        resp = MagicMock()
        resp.status_code = 200
        resp.is_success = True
        resp.json.return_value = {"jsonrpc": "2.0", "id": "1", "result": {"id": "t1"}}
        raw = Dispatcher._check_response(resp)
        assert raw["result"]["id"] == "t1"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_dispatcher.py::TestFallbackRetry tests/test_dispatcher.py::TestCheckResponse -v`
Expected: FAIL (dispatch doesn't handle A2AVersionFallbackError, _check_response doesn't exist)

- [ ] **Step 3: Modify dispatch() to add fallback retry**

Replace `hub/dispatcher.py:101-154` (the `dispatch` method):

```python
    async def dispatch(
        self,
        agent: LocalAgent,
        message_dict: dict,
        agent_message_id: str,
        user_message_id: str | None = None,
    ) -> AsyncIterator[list[dict]]:
        """Dispatch an A2A message to a local agent, yielding event batches.

        If v1.0 dispatch fails with a fallback-eligible JSON-RPC error,
        retries once with v0.3 wire format using agent.fallback_interface.
        """
        result = DispatchResult()
        interface = agent.interface

        try:
            result = await self._do_dispatch(
                agent, message_dict, agent_message_id, interface, result,
            )
            async for batch in result._pending_batches:
                yield batch
        except A2AVersionFallbackError as fallback_exc:
            if agent.fallback_interface:
                logger.warning(
                    "v1.0 dispatch to %s failed (%s) — retrying with v0.3",
                    agent.name, fallback_exc,
                )
                interface = agent.fallback_interface
                try:
                    result = await self._do_dispatch(
                        agent, message_dict, agent_message_id, interface, DispatchResult(),
                    )
                    async for batch in result._pending_batches:
                        yield batch
                except Exception as exc:
                    logger.error(
                        "Fallback dispatch to %s also failed: %s",
                        agent.name, exc, exc_info=True,
                    )
                    result = DispatchResult()
                    result.error = str(fallback_exc) or repr(fallback_exc)
                    result.error_type = "A2AVersionFallback"
            else:
                result.error = str(fallback_exc) or repr(fallback_exc)
                result.error_type = "A2AVersionFallback"
        except Exception as exc:
            logger.error("Dispatch to %s failed: %s", agent.name, exc, exc_info=True)
            result.error = str(exc) or repr(exc) or "Unknown dispatch error"
            result.error_type = type(exc).__name__

        terminal: list[dict] = []
        self._emit_terminal_events(terminal, result, agent_message_id, user_message_id, agent.local_agent_id)
        yield terminal
```

Wait — using `result._pending_batches` introduces a new pattern. Let me use a simpler approach that avoids the need for pending batches. The issue is that streaming dispatch yields inline but we need to catch errors before yielding. Let me restructure.

Actually, the simplest correct approach: keep the inline yield pattern but handle fallback at the top level. Since `A2AVersionFallbackError` is raised BEFORE any events are yielded (for streaming, from the first SSE event; for sync, from `_check_response`), we can catch it and retry cleanly.

Replace the dispatch method with:

```python
    async def dispatch(
        self,
        agent: LocalAgent,
        message_dict: dict,
        agent_message_id: str,
        user_message_id: str | None = None,
    ) -> AsyncIterator[list[dict]]:
        """Dispatch an A2A message to a local agent, yielding event batches.

        If v1.0 dispatch fails with a fallback-eligible JSON-RPC error,
        retries once with v0.3 wire format using agent.fallback_interface.
        """
        result = DispatchResult()
        interface = agent.interface

        try:
            async for batch in self._dispatch_with_interface(
                agent, message_dict, agent_message_id, interface, result,
            ):
                yield batch
        except A2AVersionFallbackError as fallback_exc:
            if agent.fallback_interface:
                logger.warning(
                    "v1.0 dispatch to %s failed (%s) — retrying with v0.3",
                    agent.name, fallback_exc,
                )
                result = DispatchResult()
                try:
                    async for batch in self._dispatch_with_interface(
                        agent, message_dict, agent_message_id,
                        agent.fallback_interface, result,
                    ):
                        yield batch
                except Exception as exc:
                    logger.error(
                        "Fallback dispatch to %s also failed: %s",
                        agent.name, exc, exc_info=True,
                    )
                    result = DispatchResult()
                    result.error = str(fallback_exc) or repr(fallback_exc)
                    result.error_type = "A2AVersionFallback"
            else:
                result.error = str(fallback_exc) or repr(fallback_exc)
                result.error_type = "A2AVersionFallback"
        except Exception as exc:
            logger.error("Dispatch to %s failed: %s", agent.name, exc, exc_info=True)
            result.error = str(exc) or repr(exc) or "Unknown dispatch error"
            result.error_type = type(exc).__name__

        terminal: list[dict] = []
        self._emit_terminal_events(terminal, result, agent_message_id, user_message_id, agent.local_agent_id)
        yield terminal

    async def _dispatch_with_interface(
        self,
        agent: LocalAgent,
        message_dict: dict,
        agent_message_id: str,
        interface: ResolvedInterface,
        result: DispatchResult,
    ) -> AsyncIterator[list[dict]]:
        """Core dispatch logic using a specific interface. Populates result in-place."""
        if agent.agent_card.get("capabilities", {}).get("streaming"):
            async for event in self._dispatch_streaming(agent, message_dict, agent_message_id, interface):
                ev_dict = event.to_publish_dict()
                if event.type in ("artifact_update", "task_status"):
                    yield [ev_dict]
                if event.type == "artifact_update":
                    result.artifact_text += event.data.get("text", "")
                elif event.type == "task_status":
                    result.task_state = event.data.get("state")
                    result.task_id = event.data.get("task_id") or result.task_id
                    result.context_id = event.data.get("context_id") or result.context_id
                elif event.type == "task_submitted":
                    result.task_id = event.data.get("task_id") or result.task_id
                    result.context_id = event.data.get("context_id") or result.context_id

            if not result.artifact_text and result.task_id:
                result_copy = await self._refetch_final_task(agent, result, interface=interface)
                result.artifact_text = result_copy.artifact_text
                result.raw_parts = result_copy.raw_parts
                result.task_state = result_copy.task_state or result.task_state
        else:
            sync_result = await self._dispatch_sync(agent, message_dict, interface)
            result.text = sync_result.text
            result.artifact_text = sync_result.artifact_text
            result.raw_parts = sync_result.raw_parts
            result.task_state = sync_result.task_state
            result.task_id = sync_result.task_id
            result.context_id = sync_result.context_id
            if result.task_state in NON_TERMINAL_STATES and result.task_id:
                polled = await self._poll_until_terminal(agent, result, interface=interface)
                result.text = polled.text
                result.artifact_text = polled.artifact_text
                result.raw_parts = polled.raw_parts
                result.task_state = polled.task_state
                result.error = polled.error
                result.error_type = polled.error_type
```

- [ ] **Step 4: Run all dispatcher tests**

Run: `uv run pytest tests/test_dispatcher.py -v`
Expected: PASS (all existing + fallback tests)

- [ ] **Step 5: Commit**

```bash
git add hub/dispatcher.py tests/test_dispatcher.py
git commit -m "feat(dispatcher): add v1.0->v0.3 fallback retry on MethodNotFound/VersionNotSupported"
```

---

### Task 13: V1.0 Streaming Dispatch Tests

**Files:**
- Modify: `tests/test_dispatcher.py`

- [ ] **Step 1: Write v1.0 streaming tests**

Add to `tests/test_dispatcher.py`:

```python
@pytest.fixture
def v10_streaming_agent():
    return LocalAgent(
        local_agent_id="test_v10_stream",
        name="V1.0 Streaming Agent",
        url="http://localhost:9002",
        agent_card={"capabilities": {"streaming": True}},
        interface=ResolvedInterface(binding="JSONRPC", protocol_version="1.0", url="http://localhost:9002/a2a"),
    )


class TestV10Streaming:
    @pytest.mark.asyncio
    async def test_v10_stream_status_and_artifact(self, v10_streaming_agent):
        """v1.0 streaming with statusUpdate and artifactUpdate ProtoJSON keys."""
        canned = [
            {"result": {"task": {"id": "t-1", "status": {"state": "TASK_STATE_SUBMITTED"}}}},
            {"result": {"artifactUpdate": {
                "artifact": {"artifactId": "art-1", "parts": [{"text": "v1 chunk"}]},
                "append": True,
                "lastChunk": False,
            }}},
            {"result": {"statusUpdate": {
                "status": {"state": "TASK_STATE_COMPLETED"},
            }}},
        ]

        dispatcher = Dispatcher()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client._canned_events = canned
        dispatcher._client = mock_client

        import hub.dispatcher as dispatcher_mod
        original = dispatcher_mod.aconnect_sse
        dispatcher_mod.aconnect_sse = _fake_aconnect_sse
        try:
            batches = []
            async for batch in dispatcher.dispatch(
                agent=v10_streaming_agent,
                message_dict=SAMPLE_MESSAGE,
                agent_message_id="am-v10-stream",
                user_message_id="um-v10-stream",
            ):
                batches.append(batch)
        finally:
            dispatcher_mod.aconnect_sse = original

        streaming_events = [ev for b in batches[:-1] for ev in b]
        terminal = batches[-1]

        artifact_updates = [e for e in streaming_events if e["type"] == "artifact_update"]
        assert len(artifact_updates) == 1
        assert artifact_updates[0]["data"]["text"] == "v1 chunk"

        status_events = [e for e in streaming_events if e["type"] == "task_status"]
        assert len(status_events) == 1
        assert status_events[0]["data"]["state"] == "completed"
        assert status_events[0]["data"]["final"] is True

        response = next(e for e in terminal if e["type"] == "agent_response")
        assert response["data"]["content"] == "v1 chunk"

    @pytest.mark.asyncio
    async def test_v10_stream_message_kind(self, v10_streaming_agent):
        """v1.0 'message' stream events produce artifact_update."""
        canned = [
            {"result": {"task": {"id": "t-2", "status": {"state": "TASK_STATE_SUBMITTED"}}}},
            {"result": {"message": {"role": "ROLE_AGENT", "parts": [{"text": "hello from v1"}]}}},
            {"result": {"statusUpdate": {"status": {"state": "TASK_STATE_COMPLETED"}}}},
        ]

        dispatcher = Dispatcher()
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client._canned_events = canned
        dispatcher._client = mock_client

        import hub.dispatcher as dispatcher_mod
        original = dispatcher_mod.aconnect_sse
        dispatcher_mod.aconnect_sse = _fake_aconnect_sse
        try:
            batches = []
            async for batch in dispatcher.dispatch(
                agent=v10_streaming_agent,
                message_dict=SAMPLE_MESSAGE,
                agent_message_id="am-v10-msg",
                user_message_id="um-v10-msg",
            ):
                batches.append(batch)
        finally:
            dispatcher_mod.aconnect_sse = original

        streaming_events = [ev for b in batches[:-1] for ev in b]
        artifact_updates = [e for e in streaming_events if e["type"] == "artifact_update"]
        assert len(artifact_updates) == 1
        assert artifact_updates[0]["data"]["text"] == "hello from v1"

    @pytest.mark.asyncio
    async def test_v10_stream_fallback_on_first_event_error(self):
        """First SSE event is a JSON-RPC error → triggers fallback."""
        agent = LocalAgent(
            local_agent_id="test_stream_fb",
            name="Stream Fallback Agent",
            url="http://localhost:9002",
            agent_card={"capabilities": {"streaming": True}},
            interface=ResolvedInterface(binding="JSONRPC", protocol_version="1.0", url="http://localhost:9002/v1"),
            fallback_interface=ResolvedInterface(binding="JSONRPC", protocol_version="0.3", url="http://localhost:9002/v03"),
        )

        v10_error_events = [
            {"jsonrpc": "2.0", "id": "1", "error": {"code": -32601, "message": "Method not found"}},
        ]
        v03_success_events = [
            {"result": {"kind": "task", "id": "t-fb", "contextId": "ctx-fb"}},
            {"result": {"kind": "status-update", "status": {"state": "completed"}, "final": True}},
        ]

        import hub.dispatcher as dispatcher_mod

        call_count = 0

        @asynccontextmanager
        async def switching_sse(client, method, url, *, json, headers):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield FakeEventSource(v10_error_events)
            else:
                yield FakeEventSource(v03_success_events)

        original = dispatcher_mod.aconnect_sse
        dispatcher_mod.aconnect_sse = switching_sse
        try:
            dispatcher = Dispatcher()
            mock_client = AsyncMock()
            mock_client.is_closed = False
            dispatcher._client = mock_client

            batches = []
            async for batch in dispatcher.dispatch(
                agent=agent,
                message_dict=SAMPLE_MESSAGE,
                agent_message_id="am-stream-fb",
                user_message_id="um-stream-fb",
            ):
                batches.append(batch)
        finally:
            dispatcher_mod.aconnect_sse = original

        assert call_count == 2
        terminal = batches[-1]
        status = next(e for e in terminal if e["type"] == "processing_status")
        assert status["data"]["status"] != "failed"
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `uv run pytest tests/test_dispatcher.py::TestV10Streaming -v`
Expected: PASS (all 3 tests)

- [ ] **Step 3: Commit**

```bash
git add tests/test_dispatcher.py
git commit -m "test(dispatcher): add v1.0 streaming and fallback retry tests"
```

---

## Phase 4: Main Loop + Canonical Parts

### Task 14: Fix HITL Reply Parts and Update Tests

**Files:**
- Modify: `hub/main.py:319-323`
- Modify: `tests/test_main.py:27-32`

- [ ] **Step 1: Write failing test for canonical HITL parts**

Add to `tests/test_main.py`:

```python
class TestHandleUserReplyCanonicalParts:
    async def test_hitl_reply_uses_flattened_parts(self):
        """HITL reply must use canonical flattened parts (no 'kind')."""
        daemon = _make_daemon()
        daemon.registry.get_agent.return_value = AGENT

        dispatched_message = {}

        async def _capture_dispatch(**kwargs):
            dispatched_message.update(kwargs.get("message_dict", {}))
            yield [{"type": "agent_response", "agent_message_id": "amsg-12345678", "data": {"content": "ok"}}]

        daemon.dispatcher.dispatch = _capture_dispatch

        await daemon._handle_user_reply(_full_user_reply_event())

        parts = dispatched_message.get("parts", [])
        assert len(parts) == 1
        assert "kind" not in parts[0]
        assert parts[0]["text"] == "yes"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_main.py::TestHandleUserReplyCanonicalParts -v`
Expected: FAIL (parts still have `kind` field)

- [ ] **Step 3: Fix HITL reply construction in main.py**

Modify `hub/main.py:319-323`:

```python
# OLD:
        reply_message: dict[str, Any] = {
            "role": "user",
            "parts": [{"kind": "text", "text": reply_text}],
            "messageId": uuid4().hex,
        }

# NEW:
        reply_message: dict[str, Any] = {
            "role": "user",
            "parts": [{"text": reply_text}],
            "messageId": uuid4().hex,
        }
```

- [ ] **Step 4: Update AGENT fixture in test_main.py**

The `AGENT` fixture needs `interface` field for forward compatibility. With `__post_init__`, it already gets a default v0.3 interface from `url`. Verify by adding an assertion:

Add to an existing test or create:

```python
class TestLocalAgentDefaultInterface:
    def test_agent_has_default_interface(self):
        assert AGENT.interface is not None
        assert AGENT.interface.protocol_version == "0.3"
        assert AGENT.interface.url == "http://localhost:9001"
```

- [ ] **Step 5: Run all main tests**

Run: `uv run pytest tests/test_main.py -v`
Expected: PASS (all tests)

- [ ] **Step 6: Commit**

```bash
git add hub/main.py tests/test_main.py
git commit -m "fix(main): use canonical flattened parts in HITL reply (remove kind)"
```

---

## Phase 5: Lock File and Integration

### Task 15: Regenerate Lock File and Run Full Suite

**Files:**
- Regenerate: `uv.lock`
- Modify: `tests/e2e_live_test.py`

- [ ] **Step 1: Regenerate uv.lock**

Run: `uv lock`
Expected: Lock file updated, `a2a-sdk` resolves to `>=1.0.1`

- [ ] **Step 2: Install updated dependencies**

Run: `uv sync`
Expected: `a2a-sdk` v1.0.x installed

- [ ] **Step 3: Run full unit test suite**

Run: `uv run pytest tests/ -k 'not e2e' -v`
Expected: ALL PASS

- [ ] **Step 4: Verify a2a-sdk version in lock file**

Run: `grep 'a2a-sdk' uv.lock | head -5`
Expected: Shows version `>=1.0.1` (not `0.3.25`)

- [ ] **Step 5: Read e2e_live_test.py and add v1.0 path marker**

Read `tests/e2e_live_test.py` and add a TODO comment marking where a v1.0 agent test path should be added when a v1.0 agent is available for testing:

```python
# TODO(a2a-v1.0): Add a parallel test path that targets a v1.0 local agent.
# The test should verify:
#   1. Agent card fetched with supportedInterfaces
#   2. SendMessage method used (not message/send)
#   3. A2A-Version: 1.0 header sent
#   4. Response states normalized to canonical lowercase
# Mark with @pytest.mark.manual until a v1.0 agent is available.
```

- [ ] **Step 6: Commit**

```bash
git add uv.lock tests/e2e_live_test.py
git commit -m "chore: regenerate lock file for a2a-sdk v1.0, add v1.0 e2e TODO"
```

---

### Task 16: Final Verification

- [ ] **Step 1: Run full test suite one more time**

Run: `uv run pytest tests/ -k 'not e2e' -v --tb=short`
Expected: ALL PASS

- [ ] **Step 2: Verify no top-level AgentCard import remains in agent_registry**

Run: `grep -n "from a2a.types import AgentCard" hub/agent_registry.py`
Expected: No matches (replaced with compat layer). Note: `hub/a2a_compat.py` has a local import inside `validate_agent_card()` — that is intentional and correct.

- [ ] **Step 3: Verify all compat functions are tested**

Run: `uv run pytest tests/test_a2a_compat.py -v --tb=short`
Expected: ALL PASS with tests covering: validate_agent_card, select_interface, select_fallback_interface, get_method_name, get_headers, normalize_task_state, normalize_role, build_message_parts, normalize_inbound_parts, build_request_params, extract_response, classify_stream_event, extract_jsonrpc_error

- [ ] **Step 4: Commit (if any fixes needed)**

```bash
git add -A
git commit -m "fix: address any issues found in final verification"
```
