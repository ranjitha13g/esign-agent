"""The report renderer.

It had no test, so renaming a Document attribute broke `agent.run` silently while all
71 other tests stayed green. These cover the branches the live book and the fake each
produce, so the next rename fails here instead of at the command line.
"""

from __future__ import annotations

import datetime as dt

from agent.policy import triage
from agent.run import render
from domain.locale import Locale
from tests.unit.test_policy import NOW, doc, signer

LOCALE = Locale("c1", "Example Precision Works", "India", "INR")


def test_it_renders_the_live_books_shape_without_crashing():
    """Sent with no signers, plus drafts that do have signers. This is exactly what
    Suryodaya looks like, and it is the case that must not read as an empty inbox."""
    docs = [
        doc(id="a", status="sent", days_old=19, signers=[]),
        doc(id="b", status="draft", days_old=19, signers=[signer(1, "viewed")]),
    ]
    out = render(LOCALE, triage(docs, now=NOW), docs)
    assert "Nothing is pending with anyone" in out
    assert "sent, but carrying no signer rows" in out
    assert "were never sent" in out


def test_it_lists_who_each_document_waits_on():
    docs = [
        doc(id="c", days_old=31,
            signers=[signer(1, "viewed", opened=NOW - dt.timedelta(days=20),
                            email="a@example.com")])
    ]
    out = render(LOCALE, triage(docs, now=NOW), docs)
    assert "a@example.com" in out
    assert "CHASE PLAN" in out


def test_an_unknown_state_is_not_reported_as_pending():
    """Fail-closed must show up in the report too, not just in the policy."""
    docs = [doc(id="d", status="under_legal_review", days_old=40,
                signers=[signer(1, "viewed", opened=NOW)])]
    out = render(LOCALE, triage(docs, now=NOW), docs)
    assert "Nothing is pending with anyone" in out


def test_the_jurisdiction_comes_from_the_platform():
    out = render(LOCALE, [], [])
    assert "India" in out


def test_an_unknown_jurisdiction_is_flagged_not_guessed():
    out = render(Locale("c1", "Somewhere", "", ""), [], [])
    assert "UNKNOWN" in out
