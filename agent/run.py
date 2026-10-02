"""Answer the seat's question against the live platform.

    "What is pending signature and with whom, and chase everything sitting over a week."

Dry-run by default. A chase is a real notification to a seeded person in a book
twenty-six other teams are using, so sending needs asking for twice: --mode live and
AS_ALLOW_SEND=1.

    uv run python -m agent.run                  # read, decide, report. Sends nothing.
    uv run python -m agent.run --json           # machine-readable, for the harness
    AS_ALLOW_SEND=1 uv run python -m agent.run --mode live
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.policy import Action, Decision, triage  # noqa: E402
from domain.esign import Document, assemble  # noqa: E402
from domain.locale import Locale, resolve  # noqa: E402
from mcp.client import McpClient  # noqa: E402
from mcp.transport import HttpTransport  # noqa: E402
from scripts.discover import BOOKS, login  # noqa: E402


def gather(c: McpClient) -> tuple[Locale, list[Document]]:
    """Read the world. Every call here is a read."""
    locale = resolve(c.call("Company.list", {}).get("data", []))
    docs = c.page("EsignDocument.list")
    signers = c.page("EsignSigner.list")
    return locale, assemble(docs, signers)


def render(locale: Locale, decisions: list[Decision], docs: list[Document]) -> str:
    by_id = {d.id: d for d in docs}
    actionable = [d for d in decisions if d.is_actionable]
    escalations = [d for d in decisions if d.action is Action.ESCALATE]
    stop = [d for d in decisions if d.action is Action.STOP_CHASING]

    out: list[str] = []
    add = out.append

    where = locale.describe() if locale.is_known else "UNKNOWN -- not stated by the API"
    add(f"Book        {where}")
    add(f"Documents   {len(docs)}")
    add("")

    # Only a confirmed-chaseable state counts as pending; see domain/esign.py.
    waiting = [d for d in docs if d.is_chaseable and d.outstanding]
    add("PENDING SIGNATURE, AND WITH WHOM")
    if not waiting:
        add("  Nothing is pending with anyone.")
        sent_no_signers = [d for d in docs if not d.is_unsent and not d.signers]
        unsent_with = [d for d in docs if d.is_unsent and d.outstanding]
        if sent_no_signers:
            add(f"  {len(sent_no_signers)} sent, but carrying no signer rows at all.")
        if unsent_with:
            add(f"  {len(unsent_with)} hold outstanding signers but were never sent.")
        add("  That combination is a data problem, not an empty inbox.")
    else:
        for d in waiting:
            who = ", ".join(f"{s.full_name} <{s.email}> [{s.status}]" for s in d.next_to_act())
            age = d.age_days()
            add(f"  {d.id[:8]}  {d.title[:44]:<44} {age:>5.0f}d  {d.signing_order}")
            add(f"            waiting on: {who or '(nobody)'}")
    add("")

    add(f"CHASE PLAN  ({len(actionable)} to act on)")
    if not actionable:
        add("  Nothing to chase.")
    for d in actionable:
        doc = by_id[d.document_id]
        who = ", ".join(s.email for s in d.targets)
        add(f"  [{d.action.value.upper()}] {doc.title[:40]} -> {who}")
        add(f"            {d.reason}")
    add("")

    if stop:
        add(f"STOP CHASING  ({len(stop)})")
        for d in stop:
            add(f"  {by_id[d.document_id].title[:46]} -- {d.reason}")
        add("")

    if escalations:
        add(f"NEEDS A HUMAN  ({len(escalations)})")
        for d in escalations[:10]:
            add(f"  {by_id[d.document_id].title[:46]} -- {d.reason}")
        if len(escalations) > 10:
            add(f"  ... and {len(escalations) - 10} more")
        add("")

    skipped: dict[str, int] = {}
    for d in decisions:
        if d.action is Action.SKIP:
            skipped[d.reason.split("(")[0].strip()] = skipped.get(
                d.reason.split("(")[0].strip(), 0
            ) + 1
    if skipped:
        add("NOT ACTED ON")
        for reason, n in sorted(skipped.items(), key=lambda kv: -kv[1]):
            add(f"  {n:>4}  {reason}")

    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", choices=sorted(BOOKS), default="suryodaya")
    ap.add_argument("--mode", choices=("dry-run", "live"), default="dry-run")
    ap.add_argument("--json", action="store_true", help="emit the decisions as JSON")
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    base, var = BOOKS[args.book]

    if args.mode == "live" and os.environ.get("AS_ALLOW_SEND") != "1":
        print(
            "Refusing to send: --mode live also needs AS_ALLOW_SEND=1.\n"
            "A chase is a real notification in a database other teams are using.",
            file=sys.stderr,
        )
        return 2

    token = login(base, os.environ.get("AS_EMAIL", ""), os.environ.get(var, ""))
    if not token:
        print(f"could not sign in to {args.book}", file=sys.stderr)
        return 1

    c = McpClient(HttpTransport(base, token))
    c.connect()
    try:
        locale, docs = gather(c)
    finally:
        c.close()

    # Chase history has nowhere to live on the record -- there is no reminder_count and
    # EsignAuditLog has no tools -- so it belongs in AgentMemory. Empty until written.
    decisions = triage(docs, chase_history={})

    if args.json:
        payload: dict[str, Any] = {
            "generated_at": dt.datetime.now(dt.UTC).isoformat(),
            "book": args.book,
            "mode": args.mode,
            "locale": {"country": locale.country, "currency": locale.currency},
            "decisions": [
                {
                    "document_id": d.document_id,
                    "action": d.action.value,
                    "reason": d.reason,
                    "age_days": d.age_days,
                    "targets": [s.email for s in d.targets],
                }
                for d in decisions
            ],
        }
        print(json.dumps(payload, indent=2))
        return 0

    print(render(locale, decisions, docs))
    if args.mode == "dry-run":
        print("\n(dry run -- nothing was sent)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
