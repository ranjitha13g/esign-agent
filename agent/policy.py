"""What to do about each document, and why.

Pure functions: state in, decision out, no I/O. That is deliberate. This is the half
of the agent that carries the judgement, and pure functions can be tested against
constructed cases -- a signer gone inactive, a fourth chase, a document voided mid-run
-- that do not exist in the live book and cannot be seeded there, because created_at
is server-stamped.

The policy decides. A separate executor acts. Nothing here sends anything.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import Enum

from domain.esign import Document, Signer

# "Everything sitting over a week" from the graded request.
STALE_AFTER_DAYS = 7.0

# Past this, another nudge is noise and the document needs a different intervention.
MAX_CHASES = 3


class Action(Enum):
    CHASE = "chase"
    REISSUE = "reissue"
    STOP_CHASING = "stop_chasing"
    SKIP = "skip"
    ESCALATE = "escalate"


@dataclass(frozen=True)
class Decision:
    document_id: str
    action: Action
    reason: str
    targets: tuple[Signer, ...] = ()
    age_days: float | None = None
    detail: dict[str, object] = field(default_factory=dict)

    @property
    def is_actionable(self) -> bool:
        return self.action in (Action.CHASE, Action.REISSUE)


def decide(
    doc: Document,
    *,
    chases_so_far: int = 0,
    now: dt.datetime | None = None,
    stale_after_days: float = STALE_AFTER_DAYS,
    max_chases: int = MAX_CHASES,
) -> Decision:
    """One document in, one decision out.

    Order matters. The cheapest and most certain disqualifications come first, so that
    a terminal document is never chased on the strength of a later rule.
    """
    now = now or dt.datetime.now(dt.UTC)
    age = doc.age_days(now)

    def out(action: Action, reason: str, targets: tuple[Signer, ...] = ()) -> Decision:
        return Decision(doc.id, action, reason, targets, age)

    if doc.replaced_by:
        return out(Action.SKIP, "superseded by a replacement document")

    # 1. Never sent. A draft has no one waiting on it, whatever its age.
    if doc.is_unsent:
        return out(
            Action.SKIP,
            "still unsent, so nobody has been asked yet"
            + (f" (it does hold {len(doc.signers)} signer rows)" if doc.signers else ""),
        )

    # 2. Anything not positively known to be chaseable is left alone. This covers the
    #    terminal states AND any state the flow has that we have never seen, because
    #    EsignDocumentFlow does not expose its states (see domain/esign.py). Failing
    #    closed here is the whole point: an unknown state must not be chased.
    if not doc.is_chaseable:
        if doc.looks_terminal:
            return out(Action.SKIP, f"state is {doc.status!r}, which is terminal")
        return out(
            Action.SKIP,
            f"state is {doc.status!r}, which is not a confirmed chaseable state",
        )

    # 3. Sent, but there is nobody to chase. Not a pass, and not silence either --
    #    a sent record with no addressee is a real defect in the data.
    if not doc.signers:
        return out(
            Action.ESCALATE,
            "sent, but carries no signer rows, so there is no one to chase",
        )

    # 4. Everyone has acted.
    if doc.is_fully_signed:
        return out(Action.SKIP, "every signer has signed")

    # 5. Expiry beats chasing: asking someone to act on a lapsed request wastes
    #    their time and ours.
    if doc.is_expired(now):
        return out(Action.STOP_CHASING, "past its expiry date; void or reissue instead")

    # 6. Not yet stale. The request says over a week.
    if age is None:
        return out(Action.SKIP, "no usable timestamp, so age cannot be established")
    if age < stale_after_days:
        return out(Action.SKIP, f"only {age:.1f} days old, under the {stale_after_days:g} day bar")

    targets = doc.next_to_act()
    if not targets:
        return out(Action.SKIP, "nobody is currently blocking")

    # 7. Already chased enough. A fourth nudge to the same address is not a plan.
    if chases_so_far >= max_chases:
        return out(
            Action.STOP_CHASING,
            f"already chased {chases_so_far} times; escalate or reissue rather than nudge again",
            targets,
        )

    # 8. Their link has expired. A reminder would point at a dead page, so the link
    #    has to be rotated whether or not they ever opened the old one. This outranks
    #    the chase/reissue split below: there is nothing to chase them towards.
    if all(s.link_is_dead(now) for s in targets):
        return out(
            Action.REISSUE,
            f"stale {age:.0f} days and every signing link has expired; "
            "a reminder would point at a dead link",
            targets,
        )

    # 9. Chased before and still never opened: the message is not getting through,
    #    so rotate the link. Deliberately not the opening move -- reissuing
    #    invalidates the link the signer already holds, and on a first contact we
    #    have no evidence it failed. Ask once, then escalate the mechanism.
    if chases_so_far >= 1 and all(not s.has_opened for s in targets):
        return out(
            Action.REISSUE,
            f"chased {chases_so_far}x, still never opened after {age:.0f} days; "
            "rotate the signing link",
            targets,
        )

    return out(Action.CHASE, f"stale {age:.0f} days and still outstanding", targets)


def triage(
    docs: list[Document],
    *,
    chase_history: dict[str, int] | None = None,
    now: dt.datetime | None = None,
) -> list[Decision]:
    """Decide across the whole book, worst first.

    Ordering is by age so that the most overdue surfaces first, which is what the
    person asking the question actually wants to see.
    """
    history = chase_history or {}
    decisions = [decide(d, chases_so_far=history.get(d.id, 0), now=now) for d in docs]
    return sorted(
        decisions,
        key=lambda d: (not d.is_actionable, -(d.age_days or 0.0)),
    )
