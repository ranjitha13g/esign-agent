"""Acts on a decision. The only module in the agent that writes.

Split from the policy on purpose. The policy decides and is pure; this decides
nothing. That split is what lets the whole task set run dozens of times in dry-run
while still exercising every judgement, and it is why the open question about whether
a reminder transition exists never required a rewrite.

Two things about this platform shape the implementation:

* There is no reminder or notify transition. The nearest real act is
  reissue_signing_link, which rotates a signer's link and invalidates the old one.
  That is a chase with a side effect, not a nudge, so it is never the default.
* There is no reminder_count or last_reminded_at on the document, and EsignAuditLog
  has no tools. So the record of a chase has to live in AgentMemory, which is private
  to this seat.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from agent.policy import Action, Decision
from domain.esign import DOMAIN as ESIGN_DOMAIN
from domain.locale import Locale
from mcp.client import McpClient, McpError

# Our own workspace, as the platform names it in Company.active_domains.
AGENT_DOMAIN = "agent"


class Mode(StrEnum):
    DRY_RUN = "dry-run"
    LIVE = "live"


@dataclass
class Outcome:
    document_id: str
    action: str
    performed: bool
    detail: str
    draft: str = ""


def chase_key(document_id: str) -> str:
    return f"chase:{document_id}"


class Executor:
    def __init__(
        self, mcp: McpClient, mode: Mode = Mode.DRY_RUN, locale: Locale | None = None
    ) -> None:
        self.mcp = mcp
        self.mode = mode
        # Guideline section 13: active_domains gates writes, never reads. Without a
        # locale we cannot confirm any domain is active, so nothing may be written.
        self.locale = locale

    def _blocked_domain(self) -> str | None:
        """Which domain, if any, stops us writing. None means go ahead.

        Checked for both apps we touch: the app that owns the documents, and our own
        workspace where the chase record lives. A chase that cannot be recorded is not
        a chase we should make -- it would repeat on the next run.
        """
        if self.locale is None:
            return "unknown"
        for domain in (ESIGN_DOMAIN, AGENT_DOMAIN):
            if not self.locale.permits_write(domain):
                return domain
        return None

    # -- chase history ----------------------------------------------------

    def chase_history(self) -> dict[str, int]:
        """How many times we have chased each document.

        Read back from AgentMemory, because the platform offers nowhere else to put
        it. Without this the 'do not send a fourth nudge' rule can never fire.
        """
        history: dict[str, int] = {}
        try:
            rows = self.mcp.call("AgentMemory.list", {"limit": 100}).get("data", [])
        except (McpError, KeyError):
            return history
        for row in rows:
            key = str(row.get("key", ""))
            if not key.startswith("chase:"):
                continue
            try:
                payload = json.loads(row.get("value") or "{}")
            except json.JSONDecodeError:
                payload = {}
            history[key.removeprefix("chase:")] = int(payload.get("count", 1))
        return history

    def _record_chase(self, decision: Decision, existing: dict[str, Any] | None) -> None:
        key = chase_key(decision.document_id)
        count = 1
        if existing:
            try:
                count = int(json.loads(existing.get("value") or "{}").get("count", 0)) + 1
            except json.JSONDecodeError:
                count = 1
        payload = json.dumps(
            {
                "count": count,
                "last_at": dt.datetime.now(dt.UTC).isoformat(),
                "action": decision.action.value,
                "targets": [s.email for s in decision.targets],
            }
        )
        # Update in place rather than appending, so a retry cannot leave two records.
        if existing:
            self.mcp.call(
                "AgentMemory.update",
                {"id": existing["id"], "value": payload},
                idempotency_key=key,
            )
        else:
            self.mcp.call(
                "AgentMemory.create",
                {"key": key, "value": payload},
                idempotency_key=key,
            )

    def _existing_memory(self, document_id: str) -> dict[str, Any] | None:
        try:
            rows = self.mcp.call("AgentMemory.list", {"limit": 100}).get("data", [])
        except (McpError, KeyError):
            return None
        for row in rows:
            if row.get("key") == chase_key(document_id):
                return row
        return None

    # -- acting -------------------------------------------------------------

    def execute(self, decision: Decision) -> Outcome:
        if not decision.is_actionable:
            return Outcome(decision.document_id, decision.action.value, False, "not actionable")

        draft = self.compose(decision)

        # Checked in both modes on purpose. Gating only live runs would let a dry run
        # report "drafted, not sent" for a domain it could never write to -- a cheerful
        # plan that would fail the moment anyone acted on it.
        blocked = self._blocked_domain()
        if blocked is not None:
            return Outcome(
                decision.document_id,
                decision.action.value,
                False,
                f"refused: this company does not list {blocked!r} as an active domain, "
                "so writes to it are not permitted",
                draft,
            )

        if self.mode is Mode.DRY_RUN:
            return Outcome(
                decision.document_id, decision.action.value, False, "drafted, not sent", draft
            )

        # Re-read before acting. The signer may have signed in the last forty seconds,
        # and other teams write to these rows continuously.
        fresh = self.mcp.call("EsignDocument.get", {"id": decision.document_id})
        row = fresh.get("data", fresh)
        if row.get("status") != "sent":
            return Outcome(
                decision.document_id,
                decision.action.value,
                False,
                f"stood down: state changed to {row.get('status')!r} before we acted",
                draft,
            )

        existing = self._existing_memory(decision.document_id)
        detail = ""
        if decision.action is Action.REISSUE and decision.targets:
            result = self.mcp.call(
                "EsignDocument.reissue_signing_link",
                {"id": decision.document_id, "signer_id": decision.targets[0].id},
                idempotency_key=chase_key(decision.document_id),
            )
            detail = str(result.get("data", result))[:200]
        else:
            # No reminder transition exists, so a CHASE records intent and hands the
            # drafted message to a human rather than pretending to have sent it.
            detail = "no reminder transition on this platform; chase recorded as intent"

        self._record_chase(decision, existing)
        return Outcome(decision.document_id, decision.action.value, True, detail, draft)

    @staticmethod
    def compose(decision: Decision) -> str:
        """The message a person would send. Tone varies with how overdue it is."""
        names = ", ".join(s.full_name for s in decision.targets) or "there"
        age = decision.age_days or 0
        if age > 30:
            urgency = f"This has been outstanding for {age:.0f} days and is now blocking."
        elif age > 14:
            urgency = f"This has been waiting {age:.0f} days."
        else:
            urgency = "This is still outstanding."
        if decision.action is Action.REISSUE:
            closing = "The previous link has been replaced; the new one is below."
        else:
            closing = "The signing link is below."
        return f"{names} -- {urgency} {closing}"
