"""Replay a recorded planner run, with no network and no model key.

Scope, stated plainly: this is a **planner** regression test, not a verifier. It reads
the agent's reply, which the brief rightly says is not evidence of anything. It earns
its place by catching changes in how the agent talks and which tools it reaches for --
neither of which the harness covers.

State verification lives in harness/verify.py, where every check reads rows and none
reads prose. If you want to know whether the agent did the right thing, look there.
What this file answers is whether it still *says* the right thing.

A failure here means one of two things, and the error says which:
  UnrecordedInteraction  -- the agent called the platform differently
  UnrecordedCompletion   -- the agent prompted the model differently
Both are real signals. Re-record with: uv run python -m agent.ask --record <name>

DRAFT -- re-author by hand before submission.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from agent.model import ReplayModel
from agent.planner import Planner
from mcp.client import McpClient
from mcp.transport import ReplayTransport

ROOT = Path(__file__).resolve().parent.parent
CASSETTES = ROOT / "cassettes"
NAME = "chase-stale-week"

pytestmark = pytest.mark.skipif(
    not (CASSETTES / f"{NAME}.model.jsonl").exists(),
    reason=f"no cassette recorded for {NAME}",
)


@pytest.fixture(scope="module")
def replayed():
    meta = json.loads((CASSETTES / f"{NAME}.meta.json").read_text(encoding="utf-8"))
    client = McpClient(ReplayTransport(CASSETTES / f"{NAME}.mcp.jsonl"))
    client.connect()
    model = ReplayModel(CASSETTES / f"{NAME}.model.jsonl")
    planner = Planner(client, model, now=dt.datetime.fromisoformat(meta["now"]))
    return meta, planner.ask(meta["question"])


def test_the_run_reaches_an_answer(replayed):
    _, result = replayed
    assert result.answer
    assert "Gave up" not in result.answer


def test_the_agent_consulted_the_policy_rather_than_improvising(replayed):
    _, result = replayed
    assert "pending_signatures" in [c["name"] for c in result.tool_calls]


def test_it_does_not_claim_to_have_sent_anything(replayed):
    """The planner holds no tool that writes. An answer implying otherwise would be
    a false report of a state change, which is worse than no answer."""
    _, result = replayed
    lowered = result.answer.lower()
    for claim in ("i have sent", "i sent", "reminder sent", "i have chased", "i chased"):
        assert claim not in lowered, f"claimed an action it cannot perform: {claim!r}"


def test_it_surfaces_the_documents_that_need_a_human(replayed):
    """Five documents are sent with no signers. Reporting a clean nil return would be
    true and useless; the agent has to say the data is broken."""
    _, result = replayed
    assert "escalate" in result.answer.lower()


def test_the_planner_never_reached_for_a_write_tool(replayed):
    """Not a prose check: the planner exposes no writing tool, and this asserts it
    stayed that way. A planner that can write is a planner that can act without the
    executor's dry-run interlock."""
    _, result = replayed
    reads = {"pending_signatures", "get_document", "read_entity"}
    assert {c["name"] for c in result.tool_calls} <= reads
