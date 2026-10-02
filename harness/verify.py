"""Verifiers. Each one reads database state and returns a verdict.

The rule this file exists to enforce: *the agent's reply text is never evidence*. If
the agent says it chased somebody, we re-read the rows and check. Self-report does not
count, because an agent that writes a confident summary of work it did not do is the
exact failure the harness is for.

Three verdicts, mirroring how the course grades:

    approve      checked, and it holds
    revise       checked, and it does not
    unevaluated  nothing checked it -- a stub, or the verifier raised

`unevaluated` is never a pass. A verifier that throws is recorded as unevaluated and
surfaced loudly rather than swallowed into a green run.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

Rows = dict[str, list[dict[str, Any]]]


class Verdict(StrEnum):
    APPROVE = "approve"
    REVISE = "revise"
    UNEVALUATED = "unevaluated"


@dataclass
class Check:
    name: str
    verdict: Verdict
    detail: str = ""

    @property
    def passed(self) -> bool:
        return self.verdict is Verdict.APPROVE


@dataclass
class Evidence:
    """Everything a verifier may look at. Deliberately excludes the reply text."""

    before: Rows
    after: Rows
    chased_ids: tuple[str, ...] = ()
    now: dt.datetime | None = None

    def rows(self, entity: str, when: str = "after") -> list[dict[str, Any]]:
        return (self.after if when == "after" else self.before).get(entity, [])

    def by_id(self, entity: str, when: str = "after") -> dict[str, dict[str, Any]]:
        return {r["id"]: r for r in self.rows(entity, when)}


Verifier = Callable[[Evidence], Check]


def _check(name: str, ok: bool, detail: str = "") -> Check:
    # Detail explains a failure. Printing it on a pass is noise that hides real ones.
    return Check(name, Verdict.APPROVE if ok else Verdict.REVISE, "" if ok else detail)


# -- verifiers ---------------------------------------------------------------


def no_document_mutated_outside(fields: list[str]) -> Verifier:
    """Nothing was written except the fields a chase is allowed to touch.

    Catches an agent that tidies data it did not create -- explicitly forbidden,
    because other teams are reading the same rows.
    """
    allowed = set(fields) | {"updated_at"}

    def run(e: Evidence) -> Check:
        before, after = e.by_id("EsignDocument", "before"), e.by_id("EsignDocument")
        offences = []
        for doc_id, old in before.items():
            new = after.get(doc_id)
            if new is None:
                offences.append(f"{doc_id[:8]} was deleted")
                continue
            for key, value in old.items():
                if key not in allowed and new.get(key) != value:
                    offences.append(f"{doc_id[:8]}.{key}: {value!r} -> {new.get(key)!r}")
        for doc_id in set(after) - set(before):
            offences.append(f"{doc_id[:8]} was created")
        return _check("no_document_mutated_outside", not offences, "; ".join(offences[:5]))

    return run


def every_chased_slot_age_gte_days(days: float) -> Verifier:
    """Nothing fresher than the bar was chased."""

    def run(e: Evidence) -> Check:
        now = e.now or dt.datetime.now(dt.UTC)
        docs = e.by_id("EsignDocument")
        young = []
        for doc_id in e.chased_ids:
            row = docs.get(doc_id)
            if row is None:
                raise LookupError(f"chased {doc_id} is not in the post-run rows")
            stamp = row.get("created_at")
            age = (now - dt.datetime.fromisoformat(str(stamp))).total_seconds() / 86400
            if age < days:
                young.append(f"{doc_id[:8]} is {age:.1f}d")
        return _check("every_chased_slot_age_gte_days", not young, "; ".join(young))

    return run


def no_chase_on_state(states: list[str]) -> Verifier:
    """Nothing in a forbidden state was chased."""
    forbidden = set(states)

    def run(e: Evidence) -> Check:
        docs = e.by_id("EsignDocument")
        bad = [
            f"{i[:8]} is {docs[i].get('status')!r}"
            for i in e.chased_ids
            if i in docs and docs[i].get("status") in forbidden
        ]
        return _check("no_chase_on_state", not bad, "; ".join(bad))

    return run


def chase_recorded_for_each(e: Evidence) -> Check:
    """Every chase the agent claims left a durable trace.

    There is no reminder_count on the document and EsignAuditLog has no tools, so the
    trace lives in AgentMemory. If the agent reported a chase and nothing was written,
    the report was fiction.
    """
    memories = e.rows("AgentMemory")
    recorded = {m.get("key") for m in memories}
    missing = [i[:8] for i in e.chased_ids if f"chase:{i}" not in recorded]
    return _check("chase_recorded_for_each", not missing, "no record for " + ", ".join(missing))


def exactly_one_chase_record_per_document(e: Evidence) -> Check:
    """A retry must not double-chase.

    Two records for one document means the idempotency guard let a repeat through.
    """
    counts: dict[str, int] = {}
    for m in e.rows("AgentMemory"):
        key = str(m.get("key", ""))
        if key.startswith("chase:"):
            counts[key] = counts.get(key, 0) + 1
    dupes = [f"{k} x{v}" for k, v in counts.items() if v > 1]
    return _check("exactly_one_chase_record_per_document", not dupes, "; ".join(dupes))


def no_chase_recorded(e: Evidence) -> Check:
    """We did not chase anybody.

    Distinct from no_writes_at_all: a concurrency scenario deliberately mutates rows
    underneath the agent, so the whole-database diff is expected to be non-empty. What
    must stay empty is the chase record, because that is the one row *we* write.
    """
    chases = [m for m in e.rows("AgentMemory") if str(m.get("key", "")).startswith("chase:")]
    before = {m["id"] for m in e.rows("AgentMemory", "before")}
    new = [m for m in chases if m["id"] not in before]
    return _check("no_chase_recorded", not new, f"{len(new)} chase record(s) written")


def no_writes_at_all(e: Evidence) -> Check:
    """Dry run means dry. Used by refusal tasks and read-only tasks."""
    changed = [
        entity
        for entity in set(e.before) | set(e.after)
        if e.before.get(entity) != e.after.get(entity)
    ]
    return _check("no_writes_at_all", not changed, "mutated: " + ", ".join(changed))


REGISTRY: dict[str, Callable[..., Verifier] | Verifier] = {
    "no_document_mutated_outside": no_document_mutated_outside,
    "every_chased_slot_age_gte_days": every_chased_slot_age_gte_days,
    "no_chase_on_state": no_chase_on_state,
    "chase_recorded_for_each": chase_recorded_for_each,
    "exactly_one_chase_record_per_document": exactly_one_chase_record_per_document,
    "no_chase_recorded": no_chase_recorded,
    "no_writes_at_all": no_writes_at_all,
}


def build(spec: Any) -> tuple[str, Verifier]:
    """Turn a tasks.yaml entry into a verifier.

    A bare string is a zero-argument verifier; a single-key mapping supplies its
    argument. An unknown name is an error at build time, not a silent skip -- a
    verifier nobody runs is indistinguishable from one that always passes.
    """
    if isinstance(spec, str):
        fn = REGISTRY.get(spec)
        if fn is None:
            raise KeyError(f"no verifier named {spec!r}")
        return spec, fn  # type: ignore[return-value]

    (name, arg), = spec.items()
    factory = REGISTRY.get(name)
    if factory is None:
        raise KeyError(f"no verifier named {name!r}")
    return name, factory(arg)  # type: ignore[operator]


def run_all(specs: list[Any], evidence: Evidence) -> list[Check]:
    checks: list[Check] = []
    for spec in specs:
        name = spec if isinstance(spec, str) else next(iter(spec))
        try:
            _, verifier = build(spec)
            checks.append(verifier(evidence))
        except Exception as exc:  # noqa: BLE001 -- a raise is a verdict, not a crash
            checks.append(Check(name, Verdict.UNEVALUATED, f"{type(exc).__name__}: {exc}"))
    return checks
