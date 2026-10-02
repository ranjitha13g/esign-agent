"""The goal-holding loop. Ours, not a wrapper around the platform's.

The model handles the question, the sequencing and the wording. It does not carry the
judgement -- agent/policy.py does, as pure functions, because a decision about whether
to chase somebody should be testable without a model in the way.

What the loop is really for is holding a goal across several reads, noticing when the
data cannot answer, and saying so.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from typing import Any

from agent.model import ModelClient
from agent.policy import triage
from domain.esign import assemble
from domain.locale import resolve
from mcp.client import McpClient, McpError, ToolNotAvailable

MAX_TURNS = 12
MAX_TOOL_RESULT = 60_000

SYSTEM = """\
You are the e-sign agent for one seat on a shared business platform. You answer \
questions about documents awaiting signature and decide what to do about them.

Rules you do not break:

1. Only state what the tools returned. If the data cannot support an answer, say so \
plainly and stop. Never infer why a person has not signed -- that reason is not in \
the database, and inventing it is worse than saying you do not know.
2. You hold one seat. Entities belonging to other seats are absent from your tools, \
not merely refused. If something you need is out of reach, say which app owns it and \
that a human or an escalation is required.
3. Never claim to have changed anything. You are reporting and recommending. A \
separate executor acts, and only when a person has allowed it.
4. The judgement about whom to chase is already made for you by pending_signatures. \
Do not second-guess its verdicts; explain them.
5. Do not name a business noun the platform did not give you. Read the jurisdiction \
from the tools rather than assuming one.

Be brief and concrete. Lead with the answer.\
"""

TOOLS: list[dict[str, Any]] = [
    {
        "name": "pending_signatures",
        "description": (
            "The full picture: every document, who it waits on, how long it has sat, "
            "and a verdict per document (chase, reissue, stop_chasing, escalate, skip) "
            "with the reason. Start here for anything about what is outstanding."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_document",
        "description": "Full detail for one document, including its signers.",
        "input_schema": {
            "type": "object",
            "properties": {"document_id": {"type": "string"}},
            "required": ["document_id"],
        },
    },
    {
        "name": "read_entity",
        "description": (
            "Read rows from any entity this seat can reach. Returns an explicit "
            "out-of-seat message when the entity belongs to another seat."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entity": {"type": "string", "description": "e.g. EsignTemplate, Party"},
                "limit": {"type": "integer"},
            },
            "required": ["entity"],
        },
    },
]


@dataclass
class Turn:
    role: str
    content: Any


@dataclass
class Result:
    answer: str
    turns: list[Turn] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    refused: bool = False


class Planner:
    def __init__(
        self, mcp: McpClient, model: ModelClient, now: dt.datetime | None = None
    ) -> None:
        self.mcp = mcp
        self.model = model
        # Ageing is measured against this instant, not the wall clock. A cassette
        # recorded on Monday and replayed on Friday must produce the same message
        # bytes, or the model cassette misses and CI fails for a reason that has
        # nothing to do with the change under test.
        self.now = now or dt.datetime.now(dt.UTC)
        self._cache: dict[str, Any] = {}

    # -- the tools the model may call ------------------------------------

    def _pending(self) -> dict[str, Any]:
        locale = resolve(self.mcp.call("Company.list", {}).get("data", []))
        docs = assemble(self.mcp.page("EsignDocument.list"), self.mcp.page("EsignSigner.list"))
        by_id = {d.id: d for d in docs}
        decisions = triage(docs, chase_history={}, now=self.now)
        return {
            "as_of": self.now.isoformat(),
            "jurisdiction": {"country": locale.country, "currency": locale.currency},
            "document_count": len(docs),
            "verdicts": [
                {
                    "document_id": d.document_id,
                    "title": by_id[d.document_id].title,
                    "status": by_id[d.document_id].status,
                    "verdict": d.action.value,
                    "reason": d.reason,
                    "age_days": round(d.age_days, 1) if d.age_days is not None else None,
                    "waiting_on": [
                        {"name": s.full_name, "email": s.email, "state": s.status}
                        for s in d.targets
                    ],
                }
                for d in decisions
            ],
        }

    def _get_document(self, document_id: str) -> Any:
        doc = self.mcp.call("EsignDocument.get", {"id": document_id})
        signers = self.mcp.call("EsignSigner.list", {"esign_document_id": document_id})
        return {"document": doc.get("data", doc), "signers": signers.get("data", [])}

    def _read_entity(self, entity: str, limit: int = 20) -> Any:
        try:
            return self.mcp.call(f"{entity}.list", {"limit": limit})
        except ToolNotAvailable:
            return {
                "out_of_seat": True,
                "detail": (
                    f"{entity} is not reachable from this seat. It belongs to another "
                    "app, and crossing that boundary needs an admin or a human."
                ),
            }
        except McpError as exc:
            return {"error": str(exc)}

    def _dispatch(self, name: str, args: dict[str, Any]) -> Any:
        if name == "pending_signatures":
            if "pending" not in self._cache:
                self._cache["pending"] = self._pending()
            return self._cache["pending"]
        if name == "get_document":
            return self._get_document(args["document_id"])
        if name == "read_entity":
            return self._read_entity(args["entity"], int(args.get("limit", 20)))
        return {"error": f"no such tool: {name}"}

    @staticmethod
    def _encode(payload: Any, result: Result) -> str:
        """Serialise a tool result, shrinking it audibly if it will not fit.

        Slicing the JSON string would hand the model a half-object that still reads
        like data: it would answer confidently from a truncated verdict list and never
        know it was short. So this never emits invalid JSON. It drops rows -- the
        skipped ones first, since nobody acts on those -- until the payload fits, says
        in-band how many it dropped, and records a warning on the run.
        """
        blob = json.dumps(payload, default=str)
        if len(blob) <= MAX_TOOL_RESULT:
            return blob

        if isinstance(payload, dict) and isinstance(payload.get("verdicts"), list):
            verdicts = payload["verdicts"]
            rest = {k: v for k, v in payload.items() if k != "verdicts"}
            keep = ("chase", "reissue", "escalate")
            # Actionable rows first; skips are summarised by the counts already in rest.
            ranked = [v for v in verdicts if v.get("verdict") in keep]

            n = len(ranked)
            while n >= 0:
                reduced = {
                    **rest,
                    "verdicts": ranked[:n],
                    "TRUNCATED": (
                        f"Showing {n} of {len(verdicts)} documents, actionable ones "
                        "first. Counts above are complete; do not infer totals from "
                        "this list, and say it was truncated if you summarise it."
                    ),
                }
                blob = json.dumps(reduced, default=str)
                if len(blob) <= MAX_TOOL_RESULT:
                    result.warnings.append(
                        f"tool result reduced from {len(verdicts)} to {n} verdicts"
                    )
                    return blob
                n = n // 2 if n > 1 else 0

        # Last resort, still valid JSON: say nothing rather than say half a thing.
        result.warnings.append("tool result too large to represent; sent a stub")
        return json.dumps(
            {
                "TRUNCATED": "This result was too large to send. Ask for a narrower slice.",
                "size_chars": len(blob),
            }
        )

    # -- the loop --------------------------------------------------------

    def ask(self, question: str) -> Result:
        messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
        result = Result(answer="")

        for _ in range(MAX_TURNS):
            response = self.model.complete(SYSTEM, messages, TOOLS)
            blocks = response.get("content", [])
            messages.append({"role": "assistant", "content": blocks})

            uses = [b for b in blocks if b.get("type") == "tool_use"]
            if not uses:
                result.answer = "".join(
                    b.get("text", "") for b in blocks if b.get("type") == "text"
                ).strip()
                result.turns = [Turn(m["role"], m["content"]) for m in messages]
                return result

            outputs = []
            for use in uses:
                payload = self._dispatch(use["name"], use.get("input") or {})
                result.tool_calls.append({"name": use["name"], "input": use.get("input")})
                outputs.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": use["id"],
                        "content": self._encode(payload, result),
                    }
                )
            messages.append({"role": "user", "content": outputs})

        result.answer = f"Gave up after {MAX_TURNS} turns without reaching an answer."
        result.turns = [Turn(m["role"], m["content"]) for m in messages]
        return result
