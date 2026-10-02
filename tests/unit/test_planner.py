"""The loop: tool dispatch, seat boundaries, and replay determinism.

DRAFT -- re-author by hand before submission.
"""

from __future__ import annotations

import datetime as dt
import json

from agent.planner import Planner
from mcp.client import ToolNotAvailable

FROZEN = dt.datetime(2026, 10, 2, 12, 0, tzinfo=dt.UTC)
SENT_AT = (FROZEN - dt.timedelta(days=19)).isoformat()


class FakeMcp:
    """Stands in for the platform. Mirrors the real response shapes."""

    def __init__(self, absent=("Contract",)):
        self.absent = set(absent)
        self.calls = []

    def page(self, name, page_size=100, **filters):
        """Mirror McpClient.page so the stub stays faithful to the real client."""
        return self.call(name, {"limit": page_size, **filters}).get("data", [])

    def call(self, name, arguments=None):
        self.calls.append((name, arguments))
        entity = name.split(".")[0]
        if entity in self.absent:
            raise ToolNotAvailable(f"{name} is not in this seat's catalogue")
        if name == "Company.list":
            return {"data": [{"id": "c1", "name": "Acme", "country": "India",
                              "default_currency": "INR", "active_domains": []}]}
        if name == "EsignDocument.list":
            return {"data": [{"id": "d1", "title": "Agreement", "status": "sent",
                              "signing_order": "parallel", "created_at": SENT_AT,
                              "updated_at": SENT_AT}], "total": 1}
        if name == "EsignSigner.list":
            return {"data": [{"id": "s1", "esign_document_id": "d1", "email": "a@x.com",
                              "full_name": "A", "sign_order": 1, "status": "viewed",
                              "last_opened_at": SENT_AT, "auth_method": "email_link"}],
                    "total": 1}
        if name == "EsignDocument.get":
            return {"data": {"id": "d1", "title": "Agreement", "status": "sent"}}
        return {"data": [], "total": 0}


class ScriptedModel:
    """Returns canned completions and records what it was asked."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    def complete(self, system, messages, tools):
        # Deep-copy via JSON: the loop mutates its message list in place.
        snapshot = json.loads(json.dumps(messages, default=str))
        self.requests.append({"system": system, "messages": snapshot, "tools": tools})
        return self._responses.pop(0)


def text(s):
    return {"stop_reason": "end_turn", "content": [{"type": "text", "text": s}]}


def use(name, inp=None, _id="u1"):
    return {
        "stop_reason": "tool_use",
        "content": [{"type": "tool_use", "id": _id, "name": name, "input": inp or {}}],
    }


def test_a_plain_answer_ends_the_loop():
    p = Planner(FakeMcp(), ScriptedModel([text("done")]), now=FROZEN)
    assert p.ask("hello").answer == "done"


def test_tool_use_is_dispatched_and_fed_back():
    model = ScriptedModel([use("pending_signatures"), text("one document is stale")])
    r = Planner(FakeMcp(), model, now=FROZEN).ask("what is pending?")
    assert r.answer == "one document is stale"
    assert [c["name"] for c in r.tool_calls] == ["pending_signatures"]
    # The tool result was handed back to the model as a user turn.
    assert model.requests[1]["messages"][-1]["content"][0]["type"] == "tool_result"


def test_pending_signatures_carries_the_verdict_and_who_is_waiting():
    model = ScriptedModel([use("pending_signatures"), text("ok")])
    Planner(FakeMcp(), model, now=FROZEN).ask("q")
    payload = json.loads(model.requests[1]["messages"][-1]["content"][0]["content"])
    verdict = payload["verdicts"][0]
    assert verdict["verdict"] == "chase"
    assert verdict["age_days"] == 19.0
    assert verdict["waiting_on"][0]["email"] == "a@x.com"


def test_an_out_of_seat_entity_is_reported_not_raised():
    """The model must be told the boundary exists so it can name the escalation,
    rather than the loop dying on an exception."""
    model = ScriptedModel([use("read_entity", {"entity": "Contract"}), text("ask the owner")])
    Planner(FakeMcp(), model, now=FROZEN).ask("show me the terms")
    payload = json.loads(model.requests[1]["messages"][-1]["content"][0]["content"])
    assert payload["out_of_seat"] is True
    assert "another" in payload["detail"]


def test_the_clock_is_injected_not_read_from_the_wall():
    """A cassette recorded on Monday and replayed on Friday must produce identical
    message bytes. If ageing came from the wall clock it would not."""
    a = ScriptedModel([use("pending_signatures"), text("x")])
    b = ScriptedModel([use("pending_signatures"), text("x")])
    Planner(FakeMcp(), a, now=FROZEN).ask("q")
    Planner(FakeMcp(), b, now=FROZEN).ask("q")
    assert a.requests[1]["messages"] == b.requests[1]["messages"]


def test_a_different_clock_changes_the_payload():
    """Guards the test above against passing vacuously."""
    a = ScriptedModel([use("pending_signatures"), text("x")])
    b = ScriptedModel([use("pending_signatures"), text("x")])
    Planner(FakeMcp(), a, now=FROZEN).ask("q")
    Planner(FakeMcp(), b, now=FROZEN + dt.timedelta(days=30)).ask("q")
    assert a.requests[1]["messages"] != b.requests[1]["messages"]


def test_the_loop_gives_up_rather_than_spinning():
    model = ScriptedModel([use("pending_signatures", _id=f"u{i}") for i in range(20)])
    r = Planner(FakeMcp(), model, now=FROZEN).ask("q")
    assert "Gave up" in r.answer


def test_pending_signatures_is_computed_once_per_run():
    """It pages the whole book; recomputing it per call would multiply the reads."""
    mcp = FakeMcp()
    model = ScriptedModel([use("pending_signatures", _id="a"),
                           use("pending_signatures", _id="b"), text("x")])
    Planner(mcp, model, now=FROZEN).ask("q")
    assert [n for n, _ in mcp.calls].count("EsignDocument.list") == 1
