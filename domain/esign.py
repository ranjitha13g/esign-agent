"""The e-sign object model, as the platform actually shapes it.

Field names and enums here were read off GET /api/schemas on 2 October 2026, not
guessed. See discovery/FINDINGS.md.

    EsignDocument.status        draft | sent   (observed; declared as a state field)
    EsignDocument.signing_order sequential | parallel
    EsignSigner.status          pending | viewed | signed | declined
    EsignSigner.sign_order      required, numeric

Two absences shape everything downstream: there is no reminder_count and no
last_reminded_at on the document, and EsignAuditLog has no tools. Chase history
therefore cannot be read back off the record, and lives in AgentMemory instead.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

# A signer in one of these states will never sign this document.
SIGNER_TERMINAL = frozenset({"signed", "declined"})
# A signer in one of these states still owes us something.
SIGNER_OUTSTANDING = frozenset({"pending", "viewed"})

# EsignDocument.status declares flow "EsignDocumentFlow", and the flow itself is not
# introspectable -- no flow tool, no flow entity, and _transitions is [] on every
# record. The states below were established by observation across BOTH books on
# 2 October 2026. Suryodaya alone would have been misleading: it only ever shows
# draft and sent. Keystone shows the rest.
#
#   draft 9 · sent 6 · viewed 5 · completed 31 · declined 6 · voided 5 · expired 36
#
# Chaseable is an allowlist, not a terminal denylist. A state we have never seen must
# fail closed -- nobody gets chased on a guess. "viewed" earns its place on evidence:
# all five such documents on Keystone still hold outstanding signers.
DOC_CHASEABLE = frozenset({"sent", "viewed"})
DOC_UNSENT = frozenset({"draft"})

# Confirmed terminal. Between them these hold 49 outstanding signers on Keystone --
# exactly the trap an agent that chased "anyone not yet signed" would fall into.
DOC_TERMINAL = frozenset({"voided", "declined", "expired", "completed"})


def _parse_ts(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed


@dataclass(frozen=True)
class Signer:
    id: str
    document_id: str
    email: str
    full_name: str
    sign_order: float
    status: str
    last_opened_at: dt.datetime | None
    auth_method: str
    access_token_expires_at: dt.datetime | None = None

    @property
    def is_outstanding(self) -> bool:
        return self.status in SIGNER_OUTSTANDING

    @property
    def has_opened(self) -> bool:
        return self.last_opened_at is not None or self.status == "viewed"

    def link_is_dead(self, now: dt.datetime | None = None) -> bool:
        """Their signing link has expired, so a reminder sends them to a dead page.

        Observed on Keystone: every signer on all ten genuinely-pending documents had
        access_token_expires_at in the past. Chasing those would have been useless --
        polite, well-worded, and pointing at nothing.
        """
        if self.access_token_expires_at is None:
            return False
        return self.access_token_expires_at < (now or dt.datetime.now(dt.UTC))

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Signer:
        return cls(
            id=str(row.get("id", "")),
            document_id=str(row.get("esign_document_id", "")),
            email=str(row.get("email", "")),
            full_name=str(row.get("full_name", "")),
            sign_order=float(row.get("sign_order") or 0),
            status=str(row.get("status") or ""),
            last_opened_at=_parse_ts(row.get("last_opened_at")),
            auth_method=str(row.get("auth_method") or ""),
            access_token_expires_at=_parse_ts(row.get("access_token_expires_at")),
        )


@dataclass(frozen=True)
class Document:
    id: str
    title: str
    status: str
    signing_order: str
    current_signer_order: float | None
    expires_at: dt.datetime | None
    created_at: dt.datetime | None
    updated_at: dt.datetime | None
    replaced_by: str
    signers: tuple[Signer, ...] = ()

    # -- state ---------------------------------------------------------

    @property
    def is_unsent(self) -> bool:
        return self.status in DOC_UNSENT

    @property
    def is_chaseable(self) -> bool:
        """Only a state we have positively confirmed can be chased."""
        return self.status in DOC_CHASEABLE

    @property
    def looks_terminal(self) -> bool:
        """Only to word the reason. The decision is DOC_CHASEABLE, which is an
        allowlist, so an unseen state is skipped whether or not it appears here."""
        return self.status in DOC_TERMINAL

    @property
    def outstanding(self) -> tuple[Signer, ...]:
        return tuple(s for s in self.signers if s.is_outstanding)

    @property
    def is_fully_signed(self) -> bool:
        return bool(self.signers) and not self.outstanding

    def is_expired(self, now: dt.datetime | None = None) -> bool:
        """A method, not a property: expiry is relative to an instant, and that
        instant is injected so a replayed run ages identically to the recorded one."""
        if self.expires_at is None:
            return False
        return self.expires_at < (now or dt.datetime.now(dt.UTC))

    # -- ageing --------------------------------------------------------

    def age_days(self, now: dt.datetime | None = None) -> float | None:
        """How long this has been sitting.

        There is no sent_at field, so the best available proxy is the last write. Note
        that created_at is server-stamped and cannot be back-dated, which is why
        realistic ageing scenarios have to be built in harness/fake.py.
        """
        stamp = self.updated_at or self.created_at
        if stamp is None:
            return None
        return ((now or dt.datetime.now(dt.UTC)) - stamp).total_seconds() / 86400.0

    def next_to_act(self) -> tuple[Signer, ...]:
        """Who the document is actually waiting on right now.

        Under sequential routing only the current position is blocking; chasing anyone
        further down the list asks them to act before their turn. Under parallel
        routing everyone outstanding is blocking at once.
        """
        outstanding = self.outstanding
        if not outstanding:
            return ()
        if self.signing_order != "sequential":
            return outstanding
        if self.current_signer_order is not None:
            at_turn = tuple(s for s in outstanding if s.sign_order == self.current_signer_order)
            if at_turn:
                return at_turn
        return (min(outstanding, key=lambda s: s.sign_order),)

    @classmethod
    def from_row(cls, row: dict[str, Any], signers: list[Signer] | None = None) -> Document:
        order = row.get("current_signer_order")
        return cls(
            id=str(row.get("id", "")),
            title=str(row.get("title") or ""),
            status=str(row.get("status") or ""),
            signing_order=str(row.get("signing_order") or ""),
            current_signer_order=float(order) if order is not None else None,
            expires_at=_parse_ts(row.get("expires_at")),
            created_at=_parse_ts(row.get("created_at")),
            updated_at=_parse_ts(row.get("updated_at")),
            replaced_by=str(row.get("replaced_by_document_id") or ""),
            signers=tuple(signers or ()),
        )


def assemble(
    doc_rows: list[dict[str, Any]], signer_rows: list[dict[str, Any]]
) -> list[Document]:
    """Join documents to their signers in one pass."""
    signers = [Signer.from_row(r) for r in signer_rows]
    by_doc: dict[str, list[Signer]] = {}
    for s in signers:
        by_doc.setdefault(s.document_id, []).append(s)
    for group in by_doc.values():
        group.sort(key=lambda s: s.sign_order)
    return [Document.from_row(r, by_doc.get(str(r.get("id", "")), [])) for r in doc_rows]
