"""An in-memory platform that speaks the same transport interface as the real one.

Needed because the live book cannot exercise the decisions that matter. On Suryodaya
every document is 19 days old, 95 are drafts, and the 5 sent ones have no signers at
all -- so "chase the stale ones" has nothing to act on. created_at is server-stamped
and ignored, so a realistic ageing scenario cannot be seeded there either.

This is not a mock of our own code. It stands in for the platform at the transport
seam, so McpClient, the policy and the planner all run unmodified against it, and the
verifiers read state out of it exactly as they read state out of the real thing.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from copy import deepcopy
from typing import Any

from mcp.transport import MCP_PROTOCOL

CRUD = ("list", "get", "create", "update", "delete")

# Mirrors the real catalogue for the slice the agent uses.
TOOLS: list[dict[str, Any]] = []
for entity in ("EsignDocument", "EsignSigner", "Company", "AgentMemory"):
    for verb in CRUD:
        TOOLS.append(
            {
                "name": f"{entity}.{verb}",
                "description": f"{verb} {entity}",
                "inputSchema": {"type": "object", "properties": {}, "additionalProperties": True},
            }
        )
for name, props in (
    ("EsignDocument.send_for_signature", {"id": {"type": "string"}}),
    (
        "EsignDocument.reissue_signing_link",
        {"id": {"type": "string"}, "signer_id": {"type": "string"}},
    ),
):
    TOOLS.append(
        {
            "name": name,
            "description": name,
            "inputSchema": {
                "type": "object",
                "properties": props,
                "required": list(props),
                "additionalProperties": False,
            },
        }
    )


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class FakePlatform:
    """Holds rows and answers tool calls against them."""

    def __init__(self, absent: tuple[str, ...] = ("Contract", "SalarySlip")) -> None:
        self.rows: dict[str, list[dict[str, Any]]] = {
            "Company": [
                {
                    "id": "co-1",
                    "name": "Example Precision Works",
                    "country": "India",
                    "default_currency": "INR",
                    "active_domains": [{"domain": "esign"}, {"domain": "crm"}],
                }
            ],
            "EsignDocument": [],
            "EsignSigner": [],
            "AgentMemory": [],
        }
        self.absent = set(absent)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        # Set to a number to make the Nth write fail once, for retry testing.
        self.fail_write_on: int | None = None
        self._writes = 0
        # Mutations that fire when a named tool is called, before it answers.
        # Keyed by tool name so a scenario can land a concurrent write exactly
        # between the policy's read and the executor's re-read.
        self._on_call: dict[str, list[Any]] = {}

    # -- seeding ---------------------------------------------------------

    def add_document(
        self,
        *,
        title: str,
        status: str = "sent",
        days_old: float = 10.0,
        signing_order: str = "parallel",
        expires_in_days: float | None = None,
        signers: list[dict[str, Any]] | None = None,
        **extra: Any,
    ) -> str:
        doc_id = str(uuid.uuid4())
        stamp = (_now() - dt.timedelta(days=days_old)).isoformat()
        self.rows["EsignDocument"].append(
            {
                "id": doc_id,
                "title": title,
                "status": status,
                "signing_order": signing_order,
                "current_signer_order": 1.0 if signing_order == "sequential" else None,
                "expires_at": (
                    (_now() + dt.timedelta(days=expires_in_days)).isoformat()
                    if expires_in_days is not None
                    else None
                ),
                "created_at": stamp,
                "updated_at": stamp,
                "replaced_by_document_id": None,
                "company_id": "co-1",
                **extra,
            }
        )
        for i, s in enumerate(signers or [], start=1):
            self.rows["EsignSigner"].append(
                {
                    "id": str(uuid.uuid4()),
                    "esign_document_id": doc_id,
                    "email": s.get("email", f"signer{i}@example.com"),
                    "full_name": s.get("full_name", f"Signer {i}"),
                    "sign_order": float(s.get("sign_order", i)),
                    "status": s.get("status", "pending"),
                    "last_opened_at": s.get("last_opened_at"),
                    "access_token_expires_at": s.get("access_token_expires_at"),
                    "auth_method": s.get("auth_method", "email_link"),
                }
            )
        return doc_id

    def mutate_on(self, tool: str, fn: Any) -> None:
        """Change the world when `tool` is next called, before it answers.

        This is how the concurrency task reproduces another team writing to a shared
        row mid-run. Pointing it at the executor's re-read (EsignDocument.get) is what
        makes the test prove the re-read happened, rather than merely proving the
        policy skips a document that was already finished when it was first read.
        """
        self._on_call.setdefault(tool, []).append(fn)

    def snapshot(self) -> dict[str, list[dict[str, Any]]]:
        return deepcopy(self.rows)

    # -- transport interface ---------------------------------------------

    def call(
        self, method: str, params: dict[str, Any] | None = None, *, retriable: bool = True
    ) -> dict[str, Any]:
        # retriable is part of the transport interface; the fake never fails
        # transiently unless a scenario asks it to, so it is accepted and ignored.
        params = params or {}
        if method == "initialize":
            return {"result": {"protocolVersion": MCP_PROTOCOL}}
        if method == "notifications/initialized":
            return {"result": {}}
        if method == "tools/list":
            return {"result": {"tools": TOOLS}}
        if method != "tools/call":
            return {"error": {"code": -32601, "message": f"no such method {method}"}}

        name = params.get("name", "")
        args = params.get("arguments") or {}
        self.calls.append((name, args))

        for fn in self._on_call.pop(name, []):
            fn(self)

        entity, _, verb = name.partition(".")
        if entity in self.absent:
            return {"error": {"code": -32003, "message": f"App for {entity} is not enabled"}}

        try:
            payload = self._dispatch(entity, verb, args)
        except KeyError as exc:
            return {"result": {"content": [{"type": "text", "text": str(exc)}], "isError": True}}
        return {"result": {"content": [{"type": "text", "text": json.dumps(payload, default=str)}]}}

    def close(self) -> None:
        return None

    # -- entity operations -------------------------------------------------

    def _dispatch(self, entity: str, verb: str, args: dict[str, Any]) -> Any:
        if entity not in self.rows and verb in CRUD:
            raise KeyError(f"unknown entity {entity}")

        if verb == "list":
            rows = self.rows[entity]
            for key, value in args.items():
                if key in ("limit", "offset"):
                    continue
                rows = [r for r in rows if str(r.get(key)) == str(value)]
            offset = int(args.get("offset", 0))
            limit = int(args.get("limit", 20))
            return {"data": rows[offset : offset + limit], "total": len(rows), "offset": offset}

        if verb == "get":
            row = self._find(entity, args["id"])
            return {"data": {**row, "_transitions": []}}

        if verb == "create":
            row = {"id": str(uuid.uuid4()), **args, "created_at": _now().isoformat()}
            self._guard_write()
            self.rows[entity].append(row)
            return {"data": row}

        if verb == "update":
            row = self._find(entity, args["id"])
            self._guard_write()
            row.update({k: v for k, v in args.items() if k != "id"})
            row["updated_at"] = _now().isoformat()
            return {"data": row}

        if verb == "delete":
            self._guard_write()
            self.rows[entity] = [r for r in self.rows[entity] if r["id"] != args["id"]]
            return {"data": {"deleted": args["id"]}}

        if verb == "send_for_signature":
            doc = self._find("EsignDocument", args["id"])
            if doc["status"] != "draft":
                raise KeyError("document is not in a sendable state")
            self._guard_write()
            doc["status"] = "sent"
            doc["updated_at"] = _now().isoformat()
            return {"data": doc}

        if verb == "reissue_signing_link":
            doc = self._find("EsignDocument", args["id"])
            signer = self._find("EsignSigner", args["signer_id"])
            if doc["status"] != "sent":
                raise KeyError("can only reissue on a sent document")
            self._guard_write()
            signer["access_token_hash"] = uuid.uuid4().hex
            signer["updated_at"] = _now().isoformat()
            return {"data": {"link": f"https://example.invalid/sign/{signer['id']}"}}

        raise KeyError(f"no such tool {entity}.{verb}")

    def _find(self, entity: str, row_id: str) -> dict[str, Any]:
        for row in self.rows[entity]:
            if row["id"] == row_id:
                return row
        raise KeyError(f"{entity} {row_id} not found")

    def _guard_write(self) -> None:
        self._writes += 1
        if self.fail_write_on == self._writes:
            self.fail_write_on = None
            raise KeyError("transient write failure (induced)")
