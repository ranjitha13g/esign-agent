"""Transport behaviour: keying, recording, and ordered replay."""

from __future__ import annotations

import json

import pytest

from mcp.transport import (
    RecordingTransport,
    ReplayTransport,
    UnrecordedInteraction,
    interaction_key,
)


class StubTransport:
    """Stands in for the live platform. Returns queued bodies in order."""

    def __init__(self, bodies):
        self._bodies = list(bodies)
        self.calls = []

    def call(self, method, params=None, *, retriable=True):
        self.calls.append((method, params))
        return self._bodies.pop(0)

    def close(self):
        pass


def test_key_ignores_argument_ordering():
    a = interaction_key("tools/call", {"name": "EsignDocument.list", "arguments": {"x": 1, "y": 2}})
    b = interaction_key("tools/call", {"name": "EsignDocument.list", "arguments": {"y": 2, "x": 1}})
    assert a == b


def test_key_separates_different_tools():
    a = interaction_key("tools/call", {"name": "EsignDocument.list", "arguments": {}})
    b = interaction_key("tools/call", {"name": "EsignDocument.get", "arguments": {}})
    assert a != b


def test_recording_writes_one_line_per_call(tmp_path):
    cassette = tmp_path / "run.jsonl"
    inner = StubTransport([{"result": {"ok": 1}}, {"result": {"ok": 2}}])
    t = RecordingTransport(inner, cassette)

    t.call("tools/call", {"name": "EsignDocument.list", "arguments": {}})
    t.call("tools/call", {"name": "EsignDocument.get", "arguments": {"id": "abc"}})

    lines = [json.loads(ln) for ln in cassette.read_text(encoding="utf-8").splitlines() if ln]
    assert len(lines) == 2
    assert lines[0]["response"] == {"result": {"ok": 1}}
    assert lines[1]["params"]["arguments"] == {"id": "abc"}


def test_replay_returns_recorded_response(tmp_path):
    cassette = tmp_path / "run.jsonl"
    inner = StubTransport([{"result": {"documents": []}}])
    rec = RecordingTransport(inner, cassette)
    rec.call("tools/call", {"name": "EsignDocument.list", "arguments": {}})

    replay = ReplayTransport(cassette)
    got = replay.call("tools/call", {"name": "EsignDocument.list", "arguments": {}})
    assert got == {"result": {"documents": []}}


def test_repeated_identical_calls_replay_in_order(tmp_path):
    """The re-read-before-write case.

    The agent reads a document, another team writes to it, the agent reads again. Same
    call, different answers. Replay must not collapse these onto one response or the
    concurrency task would pass against a cassette that proves nothing.
    """
    cassette = tmp_path / "run.jsonl"
    inner = StubTransport(
        [
            {"result": {"status": "sent"}},
            {"result": {"status": "completed"}},
        ]
    )
    rec = RecordingTransport(inner, cassette)
    args = {"name": "EsignDocument.get", "arguments": {"id": "doc-1"}}
    rec.call("tools/call", args)
    rec.call("tools/call", args)

    replay = ReplayTransport(cassette)
    assert replay.call("tools/call", args)["result"]["status"] == "sent"
    assert replay.call("tools/call", args)["result"]["status"] == "completed"


def test_replay_raises_on_unrecorded_call(tmp_path):
    cassette = tmp_path / "run.jsonl"
    cassette.write_text("", encoding="utf-8")

    replay = ReplayTransport(cassette)
    with pytest.raises(UnrecordedInteraction) as exc:
        replay.call("tools/call", {"name": "EsignDocument.void", "arguments": {}})
    assert "re-record" in str(exc.value).lower()


def test_replay_exhausts_queue_rather_than_repeating(tmp_path):
    cassette = tmp_path / "run.jsonl"
    inner = StubTransport([{"result": 1}])
    rec = RecordingTransport(inner, cassette)
    rec.call("tools/list", {})

    replay = ReplayTransport(cassette)
    replay.call("tools/list", {})
    with pytest.raises(UnrecordedInteraction):
        replay.call("tools/list", {})


def test_missing_cassette_is_an_explicit_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        ReplayTransport(tmp_path / "nope.jsonl")
