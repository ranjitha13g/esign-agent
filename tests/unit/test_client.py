"""Client behaviour: envelope errors, catalogue scoping, closed schemas, idempotency."""

from __future__ import annotations

import json

import pytest

from mcp.client import McpClient, McpError, Tool, ToolNotAvailable

LIST_TOOL = {
    "name": "EsignDocument.list",
    "description": "List e-sign documents",
    "inputSchema": {
        "type": "object",
        "properties": {"limit": {"type": "integer"}},
        "additionalProperties": False,
    },
}

UPDATE_TOOL = {
    "name": "EsignDocument.update",
    "description": "Update an e-sign document",
    "inputSchema": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "reminder_count": {"type": "integer"},
            "idempotency_key": {"type": "string"},
        },
        "required": ["id"],
        "additionalProperties": False,
    },
}

VOID_TOOL = {
    "name": "EsignDocument.void",
    "description": "Void a document",
    "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}},
}


class ScriptedTransport:
    """Returns a queued JSON-RPC body per call, recording what it was asked."""

    def __init__(self, bodies):
        self._bodies = list(bodies)
        self.calls = []

    def call(self, method, params=None, *, retriable=True):
        self.calls.append((method, params, retriable))
        return self._bodies.pop(0)

    def close(self):
        pass


def connected(extra_bodies=(), tools=(LIST_TOOL, UPDATE_TOOL, VOID_TOOL)):
    bodies = [
        {"result": {"protocolVersion": "2025-11-25"}},  # initialize
        {"result": {}},  # notifications/initialized
        {"result": {"tools": list(tools)}},  # tools/list
        *extra_bodies,
    ]
    t = ScriptedTransport(bodies)
    c = McpClient(t)
    c.connect()
    return c, t


# -- the HTTP-200 trap --------------------------------------------------------


def test_error_envelope_raises_despite_http_200():
    """A denial arrives as 200 with an error body. Treating non-200 as the only
    failure would read every refusal as a success."""
    c, _ = connected([{"jsonrpc": "2.0", "id": 4, "error": {"code": -32000, "message": "denied"}}])
    with pytest.raises(McpError) as exc:
        c.call("EsignDocument.list", {})
    assert exc.value.code == -32000
    assert "denied" in exc.value.message


def test_successful_envelope_returns_result():
    c, _ = connected([{"jsonrpc": "2.0", "id": 4, "result": {"documents": [{"id": "d1"}]}}])
    assert c.call("EsignDocument.list", {}) == {"documents": [{"id": "d1"}]}


def test_null_error_field_is_not_an_error():
    c, _ = connected([{"jsonrpc": "2.0", "id": 4, "error": None, "result": {"ok": True}}])
    assert c.call("EsignDocument.list", {}) == {"ok": True}


# -- catalogue scoping --------------------------------------------------------


def test_tool_outside_the_seat_is_absent_not_refused():
    """A tool this seat may not use is missing from tools/list. That absence is the
    permission answer, so it must not be reported as a typo or retried."""
    c, _ = connected()
    with pytest.raises(ToolNotAvailable) as exc:
        c.call("Contract.list", {})
    assert "seat boundary" in str(exc.value)


def test_transition_tools_are_distinguished_from_crud():
    assert Tool("EsignDocument.void").is_transition
    assert not Tool("EsignDocument.list").is_transition
    assert not Tool("EsignDocument.update").is_transition
    assert Tool("EsignDocument.void").is_write
    assert Tool("EsignDocument.update").is_write
    assert not Tool("EsignDocument.get").is_write


# -- closed schemas -----------------------------------------------------------


def test_unknown_argument_is_rejected_locally_with_a_named_field():
    """Schemas are closed. The platform would reject this too, but as a generic
    envelope error -- a planner retrying blind burns steps it cannot spare."""
    c, t = connected()
    with pytest.raises(ValueError) as exc:
        c.call("EsignDocument.list", {"limitt": 5})
    assert "EsignDocument.list" in str(exc.value)
    # Nothing left the client.
    assert [c[0] for c in t.calls] == ["initialize", "notifications/initialized", "tools/list"]


def test_missing_required_argument_is_rejected_locally():
    c, _ = connected()
    with pytest.raises(ValueError):
        c.call("EsignDocument.update", {"reminder_count": 1})


def test_valid_arguments_pass_through():
    c, t = connected([{"result": {"ok": True}}])
    c.call("EsignDocument.list", {"limit": 10})
    assert t.calls[-1][1]["arguments"] == {"limit": 10}


# -- idempotency --------------------------------------------------------------


def test_idempotency_key_is_attached_to_writes_when_the_schema_allows():
    c, t = connected([{"result": {"ok": True}}])
    c.call("EsignDocument.update", {"id": "d1"}, idempotency_key="abc123")
    assert t.calls[-1][1]["arguments"]["idempotency_key"] == "abc123"


def test_idempotency_key_is_not_forced_where_the_schema_forbids_it():
    """Adding it anyway would trip the closed schema and fail the write."""
    c, t = connected([{"result": {"ok": True}}])
    c.call("EsignDocument.void", {"id": "d1"}, idempotency_key="abc123")
    assert "idempotency_key" not in t.calls[-1][1]["arguments"]


def test_reads_do_not_carry_an_idempotency_key():
    """The read tool here *does* accept the field, so the schema cannot be what
    stops it. Only the is_write check can -- which is the thing under test.

    The earlier version of this test used a schema without the property, so it
    passed whether or not that check existed. Mutation testing caught it.
    """
    read_tool = {
        "name": "EsignDocument.list",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer"},
                "idempotency_key": {"type": "string"},
            },
            "additionalProperties": False,
        },
    }
    c, t = connected([{"result": {"ok": True}}], tools=(read_tool, UPDATE_TOOL, VOID_TOOL))
    c.call("EsignDocument.list", {"limit": 1}, idempotency_key="abc123")
    assert "idempotency_key" not in t.calls[-1][1]["arguments"]


# -- the MCP content envelope -------------------------------------------------
# tools/call returns {"content": [{"type": "text", "text": "<json>"}]}, so row data
# arrives as a JSON string inside a text block rather than as an object.


def test_content_envelope_is_unwrapped_to_an_object():
    body = {"result": {"content": [{"type": "text", "text": '{"data": [{"id": "d1"}]}'}]}}
    c, _ = connected([body])
    assert c.call("EsignDocument.list", {}) == {"data": [{"id": "d1"}]}


def test_non_json_text_content_is_returned_as_text():
    body = {"result": {"content": [{"type": "text", "text": "plain words"}]}}
    c, _ = connected([body])
    assert c.call("EsignDocument.list", {}) == "plain words"


def test_is_error_flag_raises_even_though_the_envelope_looks_fine():
    """isError is a tool-level failure, separate from a JSON-RPC error. Missing it
    would read a failed write as a successful one."""
    body = {
        "result": {
            "content": [{"type": "text", "text": "document is not in a sendable state"}],
            "isError": True,
        }
    }
    c, _ = connected([body])
    with pytest.raises(McpError) as exc:
        c.call("EsignDocument.void", {"id": "d1"})
    assert "sendable state" in str(exc.value)


def test_result_without_content_passes_through_unchanged():
    c, _ = connected([{"result": {"tools": []}}])
    assert c.call("EsignDocument.list", {}) == {"tools": []}



# -- paging -------------------------------------------------------------------
# No test covered page(); mutation testing found that stopping after the first page
# went unnoticed. That is the exact bug that once reported "nothing is pending" while
# five sent documents sat on page two.


def paged(total: int, page_size: int = 100):
    """A list tool that answers in pages, as the platform does."""
    rows = [{"id": f"d{i}"} for i in range(total)]

    class T:
        def __init__(self):
            self.offsets = []

        def call(self, method, params=None, *, retriable=True):
            if method != "tools/call":
                return {"result": {}}
            args = params["arguments"]
            off = args.get("offset", 0)
            lim = args.get("limit", page_size)
            self.offsets.append(off)
            body = {"data": rows[off : off + lim], "total": total, "offset": off}
            text = json.dumps(body)
            return {"result": {"content": [{"type": "text", "text": text}]}}

        def close(self):
            pass

    return T()


PAGED_TOOL_SCHEMA = {
    "type": "object",
    "properties": {"limit": {"type": "integer"}, "offset": {"type": "integer"}},
    "additionalProperties": False,
}


def connected_to(transport):
    c = McpClient(transport)
    c._tools = {"EsignDocument.list": Tool("EsignDocument.list", "", PAGED_TOOL_SCHEMA)}
    return c


def test_page_reads_every_page_not_just_the_first():
    t = paged(250)
    rows = connected_to(t).page("EsignDocument.list", page_size=100)
    assert len(rows) == 250
    assert t.offsets == [0, 100, 200]


def test_page_handles_an_exact_multiple_without_an_extra_call():
    t = paged(200)
    rows = connected_to(t).page("EsignDocument.list", page_size=100)
    assert len(rows) == 200


def test_page_on_an_empty_entity_returns_nothing_and_stops():
    t = paged(0)
    assert connected_to(t).page("EsignDocument.list", page_size=100) == []
    assert t.offsets == [0]
