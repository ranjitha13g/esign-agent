"""Worlds for the task set to run against.

Each builds a FakePlatform seeded for one question. They exist because the live book
cannot pose these questions: every document there is the same age, 95 are drafts, and
the 5 sent ones have no signers.
"""

from __future__ import annotations

import datetime as dt

from harness.fake import FakePlatform


def mixed_book() -> FakePlatform:
    """The ordinary case: some stale, some fresh, some finished."""
    p = FakePlatform()
    p.add_document(
        title="Long overdue, opened but unsigned",
        days_old=31,
        signers=[{"status": "viewed", "last_opened_at": "2026-09-10T00:00:00+00:00"}],
    )
    p.add_document(
        title="Stale and never opened",
        days_old=14,
        signers=[{"status": "pending", "last_opened_at": None}],
    )
    p.add_document(
        title="Sent three days ago",
        days_old=3,
        signers=[{"status": "viewed", "last_opened_at": "2026-09-30T00:00:00+00:00"}],
    )
    p.add_document(
        title="Everyone has signed",
        days_old=40,
        signers=[{"status": "signed"}, {"status": "signed", "sign_order": 2}],
    )
    p.add_document(title="Never sent", status="draft", days_old=90,
                   signers=[{"status": "pending"}])
    return p


def terminal_states() -> FakePlatform:
    """States that must never be chased, including one the flow never told us about.

    'archived' is deliberately a state we have never seen. A policy that guessed a
    terminal list would chase it; one that only chases confirmed-chaseable states
    will not.
    """
    p = FakePlatform()
    for status in ("voided", "declined", "expired", "completed", "archived"):
        p.add_document(
            title=f"In state {status}",
            status=status,
            days_old=60,
            signers=[{"status": "pending"}],
        )
    return p


def sequential_routing() -> FakePlatform:
    """Only the signer whose turn it is may be chased."""
    p = FakePlatform()
    p.add_document(
        title="Three signers in order",
        days_old=21,
        signing_order="sequential",
        signers=[
            {"status": "viewed", "sign_order": 1, "last_opened_at": "2026-09-20T00:00:00+00:00",
             "email": "first@example.com"},
            {"status": "pending", "sign_order": 2, "email": "second@example.com"},
            {"status": "pending", "sign_order": 3, "email": "third@example.com"},
        ],
    )
    return p


def already_chased() -> FakePlatform:
    """A document chased three times already. A fourth is refused."""
    p = FakePlatform()
    doc_id = p.add_document(
        title="Chased to death",
        days_old=45,
        signers=[{"status": "viewed", "last_opened_at": "2026-09-01T00:00:00+00:00"}],
    )
    p.rows["AgentMemory"].append(
        {
            "id": "m1",
            "key": f"chase:{doc_id}",
            "value": '{"count": 3, "last_at": "2026-09-25T00:00:00+00:00"}',
        }
    )
    return p


def signs_mid_run() -> FakePlatform:
    """Another actor signs between our read and our write.

    The executor re-reads before acting, so it must stand down rather than chase
    somebody who has already signed.
    """
    p = FakePlatform()
    doc_id = p.add_document(
        title="Signed while we were thinking",
        days_old=20,
        signers=[{"status": "viewed", "last_opened_at": "2026-09-15T00:00:00+00:00"}],
    )

    def complete(platform: FakePlatform) -> None:
        for row in platform.rows["EsignDocument"]:
            if row["id"] == doc_id:
                row["status"] = "completed"
        for row in platform.rows["EsignSigner"]:
            if row["esign_document_id"] == doc_id:
                row["status"] = "signed"

    # Fires on the executor's re-read, after the policy has already decided to chase.
    p.mutate_on("EsignDocument.get", complete)
    return p


def expired_document() -> FakePlatform:
    """Lapsed. Chasing wastes the signer's time; it needs voiding or reissuing."""
    p = FakePlatform()
    p.add_document(
        title="Lapsed last month",
        days_old=90,
        expires_in_days=-30,
        signers=[{"status": "viewed", "last_opened_at": "2026-08-01T00:00:00+00:00"}],
    )
    return p


def sent_without_signers() -> FakePlatform:
    """The shape the real book is actually in: sent, but addressed to nobody."""
    p = FakePlatform()
    for i in range(3):
        p.add_document(title=f"Sent to nobody {i}", days_old=19, signers=[])
    return p


def dead_signing_links() -> FakePlatform:
    """Stale, and the signing links have already expired.

    This is Keystone's real shape: all ten genuinely-pending documents had
    access_token_expires_at in the past. A chase there is polite and points at
    nothing, so the correct act is to rotate the link.
    """
    p = FakePlatform()
    expired = (dt.datetime.now(dt.UTC) - dt.timedelta(days=2)).isoformat()
    p.add_document(
        title="Pending, link long dead",
        days_old=16,
        signers=[
            {"status": "viewed", "last_opened_at": "2026-09-20T00:00:00+00:00",
             "access_token_expires_at": expired}
        ],
    )
    return p


def out_of_seat() -> FakePlatform:
    """Contract belongs to another seat and is absent from the catalogue."""
    return FakePlatform(absent=("Contract", "SalarySlip"))


