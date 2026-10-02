"""Bounded retry with exponential backoff and jitter.

Shared by the MCP transport and the model client, because both talk to a network and
neither should die on one blip.

Two rules keep this honest:

* **Only transient failures are retried.** A 401, 403 or 400 means the request was
  wrong and will be wrong again; retrying it wastes time and, on a shared platform,
  adds load that other teams feel. Only connection errors, timeouts, 429 and 5xx are
  worth a second attempt.
* **Unsafe writes are not retried.** A write whose schema has no idempotency key could
  land twice -- once from the attempt that appeared to fail, once from the retry. The
  caller says whether a call is safe to repeat; the default is no.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")

MAX_ATTEMPTS = 3
BASE_DELAY = 0.5
MAX_DELAY = 8.0

# Retried. Everything else is treated as a permanent answer.
TRANSIENT_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class Transient(Exception):
    """A failure worth trying again."""


def backoff_delay(attempt: int, rng: random.Random | None = None) -> float:
    """Exponential, with full jitter.

    Jitter matters here specifically: twenty-six teams share this platform, and
    synchronised retries after a blip are how a recovering service gets knocked over
    again.
    """
    rng = rng or random
    ceiling = min(MAX_DELAY, BASE_DELAY * (2**attempt))
    return rng.uniform(0, ceiling)


def with_retry(
    fn: Callable[[], T],
    *,
    retriable: bool = True,
    attempts: int = MAX_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
    on_retry: Callable[[int, Exception], None] | None = None,
) -> T:
    """Call fn, retrying only Transient failures."""
    if not retriable:
        return fn()

    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except Transient as exc:
            last = exc
            if attempt == attempts - 1:
                break
            if on_retry:
                on_retry(attempt + 1, exc)
            sleep(backoff_delay(attempt, rng))
    assert last is not None
    raise last
