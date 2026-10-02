"""The model side of the loop, in the same three flavours as the MCP transport.

    AnthropicClient   calls the real API
    RecordingModel    wraps it and writes a cassette
    ReplayModel       serves a cassette, never calls out

Recording both sides is what makes CI honest. If only the MCP calls replayed, every
pull request would still hit the model: it would cost money, and a red build could not
be told apart from a differently-worded completion. With both recorded, CI needs no
ANTHROPIC_API_KEY and a replay failure always means behaviour changed.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Protocol

from mcp.retry import Transient, with_retry

# Judgement-heavy work, so the default is the most capable model. Override with
# ESIGN_MODEL when iterating, where a cheaper model is usually enough.
DEFAULT_MODEL = os.environ.get("ESIGN_MODEL", "claude-opus-5-5")
MAX_TOKENS = 4096


class UnrecordedCompletion(RuntimeError):
    """Replay hit a model request the cassette does not hold."""


def request_key(system: str, messages: list[dict[str, Any]], tools: list[dict]) -> str:
    blob = json.dumps(
        {"system": system, "messages": messages, "tools": [t.get("name") for t in tools]},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


class ModelClient(Protocol):
    def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict]
    ) -> dict[str, Any]: ...


def _serialise(response: Any) -> dict[str, Any]:
    """Reduce an SDK response to the plain dict the loop actually uses."""
    return {
        "stop_reason": response.stop_reason,
        "content": [
            block.model_dump() if hasattr(block, "model_dump") else dict(block)
            for block in response.content
        ],
    }


class AnthropicClient:
    def __init__(self, api_key: str | None = None, model: str = DEFAULT_MODEL) -> None:
        from anthropic import Anthropic

        self.model = model
        self._c = Anthropic(api_key=api_key or os.environ["ANTHROPIC_API_KEY"])

    def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict]
    ) -> dict[str, Any]:
        """Completions are read-only, so retrying one is always safe.

        Rate limits and overloads are the common case here and both are transient;
        crashing a multi-step run on one is needless.
        """
        return with_retry(lambda: self._once(system, messages, tools))

    def _once(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict]
    ) -> dict[str, Any]:
        import anthropic

        try:
            response = self._c.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=messages,
                tools=tools,
            )
        except (
            anthropic.RateLimitError,
            anthropic.APITimeoutError,
            anthropic.APIConnectionError,
            anthropic.InternalServerError,
        ) as exc:
            raise Transient(f"{type(exc).__name__}: {exc}") from exc
        return _serialise(response)


class RecordingModel:
    def __init__(self, inner: ModelClient, cassette: Path) -> None:
        self._inner = inner
        self._path = Path(cassette)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text("", encoding="utf-8")

    def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict]
    ) -> dict[str, Any]:
        response = self._inner.complete(system, messages, tools)
        entry = {"key": request_key(system, messages, tools), "response": response}
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True, default=str) + "\n")
        return response


class ReplayModel:
    def __init__(self, cassette: Path) -> None:
        self._path = Path(cassette)
        if not self._path.exists():
            raise FileNotFoundError(f"no model cassette at {self._path} -- record one first")
        self._queues: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entry = json.loads(line)
                self._queues[entry["key"]].append(entry["response"])

    def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict]
    ) -> dict[str, Any]:
        queue = self._queues.get(request_key(system, messages, tools))
        if not queue:
            raise UnrecordedCompletion(
                f"no recorded completion at turn {len(messages)} in {self._path.name}. "
                "The agent took a different path -- re-record with --record."
            )
        return queue.popleft()
