"""MCP client for AgentSwitch.

Three platform facts shape this file, and each has bitten somebody:

1. A JSON-RPC error still returns HTTP 200. The failure is in the envelope.
2. Tool argument schemas are closed -- an argument not in the schema is rejected
   rather than ignored, so it is worth catching locally with a clear message.
3. A tool you may not use is absent from tools/list, not refused when called. So an
   unknown tool name is a scoping fact about this seat, not a typo to retry.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft7Validator
from jsonschema import ValidationError as SchemaValidationError

from .transport import MCP_PROTOCOL, Transport

CRUD_VERBS = {"list", "get", "create", "update", "delete"}
WRITE_VERBS = {"create", "update", "delete"}


class McpError(RuntimeError):
    """The platform answered 200 with an error envelope."""

    def __init__(self, code: Any, message: str, data: Any = None) -> None:
        super().__init__(f"MCP error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data


class ToolNotAvailable(KeyError):
    """Not in this seat's catalogue -- which is a permission answer, not a typo."""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str = ""
    schema: dict[str, Any] = field(default_factory=dict)

    @property
    def entity(self) -> str:
        return self.name.rsplit(".", 1)[0] if "." in self.name else self.name

    @property
    def verb(self) -> str:
        return self.name.rsplit(".", 1)[-1] if "." in self.name else self.name

    @property
    def is_transition(self) -> bool:
        """One tool per workflow transition, so a non-CRUD verb is a state move."""
        return "." in self.name and self.verb not in CRUD_VERBS

    @property
    def is_write(self) -> bool:
        return self.verb in WRITE_VERBS or self.is_transition


class McpClient:
    def __init__(self, transport: Transport, client_name: str = "team-18-esign") -> None:
        self._t = transport
        self._client_name = client_name
        self._tools: dict[str, Tool] = {}
        self._initialized = False

    # -- lifecycle ---------------------------------------------------------

    def connect(self) -> dict[str, Tool]:
        """initialize -> notifications/initialized -> tools/list."""
        self._unwrap(
            self._t.call(
                "initialize",
                {
                    "protocolVersion": MCP_PROTOCOL,
                    "capabilities": {},
                    "clientInfo": {"name": self._client_name, "version": "0.1"},
                },
            )
        )
        self._t.call("notifications/initialized")
        self._initialized = True
        return self.list_tools()

    def list_tools(self, refresh: bool = False) -> dict[str, Tool]:
        if self._tools and not refresh:
            return self._tools
        result = self._unwrap(self._t.call("tools/list")) or {}
        self._tools = {
            t["name"]: Tool(
                name=t["name"],
                description=t.get("description", ""),
                schema=t.get("inputSchema") or t.get("input_schema") or {},
            )
            for t in result.get("tools", [])
        }
        return self._tools

    # -- calling -----------------------------------------------------------

    def call(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        tools = self.list_tools()
        if name not in tools:
            raise ToolNotAvailable(
                f"{name!r} is not in this seat's catalogue ({len(tools)} tools). "
                "A tool you may not use is absent rather than refused, "
                "so this is the seat boundary."
            )

        tool = tools[name]
        args = dict(arguments or {})

        # Make retries provably safe: the idempotency task asserts that inducing a retry
        # leaves exactly one notification behind.
        if tool.is_write and idempotency_key is not None:
            if self._schema_allows(tool, "idempotency_key"):
                args.setdefault("idempotency_key", idempotency_key)

        self._validate(tool, args)

        # A write the platform cannot deduplicate must not be retried: the attempt
        # that looked like it failed may well have landed. Reads are always safe.
        safe_to_repeat = not tool.is_write or "idempotency_key" in args
        body = self._t.call(
            "tools/call", {"name": name, "arguments": args}, retriable=safe_to_repeat
        )
        return self._unwrap_content(self._unwrap(body), tool_name=name)

    def page(self, name: str, page_size: int = 100, **filters: Any) -> list[dict[str, Any]]:
        """Read an entity in full.

        The default page is 20. Reading one page and reasoning about it as though it
        were the entity is how you answer "nothing is pending" while five sent
        documents sit on page two -- and the answer looks entirely plausible.
        """
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            result = self.call(name, {"limit": page_size, "offset": offset, **filters})
            batch = result.get("data", []) if isinstance(result, dict) else []
            rows.extend(batch)
            total = result.get("total") if isinstance(result, dict) else None
            offset += len(batch)
            if not batch or len(batch) < page_size or (total is not None and offset >= total):
                return rows

    def new_idempotency_key(self) -> str:
        return uuid.uuid4().hex

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _schema_allows(tool: Tool, key: str) -> bool:
        props = (tool.schema or {}).get("properties") or {}
        return key in props

    @staticmethod
    def _validate(tool: Tool, arguments: dict[str, Any]) -> None:
        """Catch closed-schema violations here, with a message that names the field.

        The platform would reject these anyway; the point is that its rejection arrives
        as a generic error envelope, and a planner retrying blind burns steps.
        """
        schema = tool.schema
        if not schema:
            return
        try:
            Draft7Validator(schema).validate(arguments)
        except SchemaValidationError as exc:
            where = ".".join(str(p) for p in exc.absolute_path) or "(root)"
            raise ValueError(
                f"arguments rejected for {tool.name} at {where}: {exc.message}"
            ) from exc

    @staticmethod
    def _unwrap_content(result: Any, tool_name: str = "") -> Any:
        """Lift the payload out of the MCP content envelope.

        A tools/call result is {"content": [{"type": "text", "text": "<json>"}]}, so the
        row data arrives as a JSON string inside a text block rather than as an object.
        Two things ride on unwrapping it here: callers get dicts instead of strings, and
        isError -- which is a tool-level failure flag distinct from a JSON-RPC error --
        gets noticed rather than silently read as a successful result.
        """
        if not isinstance(result, dict) or "content" not in result:
            return result

        blocks = result.get("content") or []
        text = "".join(
            b.get("text", "")
            for b in blocks
            if isinstance(b, dict) and b.get("type") == "text"
        )

        payload: Any = text
        if text:
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = text

        if result.get("isError"):
            message = payload if isinstance(payload, str) else json.dumps(payload)
            raise McpError("isError", "{} failed: {}".format(tool_name or "tool", message))

        return payload

    @staticmethod
    def _unwrap(body: dict[str, Any]) -> Any:
        """HTTP 200 is not success. The envelope decides."""
        if not isinstance(body, dict):
            raise McpError("malformed", f"expected a JSON object, got {type(body).__name__}")
        if "error" in body and body["error"] is not None:
            err = body["error"]
            if isinstance(err, dict):
                raise McpError(err.get("code"), err.get("message", ""), err.get("data"))
            raise McpError("unknown", str(err))
        return body.get("result")

    def close(self) -> None:
        self._t.close()
