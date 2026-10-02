"""Transport for POST /api/mcp, in three flavours that share one interface.

    HttpTransport       talks to the live platform
    RecordingTransport  wraps another transport and writes a cassette
    ReplayTransport     serves a cassette and refuses to touch the network

The split exists so CI can run the whole harness without credentials and without
writing to a database twenty-six other teams are using.

Ordering matters more than it looks. The same call can legitimately return different
answers within one run -- re-reading a document after another team wrote to it is the
behaviour the concurrency task exists to prove. So a cassette is an ordered log, and
replay consumes repeated calls in the order they were recorded rather than collapsing
them onto one response.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Protocol

import httpx

from .retry import TRANSIENT_STATUS, Transient, with_retry

MCP_PROTOCOL = "2025-11-25"


class UnrecordedInteraction(RuntimeError):
    """Replay hit a call the cassette does not contain.

    Not a nuisance -- a signal. It means the agent did something different from the
    recorded run, which is exactly what a regression looks like.
    """


def interaction_key(method: str, params: dict[str, Any] | None) -> str:
    """Stable key for one JSON-RPC call.

    Keyed on the method plus, for tools/call, the tool name and its arguments. Arguments
    are canonicalised (sorted keys) so formatting differences do not produce a miss.
    """
    params = params or {}
    if method == "tools/call":
        ident: Any = {
            "name": params.get("name"),
            "arguments": params.get("arguments") or {},
        }
    else:
        ident = params
    blob = json.dumps({"method": method, "ident": ident}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


class Transport(Protocol):
    """Returns the raw JSON-RPC response body. Unwrapping is the client's job."""

    def call(
        self, method: str, params: dict[str, Any] | None = None, *, retriable: bool = True
    ) -> dict[str, Any]: ...

    def close(self) -> None: ...


class HttpTransport:
    """The live platform."""

    def __init__(self, base_url: str, token: str, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": "Bearer " + token},
            timeout=timeout,
        )
        self._id = 0
        # Retries that happened, for the run record. Silent recovery still hides a
        # platform that is struggling.
        self.retries: list[str] = []

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        retriable: bool = True,
    ) -> dict[str, Any]:
        """One JSON-RPC call, retried on transient failure only.

        `retriable=False` for writes the platform cannot deduplicate: a retry there
        could land the same write twice.
        """
        return with_retry(
            lambda: self._once(method, params),
            retriable=retriable,
            on_retry=self._note_retry,
        )

    def _note_retry(self, attempt: int, exc: Exception) -> None:
        self.retries.append(f"attempt {attempt}: {exc}")

    def _once(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        try:
            r = self._http.post("/api/mcp", json=payload)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            # Never reached the server, or never heard back. Worth another go.
            raise Transient(f"{type(exc).__name__}: {exc}") from exc

        # Only authentication answers at the HTTP layer. Everything else -- including
        # every denial -- comes back 200 with the failure inside the envelope, so a
        # client that treats non-200 as the only failure reads refusals as successes.
        if r.status_code == 401:
            raise PermissionError("401 from /api/mcp -- token missing, expired or rejected")
        if r.status_code == 405:
            raise RuntimeError(
                "405 from /api/mcp -- this endpoint is POST only, there is no stream"
            )
        if r.status_code in TRANSIENT_STATUS:
            raise Transient(f"HTTP {r.status_code} from /api/mcp")

        try:
            return r.json()
        except ValueError as exc:
            raise RuntimeError(
                f"non-JSON response from /api/mcp (HTTP {r.status_code}): {r.text[:500]}"
            ) from exc

    def close(self) -> None:
        self._http.close()


class RecordingTransport:
    """Wraps a live transport and appends every exchange to a cassette."""

    def __init__(self, inner: Transport, cassette: Path) -> None:
        self._inner = inner
        self._path = Path(cassette)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Start a fresh recording rather than appending to a stale one.
        self._path.write_text("", encoding="utf-8")

    def call(
        self, method: str, params: dict[str, Any] | None = None, *, retriable: bool = True
    ) -> dict[str, Any]:
        response = self._inner.call(method, params, retriable=retriable)
        entry = {
            "key": interaction_key(method, params),
            "method": method,
            "params": params or {},
            "response": response,
        }
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True, default=str) + "\n")
        return response

    def close(self) -> None:
        self._inner.close()


class ReplayTransport:
    """Serves a cassette. Never touches the network."""

    def __init__(self, cassette: Path) -> None:
        self._path = Path(cassette)
        if not self._path.exists():
            raise FileNotFoundError(f"no cassette at {self._path} -- record one first")

        self._queues: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
        self._log: list[dict[str, Any]] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            self._queues[entry["key"]].append(entry["response"])
            self._log.append(entry)

    def call(
        self, method: str, params: dict[str, Any] | None = None, *, retriable: bool = True
    ) -> dict[str, Any]:
        key = interaction_key(method, params)
        queue = self._queues.get(key)
        if not queue:
            raise UnrecordedInteraction(
                "no recorded response for {} {} in {}.\n"
                "The agent diverged from the recorded run, or the cassette is stale. "
                "Re-record with --record.".format(
                    method,
                    (params or {}).get("name", ""),
                    self._path.name,
                )
            )
        return queue.popleft()

    @property
    def interactions(self) -> list[dict[str, Any]]:
        return list(self._log)

    def close(self) -> None:
        return None
