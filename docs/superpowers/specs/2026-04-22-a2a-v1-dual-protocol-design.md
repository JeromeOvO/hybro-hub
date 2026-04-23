# hybro-hub A2A v1.0 Dual-Protocol Client Upgrade

**Date:** 2026-04-22
**Scope:** Client-side dual protocol support (v0.3 + v1.0)
**Out of scope:** Inbound A2A server, external v0.3 client connections

---

## 1. Problem Statement

hybro-hub acts as a background daemon that discovers local A2A agents and dispatches messages to them as a JSON-RPC client. The codebase currently uses `a2a-sdk==0.3.25` conventions (slash-delimited methods, `kind` discriminator, lowercase enum states, Pydantic `AgentCard.model_validate()`).

The `pyproject.toml` already declares `a2a-sdk>=1.0.1,<2`, but the lock file is still pinned to `0.3.25` and all code uses v0.3 wire format. We need to:

1. Upgrade to `a2a-sdk>=1.0.1` (protobuf-based types, no Pydantic `model_validate()`)
2. Continue communicating with v0.3 local agents (legacy agents that won't upgrade immediately)
3. Communicate with v1.0 local agents using the new wire format
4. Keep the relay event format unchanged so the cloud backend needs no simultaneous migration

## 2. Success Criteria

| Criterion | Verification |
|-----------|-------------|
| hybro-hub discovers v0.3 agent cards (with `url` field) | `test_agent_registry.py` with v0.3 card fixture |
| hybro-hub discovers v1.0 agent cards (with `supportedInterfaces`) | `test_agent_registry.py` with v1.0 card fixture |
| v0.3 agents receive old-style JSON-RPC (`message/send`, `kind`-based parts) | `test_dispatcher.py` v0.3 path |
| v1.0 agents receive new-style JSON-RPC (`SendMessage`, `A2A-Version: 1.0`, flattened parts) | `test_dispatcher.py` v1.0 path |
| Relay event format is unchanged (lowercase states, same event types) | `test_main.py` integration checks |
| All unit tests pass after migration | `pytest tests/ -k 'not e2e'` green |
| e2e_live_test.py updated to handle both protocol paths | Manually verified or marked with TODO for v1.0 agent availability |

## 3. Key Discovery: `a2a.compat.v0_3` Built-in Layer

`a2a-sdk` v1.0.1 ships a comprehensive v0.3 compat module at `a2a.compat.v0_3`:

- **`a2a.compat.v0_3.types`**: Full set of v0.3 Pydantic models (100+ classes) with `model_validate()` / `model_dump()` preserved
- **`a2a.compat.v0_3.conversions`**: Bidirectional conversion functions (`to_core_*` / `to_compat_*`) for Part, Message, Task, TaskStatus, Artifact, AgentCard, StreamResponse
- **`a2a.compat.v0_3.versions`**: `is_legacy_version(version_string)` returning `True` for `>=0.3, <1.0`
- **Constants**: `PROTOCOL_VERSION_0_3 = '0.3'`, `PROTOCOL_VERSION_1_0 = '1.0'`, `PROTOCOL_VERSION_CURRENT = '1.0'`, `VERSION_HEADER = 'A2A-Version'`

This means we do NOT need to write conversion logic ourselves. Our compat layer (`hub/a2a_compat.py`) focuses on:
- Version detection from agent cards
- Method name routing
- Header injection
- Dispatching to the right wire format builders using SDK-provided conversions where needed

## 4. Architecture

### 4.1 New Module: `hub/a2a_compat.py`

Central module that encapsulates all v0.3/v1.0 differences. Neither `dispatcher.py` nor `agent_registry.py` should contain protocol-version branching beyond calling into this module.

```
hub/a2a_compat.py

# Data types
@dataclass ResolvedInterface(binding, protocol_version, url)
@dataclass JsonRpcError(code, message, data)
class A2AVersionFallbackError(Exception)    # Raised when v1.0 dispatch gets fallback-eligible error

# Card handling
├── validate_agent_card(card_data: dict) -> dict | None                        # Try v1.0 then v0.3
├── select_interface(card: dict) -> ResolvedInterface                          # Pick best JSONRPC interface
├── select_fallback_interface(card: dict, primary: ResolvedInterface) -> ResolvedInterface | None
│                                                                               # v0.3 alternate for retry

# Wire format translation
├── get_method_name(base_method: str, version: str) -> str                     # "message/send" <-> "SendMessage"
├── get_headers(version: str) -> dict                                          # v1.0: {"A2A-Version": "1.0"}; v0.3: {} (omit — compatibility tradeoff)
├── normalize_task_state(state: str) -> str                                    # TASK_STATE_COMPLETED -> completed
├── normalize_role(role: str) -> str                                           # ROLE_USER -> user
├── build_message_parts(parts: list[dict], version: str) -> list[dict]         # Canonical -> wire parts (text, file, data)
├── build_request_params(message_dict: dict, version: str, ...) -> dict        # Full params builder
├── extract_response(raw: dict, version: str) -> dict                          # Normalize response to canonical
├── classify_stream_event(data: dict, version: str) -> tuple[str, dict]        # Classify SSE events

# Error handling
├── extract_jsonrpc_error(raw: dict) -> JsonRpcError | None                    # Parse JSON-RPC error
└── FALLBACK_ELIGIBLE_CODES: set[int] = {-32601, -32009}                       # MethodNotFound, VersionNotSupportedError
```

### 4.2 Internal Canonical Format

The hub works internally with a lowercase canonical format for states/roles/events. This is the format that `main.py` and the relay layer already expect. The compat layer translates at the boundaries:

- **Outbound** (hub -> agent): canonical -> v0.3 wire OR canonical -> v1.0 wire
- **Inbound** (agent -> hub): v0.3 wire -> canonical OR v1.0 wire -> canonical

States: `submitted`, `working`, `completed`, `failed`, `canceled`, `rejected`, `input-required`, `auth-required`
Roles: `user`, `agent`

**Parts (important nuance):** The current codebase is inconsistent about part shapes:
- Relay inbound messages use **flattened** parts: `{"text": "hello"}` (no `kind` field) — see `tests/test_main.py:42`
- HITL reply construction in `main.py:321` uses **v0.3-style** parts: `{"kind": "text", "text": reply_text}`
- The dispatcher's `_collect_parts()` already handles both: it checks `root.get("text")` which works regardless of whether `kind` is present

**Decision:** The canonical internal format for parts is **flattened** (no `kind`): `{"text": "hello"}`, `{"data": {...}}`. This matches what the relay sends inbound and is forward-compatible with v1.0. The existing HITL reply construction in `main.py:321` that adds `kind` should be updated to use the flattened form. The compat layer's `build_request_params()` must accept this flattened canonical form and:
- For v0.3 agents: add `kind` field back (`{"kind": "text", "text": "hello"}`)
- For v1.0 agents: pass through as-is (already matches v1.0 wire format)

This also means `_collect_parts()` in the dispatcher continues to work unchanged — it already indexes by content field presence, not by `kind`.

#### Non-text Part Mapping (file, data, raw)

The current hub already preserves non-text parts as raw dicts through dispatch and relay publish (see `dispatcher.py:54 raw_parts`, `dispatcher.py:221 response_data["parts"]`, `dispatcher.py:537 _collect_parts`). The compat layer must handle bidirectional conversion for all part types, not just text.

**Canonical (internal) -> v0.3 wire:**

| Canonical part | v0.3 wire form |
|---------------|----------------|
| `{"text": "hello"}` | `{"kind": "text", "text": "hello"}` |
| `{"text": "hello", "metadata": {...}}` | `{"kind": "text", "text": "hello", "metadata": {...}}` |
| `{"url": "https://...", "mediaType": "application/pdf", "filename": "doc.pdf"}` | `{"kind": "file", "file": {"uri": "https://...", "mimeType": "application/pdf", "name": "doc.pdf"}}` |
| `{"raw": "<base64>", "mediaType": "image/png", "filename": "img.png"}` | `{"kind": "file", "file": {"bytes": "<base64>", "mimeType": "image/png", "name": "img.png"}}` |
| `{"data": {"key": "value"}}` | `{"kind": "data", "data": {"key": "value"}}` |
| `{"data": {"key": "value"}, "metadata": {...}}` | `{"kind": "data", "data": {"key": "value"}, "metadata": {...}}` |

**v0.3 wire -> Canonical (inbound normalization):**

| v0.3 wire form | Canonical part |
|----------------|---------------|
| `{"kind": "text", "text": "hello"}` | `{"text": "hello"}` |
| `{"kind": "file", "file": {"uri": "https://...", "mimeType": "application/pdf", "name": "doc.pdf"}}` | `{"url": "https://...", "mediaType": "application/pdf", "filename": "doc.pdf"}` |
| `{"kind": "file", "file": {"bytes": "<base64>", "mimeType": "image/png", "name": "img.png"}}` | `{"raw": "<base64>", "mediaType": "image/png", "filename": "img.png"}` |
| `{"kind": "data", "data": {...}}` | `{"data": {...}}` |

**v1.0 wire -> Canonical:**
v1.0 wire is already canonical form. The only normalization needed is:
- `mimeType` -> `mediaType` (if any v1.0 agent accidentally sends old field name)
- Strip `kind` if accidentally present

**Canonical -> v1.0 wire:**
Pass through as-is — canonical form matches v1.0 wire format.

**Key field renames between versions:**

| Concept | v0.3 field | v1.0 / Canonical field |
|---------|-----------|----------------------|
| MIME type | `mimeType` | `mediaType` |
| File name | `name` (nested in `file`) | `filename` (top-level on Part) |
| File URI | `file.uri` | `url` (top-level) |
| File bytes | `file.bytes` | `raw` (top-level) |

**Test requirements for non-text parts:**
- `test_a2a_compat.py`: round-trip conversion for each part type (canonical -> v0.3 -> canonical, canonical -> v1.0 -> canonical)
- `test_dispatcher.py`: v0.3 and v1.0 agents returning file/data artifacts are correctly normalized and appear in `DispatchResult.raw_parts`
- Verify `_collect_non_text_parts_from_artifact()` and `_collect_non_text_parts_from_message()` work with both canonical and v0.3 shapes

### 4.3 Modified: `hub/agent_registry.py`

**Interface-level selection model (fixes early-collapse bug):**

A single agent card can expose multiple interfaces at different protocol versions. Storing one `protocol_version` per agent would be wrong for dual-mode cards — we might send v1.0 payloads to a v0.3 endpoint. Instead, the selection unit is an **interface tuple**: `(protocol_binding, protocol_version, url)`.

New dataclass in `hub/a2a_compat.py`:
```python
@dataclass(frozen=True)
class ResolvedInterface:
    binding: str         # "JSONRPC"
    protocol_version: str  # "0.3" | "1.0"
    url: str             # The RPC endpoint URL
    # tenant is intentionally excluded — see design decision below
```

**Why no `tenant` field:** The v1.0 spec adds optional `tenant` to `AgentInterface` and request messages for multi-tenant agent deployments. hybro-hub discovers and talks to **local** agents on the same machine — these are inherently single-tenant (the hub user is the only tenant). Adding tenant support would require: tenant extraction from card, tenant propagation in JSON-RPC params, tenant-scoped task tracking, and relay-side tenant mapping — none of which have a use case today. If multi-tenant local agents emerge (e.g. a shared local agent serving multiple hub instances), `ResolvedInterface` would gain an optional `tenant: str | None` field, `build_request_params()` would inject it into the JSON-RPC `params`, and `select_interface()` would filter by tenant affinity. This is explicitly out of scope for this upgrade.

`LocalAgent` gets `interface: ResolvedInterface` (primary) and `fallback_interface: ResolvedInterface | None` (v0.3 alternate for retry). The selection logic in `a2a_compat.select_interface()` picks the best JSON-RPC interface the hub can speak:

```python
def select_interface(card: dict) -> ResolvedInterface:
    """Select the best JSON-RPC interface from an agent card.

    For v1.0 cards with supportedInterfaces:
      1. Filter to JSONRPC binding only (hub doesn't speak gRPC or HTTP+JSON)
      2. Prefer v1.0 interfaces; fall back to v0.3 if no v1.0 JSONRPC available
      3. Use protocolVersion from the selected interface, not a card-level heuristic
    For v0.3 cards (top-level url, no supportedInterfaces):
      Return ResolvedInterface(binding="JSONRPC", protocol_version="0.3", url=card["url"])
    """
```

The dispatcher then uses `agent.interface.protocol_version` and `agent.interface.url` for all protocol decisions.

**Other changes:**
1. Replace `AgentCard.model_validate(card_data)` with `a2a_compat.validate_agent_card(card_data)` — try-v1.0-then-fallback-v0.3 (see updated validation logic below)
2. Add `interface: ResolvedInterface` and `fallback_interface: ResolvedInterface | None` fields to `LocalAgent` dataclass
3. After card fetch, call `a2a_compat.select_interface()` and `a2a_compat.select_fallback_interface()` to populate both fields
4. Remove direct import of `AgentCard` from `a2a.types` (it's now protobuf, not Pydantic)
5. Keep importing `AGENT_CARD_WELL_KNOWN_PATH` and `PREV_AGENT_CARD_WELL_KNOWN_PATH` from `a2a.utils.constants` (unchanged in v1.0)

### 4.4 Modified: `hub/dispatcher.py`

Changes to `_build_jsonrpc()`:
- Accept `version` parameter
- Use `a2a_compat.get_method_name()` for method string
- Use `a2a_compat.build_request_params()` to shape the params (handles part format conversion for v1.0)

Changes to HTTP calls (`_dispatch_sync`, `_dispatch_streaming`, `_fetch_task`, `cancel_task`):
- POST to `agent.interface.url` (replaces `agent.url` for RPC calls; `agent.url` remains the base URL used only for card fetching)
- Pass `a2a_compat.get_headers(agent.interface.protocol_version)` as extra headers

Changes to response parsing (`_extract_response_content`):
- Delegate to `a2a_compat.extract_response(raw, agent.interface.protocol_version)` which handles:
  - v0.3: existing `kind`-based parsing (unchanged logic, just moved)
  - v1.0: member-presence parsing (`task` / `message` keys)
  - Normalizes states/roles to canonical lowercase

Changes to streaming (`_dispatch_streaming`):
- The existing `inner = data.get("result", data)` unwrapping step is preserved for both versions
- Delegate event classification to `a2a_compat.classify_stream_event(inner, agent.interface.protocol_version)`:
  - v0.3: checks `inner.get("kind")` -> `"artifact-update"`, `"status-update"`, `"task"`, `"message"`
  - v1.0: checks for **camelCase ProtoJSON keys** on `inner`: `"statusUpdate"`, `"artifactUpdate"`, `"task"`, `"message"`
  - Both return a normalized `(event_type, normalized_data)` tuple using canonical event names and lowercase states
- The rest of the streaming logic (yielding `DispatchEvent`) stays the same

**v1.0 StreamResponse JSON keys (authoritative reference):**
The v1.0 `StreamResponse` protobuf has `oneof response` with proto field names `status_update`, `artifact_update`, `task`, `message`. ProtoJSON serializes these as **camelCase**: `statusUpdate`, `artifactUpdate`, `task`, `message`. The classifier checks for exactly these four camelCase keys. Do NOT check for snake_case (`status_update`) or prefixed forms (`taskStatusUpdate`).

Changes to polling (`_poll_until_terminal`):
- State comparisons use canonical lowercase (already the case)
- `a2a_compat.normalize_task_state()` applied to fetched task states

**JSON-RPC error extraction (new):**

The current dispatcher treats all non-2xx responses as opaque exceptions (via `resp.raise_for_status()`). For the fallback retry to work, we need to extract structured JSON-RPC errors before raising. New model in `hub/a2a_compat.py`:

```python
@dataclass(frozen=True)
class JsonRpcError:
    code: int       # e.g. -32601
    message: str    # e.g. "Method not found"
    data: Any = None

FALLBACK_ELIGIBLE_CODES = {-32601, -32009}  # MethodNotFound, VersionNotSupportedError

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

In `_dispatch_sync()` and `_dispatch_streaming()`, check for JSON-RPC-level errors **before** content extraction. This applies to **all HTTP responses**, not just 200:

1. **200 responses**: Parse the JSON body and call `extract_jsonrpc_error()`. JSON-RPC 2.0 uses HTTP 200 for all responses including errors — the error is in the `"error"` field of the JSON body, not the HTTP status.
2. **Non-200 responses (4xx/5xx)**: Attempt to parse the response body as JSON and call `extract_jsonrpc_error()`. Some A2A implementations return HTTP error codes alongside parsable JSON-RPC error bodies (the spec's error mapping includes HTTP status semantics). Only fall through to `raise_for_status()` if the body is not parsable JSON or contains no JSON-RPC error field.

If `extract_jsonrpc_error()` returns a code in `FALLBACK_ELIGIBLE_CODES`, raise `A2AVersionFallbackError`. For all other JSON-RPC errors, convert to a descriptive exception (preserving the code and message) rather than a raw HTTP error.

For streaming, a JSON-RPC error may appear as the first (and only) SSE event. The streaming parser should check the first event for an `"error"` field before entering the event classification loop.

Changes to dispatch entry point (`dispatch()`):
- Catch `A2AVersionFallbackError` from v1.0 dispatch
- Retry using `agent.fallback_interface` (v0.3 URL from card, or same URL for single-interface cards)
- Log a warning with agent name and original error code
- If retry also fails, propagate the original error

### 4.5 Modified: `hub/main.py`

Changes to message construction:
- The `reply_message` and user message dicts currently use v0.3-style parts with `kind` field
- Before dispatching, pass through `a2a_compat.build_message_parts()` to convert to the target version's format
- Alternatively, keep constructing in canonical (v0.3) format and let `dispatcher._build_jsonrpc()` handle the conversion per-agent — this is simpler and preferred

Decision: **main.py constructs messages in canonical format; dispatcher converts per-agent.** This minimizes changes to main.py.

### 4.6 State/Enum Mapping Tables

**Task States:**

| Canonical (internal) | v0.3 wire | v1.0 wire |
|---------------------|-----------|-----------|
| `submitted` | `submitted` | `TASK_STATE_SUBMITTED` |
| `working` | `working` | `TASK_STATE_WORKING` |
| `completed` | `completed` | `TASK_STATE_COMPLETED` |
| `failed` | `failed` | `TASK_STATE_FAILED` |
| `canceled` | `canceled` | `TASK_STATE_CANCELED` |
| `rejected` | `rejected` | `TASK_STATE_REJECTED` |
| `input-required` | `input-required` | `TASK_STATE_INPUT_REQUIRED` |
| `auth-required` | `auth-required` | `TASK_STATE_AUTH_REQUIRED` |

**Roles:**

| Canonical | v0.3 wire | v1.0 wire |
|-----------|-----------|-----------|
| `user` | `user` | `ROLE_USER` |
| `agent` | `agent` | `ROLE_AGENT` |

**JSON-RPC Methods:**

| Canonical | v0.3 wire | v1.0 wire |
|-----------|-----------|-----------|
| `send` | `message/send` | `SendMessage` |
| `stream` | `message/stream` | `SendStreamingMessage` |
| `get_task` | `tasks/get` | `GetTask` |
| `cancel_task` | `tasks/cancel` | `CancelTask` |

**Streaming Event Types (inbound):**

| Canonical | v0.3 discriminator | v1.0 JSON member (ProtoJSON of `StreamResponse.oneof`) |
|-----------|-------------------|-------------------|
| `status-update` | `kind: "status-update"` | `"statusUpdate"` key present |
| `artifact-update` | `kind: "artifact-update"` | `"artifactUpdate"` key present |
| `task` | `kind: "task"` | `"task"` key present |
| `message` | `kind: "message"` | `"message"` key present |

v1.0 stream events are `StreamResponse` protobuf messages serialized as ProtoJSON. The `oneof response` field appears as exactly one top-level key. Classification uses `HasField` semantics: check which key is present.

Note: v1.0 removes `final` boolean from `TaskStatusUpdateEvent`. For v1.0 agents, stream termination is determined by the task reaching a terminal state (`TASK_STATE_COMPLETED`, `TASK_STATE_FAILED`, `TASK_STATE_CANCELED`, `TASK_STATE_REJECTED`), not by a `final` flag. The normalized output always sets `final=True` when the state is terminal, preserving backward compat for downstream consumers.

### 4.7 Agent Card Validation Logic (try-both, not guess-first)

The contract is "find a usable schema," not "guess one schema and fail." For dual-mode cards that could parse as either version, we try v1.0 first (stricter, more information) and fall back to v0.3.

```python
def validate_agent_card(card_data: dict) -> dict | None:
    """Validate an agent card, trying v1.0 first, then falling back to v0.3.

    Returns the validated card dict, or None if neither schema accepts it.
    Does NOT hard-switch on a heuristic — always attempts both parsers.

    IMPORTANT: Both parsers must tolerate unknown/vendor fields.
    The A2A spec requires implementations to ignore unrecognized fields
    for forward compatibility. A valid v1.0+ card with extra vendor
    fields (e.g. "x-vendor-feature") must not be rejected.
    """
    # Try v1.0 first (protobuf ParseDict with unknown field tolerance)
    try:
        from google.protobuf.json_format import ParseDict
        from a2a.types import AgentCard
        ParseDict(card_data, AgentCard(), ignore_unknown_fields=True)
        return card_data
    except Exception:
        pass

    # Fall back to v0.3 Pydantic (model_config already has extra="allow"
    # in the generated compat types; if not, override with ConfigDict)
    try:
        from a2a.compat.v0_3.types import AgentCard as V03AgentCard
        V03AgentCard.model_validate(card_data)
        return card_data
    except Exception:
        pass

    return None
```

**Forward compatibility note:** `ParseDict(..., ignore_unknown_fields=True)` is required because the A2A spec mandates that implementations ignore unrecognized fields. Without this flag, `ParseDict` raises `ParseError` on any field not in the proto definition, which would cause discovery to silently drop valid cards that include vendor extensions or fields from a newer spec revision. The v0.3 Pydantic path should also tolerate extras — verify during Phase 1 that `V03AgentCard` has `model_config = ConfigDict(extra="allow")` or equivalent; if not, wrap the call to strip unknown fields before validation.

### 4.8 Interface Selection Logic

The selection unit is an interface tuple, not a whole-card heuristic. `select_interface()` reads `protocolVersion` from each `supportedInterfaces` entry, not from the card level.

```python
def select_interface(card: dict) -> ResolvedInterface:
    """Select the best JSON-RPC interface from an agent card.

    For cards with supportedInterfaces:
      1. Filter to JSONRPC binding only
      2. Group by protocolVersion from each interface entry
      3. Prefer v1.0 interface; fall back to v0.3 if no v1.0 JSONRPC
      4. If protocolVersion is missing on an interface, treat as v0.3
    For v0.3 cards (top-level url, no supportedInterfaces):
      Return (JSONRPC, 0.3, card["url"])
    Raise ValueError if no usable JSON-RPC interface found.
    """
    interfaces = card.get("supportedInterfaces", [])

    jsonrpc_interfaces: list[ResolvedInterface] = []
    for iface in interfaces:
        binding = iface.get("protocolBinding", "")
        if binding != "JSONRPC":
            continue
        pv = iface.get("protocolVersion", "0.3")
        url = iface.get("url", "")
        if url:
            jsonrpc_interfaces.append(ResolvedInterface(
                binding="JSONRPC", protocol_version=pv, url=url,
            ))

    if jsonrpc_interfaces:
        # Prefer highest version the hub supports
        for target_version in ("1.0", "0.3"):
            for ri in jsonrpc_interfaces:
                if ri.protocol_version == target_version:
                    return ri
        # If neither exact match, take the first JSONRPC interface
        return jsonrpc_interfaces[0]

    # v0.3 fallback: top-level url
    url = card.get("url", "")
    if url:
        return ResolvedInterface(binding="JSONRPC", protocol_version="0.3", url=url)

    raise ValueError("No usable JSON-RPC interface in agent card")
```

**Runtime fallback for v1.0 dispatch failures:**

When a v1.0 dispatch returns JSON-RPC error `-32601` (MethodNotFound) or `-32009` (VersionNotSupportedError), the dispatcher retries once with v0.3 wire format. The retry target depends on what the card offers:

1. **Dual-mode card** (has both v1.0 and v0.3 JSONRPC interfaces): retry against the v0.3 interface URL from the card. This is the correct endpoint for legacy protocol.
2. **Single-interface card** (only one JSONRPC entry, advertised v1.0): retry against the same URL with v0.3 wire format. This covers stale-card cases where the server hasn't actually upgraded.
3. **v0.3 card**: no fallback needed — already using v0.3.

To support this, `select_interface()` returns the primary choice, and a new `select_fallback_interface()` function returns the v0.3 alternate (or `None` if unavailable). `LocalAgent` stores both:
```python
@dataclass
class LocalAgent:
    ...
    interface: ResolvedInterface           # Primary (preferred)
    fallback_interface: ResolvedInterface | None  # v0.3 alternate for retry
```

For single-interface v1.0 cards, `fallback_interface` is synthesized as `ResolvedInterface(binding="JSONRPC", protocol_version="0.3", url=<same url>)`.

If the retry also fails, propagate the original v1.0 error. This fallback is implemented in `dispatcher.py`, not in the compat layer.

## 5. File Change Summary

| File | Change Type | Description |
|------|-----------|-------------|
| `hub/a2a_compat.py` | **NEW** | Central protocol compat layer with `ResolvedInterface`, validation, interface selection, wire format translation (~300 lines) |
| `hub/agent_registry.py` | MODIFY | Replace `AgentCard.model_validate()`, add `interface: ResolvedInterface` to `LocalAgent`, use compat validation and selection |
| `hub/dispatcher.py` | MODIFY | Route method names/headers/parts/responses through compat layer per `agent.interface.protocol_version`; add v1.0 dispatch retry fallback |
| `hub/main.py` | MINOR MODIFY | Fix HITL reply part construction to use canonical flattened form (remove `kind` from `main.py:321`) |
| `tests/test_a2a_compat.py` | **NEW** | Unit tests for compat module: validation, interface selection, all mapping functions |
| `tests/test_agent_registry.py` | MODIFY | Add v0.3 and v1.0 card fixtures, test interface selection and version detection |
| `tests/test_dispatcher.py` | MODIFY | Add v1.0 sync/stream test paths, verify state normalization, test v1.0->v0.3 fallback retry |
| `tests/test_main.py` | MODIFY | Verify messages dispatched with correct protocol per agent; verify canonical part format |
| `tests/e2e_live_test.py` | MODIFY | Split into two manual verification paths: one targeting a v0.3 agent, one targeting a v1.0 agent. Mark as `pytest.mark.manual` (not in default test suite). If the current test cannot run against a v1.0 agent yet, add a TODO and keep it v0.3-only for now. |
| `pyproject.toml` | NO CHANGE | Already declares `a2a-sdk>=1.0.1,<2` |
| `uv.lock` | REGENERATE | Last step: `uv lock` to pick up v1.0.x |

## 6. Execution Order

### Phase 1: Compat Layer Foundation
1. Create `hub/a2a_compat.py` with `ResolvedInterface`, `validate_agent_card()`, `select_interface()`, and all mapping/conversion functions
2. Create `tests/test_a2a_compat.py` — unit tests for: validation (both schemas, malformed cards, cards with vendor extensions), interface selection (v0.3 card, v1.0 card, dual-mode card, missing JSONRPC), every mapping function (states, roles, methods, stream events), and **all part types**: text, file-by-url, file-by-bytes, structured data — round-trip conversion tests (canonical -> v0.3 -> canonical, canonical -> v1.0 -> canonical) including `mediaType`/`mimeType` and `filename`/`name` field renaming

### Phase 2: Agent Discovery
3. Add `interface: ResolvedInterface` field to `LocalAgent` dataclass in `hub/agent_registry.py`
4. Replace `AgentCard.model_validate()` with `a2a_compat.validate_agent_card()` + `a2a_compat.select_interface()`
5. Update `tests/test_agent_registry.py` with v0.3 card fixture, v1.0 card fixture, dual-mode card fixture

### Phase 3: Dispatcher Dual Protocol
6. Modify `hub/dispatcher.py` to route through compat layer based on `agent.interface.protocol_version`
7. Add v1.0->v0.3 fallback retry on `MethodNotFound`/`VersionNotSupported`
8. Update `tests/test_dispatcher.py` with v1.0 sync/stream test paths and fallback retry test

### Phase 4: Main Loop + Canonical Parts
9. Fix HITL reply construction in `hub/main.py` to use canonical flattened parts (remove `kind`)
10. Update `tests/test_main.py` to verify correct protocol dispatch per agent interface version

### Phase 5: Lock File and Integration
11. Run `uv lock` to regenerate lock file with `a2a-sdk>=1.0.1`
12. Run full unit test suite: `pytest tests/ -k 'not e2e'`
13. Update `tests/e2e_live_test.py` — split into v0.3 and v1.0 paths (manual verification)
14. If possible, manual integration test with a real v0.3 agent and a v1.0 agent

## 7. Risk Mitigation

| Risk | Mitigation |
|------|-----------|
| `a2a.compat.v0_3` module API differs from what we expect | Verify imports early in Phase 1; fall back to manual conversion if needed |
| `google.protobuf` becomes a transitive dependency | Already pulled in by `a2a-sdk>=1.0.1`; no new direct dependency needed |
| Some v0.3 agents return cards that fail v1.0 protobuf `ParseDict` but passed Pydantic `model_validate` | `validate_agent_card()` tries v1.0 first, falls back to v0.3 — always tries both, never hard-switches on a heuristic |
| Agent card advertises v1.0 but server actually speaks v0.3 | Dispatcher retries once with v0.3 wire format on `MethodNotFound` / `VersionNotSupported` JSON-RPC errors |
| Dual-mode card with multiple JSONRPC interfaces at different versions | `select_interface()` reads `protocolVersion` from each interface entry, prefers v1.0, falls back to v0.3 — never guesses from card-level fields |
| Relay event format accidentally changes | Explicit normalization to canonical lowercase states; test assertions on relay event shape |
| `a2a-adapter` compat with `a2a-sdk>=1.0.1` | `a2a-adapter>=0.2.9` should already work; verify during lock regeneration |
| v1.0 streaming event keys misidentified | Verified: ProtoJSON `StreamResponse.oneof` serializes as `statusUpdate`/`artifactUpdate` (not `taskStatusUpdate`/`taskArtifactUpdate`). Test fixtures will cover exact key names. |
| v1.0 agent card with vendor extensions rejected by discovery | `ParseDict` called with `ignore_unknown_fields=True`; verify in Phase 1 that v0.3 Pydantic path also tolerates extras |
| Non-text parts (file/data) silently corrupted during conversion | Explicit mapping tables for all part types; round-trip conversion tests in `test_a2a_compat.py`; `_collect_non_text_parts_*` verified against both wire formats |

## 8. Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| v0.3 `A2A-Version` header | **Omit** (do not send `A2A-Version: 0.3`) | Intentional compatibility tradeoff: the v1.0 spec leans toward always sending the header (with 0.3 assumed for missing), but many pre-v1.0 agents were built before the header existed and may reject or mishandle an unexpected `A2A-Version` header. Omitting is safer for the existing local agent ecosystem. Revisit if we encounter v0.3 agents that require the header. |
| v1.0 `A2A-Version` header | **Always send** `A2A-Version: 1.0` | Required by spec for v1.0 requests |
| Fallback target for dual-mode cards | **Use v0.3 interface URL from card** | Dual-mode cards may have separate endpoints per version; retrying against the v1.0 URL with v0.3 format is wrong if a dedicated v0.3 URL exists |
| Fallback target for single-interface v1.0 cards | **Same URL, v0.3 wire format** | Only option for stale-card cases; acceptable because v0.3 agents typically serve all methods at one URL |
| Internal canonical part format | **Flattened** (no `kind`) | Matches relay inbound format and v1.0 wire format; minimizes conversion steps |
| `main.py` message construction | **Canonical format; dispatcher converts** | Keeps main.py changes minimal; single conversion point in dispatcher |
| Multi-tenant (`tenant` field) | **Excluded from `ResolvedInterface`** | Local agents are single-tenant; no use case today. Documented extension path if needed. |
| Agent card unknown fields | **Tolerate** (`ignore_unknown_fields=True`) | A2A spec requires forward compatibility; vendor extensions must not break discovery |

## 9. Not In Scope

- hybro-hub as an A2A server accepting inbound requests
- External v0.3 client connections to hybro-hub
- `ListTasks` support (new v1.0 method — not needed for client dispatch)
- Agent card signature verification
- gRPC transport binding
- OAuth 2.0 flow updates
- Multi-tenant support (`tenant` field on interfaces and requests) — local agents are single-tenant; documented extension path in 4.3
