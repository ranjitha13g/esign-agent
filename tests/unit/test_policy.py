"""The judgement layer, against cases the live book does not contain.

Suryodaya holds 95 drafts and 5 sent documents with no signers at all, so none of the
decisions that matter can be exercised against it -- and created_at is server-stamped,
so the missing cases cannot be seeded there either. They are constructed here.

DRAFT -- re-author by hand before submission.
"""

from __future__ import annotations

import datetime as dt

from agent.policy import Action, decide, triage
from domain.esign import Document, Signer

NOW = dt.datetime(2026, 10, 2, 12, 0, tzinfo=dt.UTC)


def signer(order=1, status="pending", opened=None, email="a@example.com"):
    return Signer(
        id=f"s{order}",
        document_id="d1",
        email=email,
        full_name=f"Signer {order}",
        sign_order=float(order),
        status=status,
        last_opened_at=opened,
        auth_method="email_link",
    )


def doc(status="sent", days_old=10, signers=(), order="parallel", **kw):
    return Document(
        id=kw.pop("id", "d1"),
        title="t",
        status=status,
        signing_order=order,
        current_signer_order=kw.pop("current_signer_order", None),
        expires_at=kw.pop("expires_at", None),
        created_at=NOW - dt.timedelta(days=days_old),
        updated_at=NOW - dt.timedelta(days=days_old),
        replaced_by=kw.pop("replaced_by", ""),
        signers=tuple(signers),
    )


# -- never chase these --------------------------------------------------------


def test_a_voided_document_is_never_chased():
    d = decide(doc(status="voided", signers=[signer()]), now=NOW)
    assert d.action is Action.SKIP
    assert "terminal" in d.reason


def test_a_declined_document_is_never_chased():
    assert decide(doc(status="declined", signers=[signer()]), now=NOW).action is Action.SKIP


def test_a_superseded_document_is_never_chased():
    """void-and-reissue is modelled natively, so the old copy must be left alone."""
    d = decide(doc(replaced_by="d2", signers=[signer()]), now=NOW)
    assert d.action is Action.SKIP
    assert "superseded" in d.reason


def test_an_unknown_state_is_not_chased():
    """EsignDocumentFlow does not expose its states, so the set of terminal names is
    not knowable. A policy that guessed would chase anything it failed to guess. This
    asserts the inverse: only a confirmed-chaseable state is ever acted on."""
    d = decide(doc(status="under_legal_review", signers=[signer()]), now=NOW)
    assert d.action is Action.SKIP
    assert "not a confirmed chaseable state" in d.reason


def test_a_draft_is_never_chased_however_old():
    d = decide(doc(status="draft", days_old=400, signers=[signer()]), now=NOW)
    assert d.action is Action.SKIP
    assert "unsent" in d.reason


def test_a_document_in_state_viewed_is_still_chaseable():
    """Observed on Keystone: five documents sit in state 'viewed' and every one of
    them still holds outstanding signers. Treating 'sent' as the only live state
    would silently skip all five."""
    d = decide(
        doc(status="viewed", signers=[signer(1, "pending", opened=NOW)]),
        chases_so_far=0,
        now=NOW,
    )
    assert d.action is Action.CHASE


def test_terminal_documents_are_skipped_even_holding_outstanding_signers():
    """The trap this guards: on Keystone, expired/declined/voided documents hold 49
    outstanding signers between them. An agent that chased 'anyone not yet signed'
    would chase all of them."""
    for status in ("expired", "declined", "voided"):
        d = decide(doc(status=status, days_old=40, signers=[signer(1, "pending")]), now=NOW)
        assert d.action is Action.SKIP, status
        assert "terminal" in d.reason


def test_a_fully_signed_document_is_not_chased():
    d = decide(doc(signers=[signer(1, "signed"), signer(2, "signed")]), now=NOW)
    assert d.action is Action.SKIP


def test_fresh_documents_are_left_alone():
    """The request says over a week. Six days is not over a week."""
    d = decide(doc(days_old=6, signers=[signer(opened=NOW)]), now=NOW)
    assert d.action is Action.SKIP
    assert "under the 7 day bar" in d.reason


# -- the live data's own shape ------------------------------------------------


def test_sent_with_no_signers_escalates_rather_than_passing_silently():
    """All five sent documents on Suryodaya look like this. Reporting 'nothing
    pending' would be true and useless; this is a defect someone must fix."""
    d = decide(doc(signers=[]), now=NOW)
    assert d.action is Action.ESCALATE
    assert "no signer rows" in d.reason


# -- who to chase -------------------------------------------------------------


def test_sequential_routing_chases_only_the_current_position():
    """Chasing signer 2 before signer 1 has signed asks them to act out of turn."""
    d = decide(
        doc(
            order="sequential",
            current_signer_order=1.0,
            signers=[signer(1, "viewed", opened=NOW), signer(2, "pending")],
        ),
        now=NOW,
    )
    assert d.action is Action.CHASE
    assert [s.sign_order for s in d.targets] == [1.0]


def test_parallel_routing_chases_everyone_outstanding():
    d = decide(
        doc(
            order="parallel",
            signers=[
                signer(1, "viewed", opened=NOW, email="a@x.com"),
                signer(2, "viewed", opened=NOW, email="b@x.com"),
                signer(3, "signed", email="c@x.com"),
            ],
        ),
        now=NOW,
    )
    assert d.action is Action.CHASE
    assert {s.email for s in d.targets} == {"a@x.com", "b@x.com"}


def test_a_first_contact_is_a_chase_not_a_reissue():
    """Reissuing invalidates the link the signer already holds. On first contact
    there is no evidence it failed, so rotating it would be destructive guesswork."""
    d = decide(doc(signers=[signer(1, "pending", opened=None)]), chases_so_far=0, now=NOW)
    assert d.action is Action.CHASE


def test_an_unopened_link_is_reissued_once_a_chase_has_already_failed():
    """Asked once, still never opened: the message is not getting through, so change
    the mechanism rather than repeating it into the same void."""
    d = decide(doc(signers=[signer(1, "pending", opened=None)]), chases_so_far=1, now=NOW)
    assert d.action is Action.REISSUE
    assert "never opened" in d.reason


def test_an_opened_link_is_never_reissued_however_often_chased():
    """They have the link and have used it. Rotating it would break what works."""
    d = decide(doc(signers=[signer(1, "viewed", opened=NOW)]), chases_so_far=2, now=NOW)
    assert d.action is Action.CHASE


def test_an_opened_but_unsigned_link_is_chased_not_reissued():
    d = decide(doc(signers=[signer(1, "viewed", opened=NOW - dt.timedelta(days=3))]), now=NOW)
    assert d.action is Action.CHASE


# -- knowing when to stop -----------------------------------------------------


def test_a_fourth_chase_is_refused():
    d = decide(
        doc(signers=[signer(1, "viewed", opened=NOW)]),
        chases_so_far=3,
        now=NOW,
    )
    assert d.action is Action.STOP_CHASING
    assert "escalate or reissue" in d.reason


def test_an_expired_document_is_not_chased():
    d = decide(
        doc(expires_at=NOW - dt.timedelta(days=1), signers=[signer(1, "viewed", opened=NOW)]),
        now=NOW,
    )
    assert d.action is Action.STOP_CHASING
    assert "expiry" in d.reason


def test_expiry_outranks_staleness():
    """Both rules fire; the document is stale AND lapsed. Chasing a lapsed request
    wastes the signer's time, so expiry must win."""
    d = decide(
        doc(
            days_old=90,
            expires_at=NOW - dt.timedelta(days=30),
            signers=[signer(1, "viewed", opened=NOW)],
        ),
        now=NOW,
    )
    assert d.action is Action.STOP_CHASING


# -- across the book ----------------------------------------------------------


def test_triage_puts_actionable_items_first_and_oldest_first():
    docs = [
        doc(id="fresh", days_old=2, signers=[signer(1, "viewed", opened=NOW)]),
        doc(id="old", days_old=40, signers=[signer(1, "viewed", opened=NOW)]),
        doc(id="mid", days_old=10, signers=[signer(1, "viewed", opened=NOW)]),
    ]
    order = [d.document_id for d in triage(docs, now=NOW)]
    assert order == ["old", "mid", "fresh"]


def test_chase_history_is_honoured_per_document():
    docs = [
        doc(id="a", signers=[signer(1, "viewed", opened=NOW)]),
        doc(id="b", signers=[signer(1, "viewed", opened=NOW)]),
    ]
    out = {d.document_id: d.action for d in triage(docs, chase_history={"b": 3}, now=NOW)}
    assert out["a"] is Action.CHASE
    assert out["b"] is Action.STOP_CHASING
