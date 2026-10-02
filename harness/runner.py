"""Runs the task set and scores it from disk.

Three rules, taken literally from the brief:

1. Our own loop, not a wrapper around the platform's.
2. Verifiers read the database, never the agent's prose. The reply is recorded in the
   run directory for a human to read; no check consults it.
3. Every run is written to disk before anything is scored. Scoring then re-reads from
   that directory, so a failure is always replayable and never depends on a process
   that has already exited.

    uv run python -m harness.runner                     # every task, against the fake
    uv run python -m harness.runner --task chase-stale-week
    uv run python -m harness.runner --live-read         # read live, still never writes
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.executor import Executor, Mode  # noqa: E402
from agent.policy import triage  # noqa: E402
from domain.esign import assemble  # noqa: E402
from domain.locale import resolve  # noqa: E402
from harness import scenarios  # noqa: E402
from harness.verify import Evidence, Verdict, run_all  # noqa: E402
from mcp.client import McpClient  # noqa: E402
from mcp.transport import HttpTransport  # noqa: E402

RUNS = ROOT / "runs"
TASKS = ROOT / "harness" / "tasks.yaml"


def read_world(c: McpClient) -> tuple[list[dict], list[dict]]:
    docs = c.page("EsignDocument.list")
    signers = c.page("EsignSigner.list")
    return docs, signers


def execute_task(task: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    """Run one task end to end and persist everything before returning."""
    scenario = getattr(scenarios, task["scenario"])
    platform = scenario()
    client = McpClient(platform)
    client.connect()

    before = platform.snapshot()
    now = dt.datetime.now(dt.UTC)

    docs, signers = read_world(client)
    locale = resolve(client.call("Company.list", {}).get("data", []))
    executor = Executor(client, Mode(task.get("mode", "dry-run")), locale=locale)
    decisions = triage(assemble(docs, signers), chase_history=executor.chase_history(), now=now)

    outcomes: list[dict[str, Any]] = []
    chased: list[str] = []
    errors: list[dict[str, str]] = []

    def act(decision) -> None:
        """Run one decision. A failure on one document must not lose the rest.

        The run has already written to the database by this point. Aborting here
        would leave those writes unexplained and unscored -- the opposite of what
        the harness is for.
        """
        try:
            outcome = executor.execute(decision)
        except Exception as exc:  # noqa: BLE001 -- recorded as evidence, not swallowed
            errors.append(
                {
                    "document_id": decision.document_id,
                    "action": decision.action.value,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            outcomes.append(
                {
                    "document_id": decision.document_id,
                    "action": decision.action.value,
                    "reason": decision.reason,
                    "age_days": decision.age_days,
                    "performed": False,
                    "detail": f"FAILED {type(exc).__name__}: {exc}",
                    "draft": "",
                }
            )
            return
        if outcome.performed:
            chased.append(decision.document_id)
        outcomes.append(
            {
                "document_id": decision.document_id,
                "action": decision.action.value,
                "reason": decision.reason,
                "age_days": decision.age_days,
                "performed": outcome.performed,
                "detail": outcome.detail,
                "draft": outcome.draft,
            }
        )

    try:
        for decision in decisions:
            act(decision)
        # Replay-and-retry tasks run the whole thing twice to prove idempotency.
        if task.get("retry"):
            for decision in decisions:
                if decision.is_actionable:
                    act(decision)
    finally:
        # Persist whatever happened, including a crash. Scoring reads from disk, so a
        # run that is not written down cannot be scored, replayed or explained.
        after = platform.snapshot()
        record = {
            "task": task["id"],
            "mode": task.get("mode", "dry-run"),
            "ran_at": now.isoformat(),
            "decisions": outcomes,
            "chased_ids": chased,
            "errors": errors,
            "transport_retries": list(getattr(platform, "retries", [])),
            "tool_calls": [{"name": n, "arguments": a} for n, a in platform.calls],
        }
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "run.json").write_text(
            json.dumps(record, indent=2, default=str), encoding="utf-8"
        )
        for name, blob in (("before", before), ("after", after)):
            (out_dir / f"{name}.json").write_text(
                json.dumps(blob, indent=2, default=str), encoding="utf-8"
            )
        client.close()

    return record


def score_from_disk(task: dict[str, Any], out_dir: Path) -> list[dict[str, Any]]:
    """Re-read the persisted run and verify it. Never touches a live process."""
    record = json.loads((out_dir / "run.json").read_text(encoding="utf-8"))
    evidence = Evidence(
        before=json.loads((out_dir / "before.json").read_text(encoding="utf-8")),
        after=json.loads((out_dir / "after.json").read_text(encoding="utf-8")),
        chased_ids=tuple(record["chased_ids"]),
        now=dt.datetime.fromisoformat(record["ran_at"]),
    )
    checks = run_all(task.get("verify", []), evidence)
    payload = [{"name": c.name, "verdict": c.verdict.value, "detail": c.detail} for c in checks]

    # A task that threw part-way is not a pass, whatever the verifiers say about the
    # state it managed to reach.
    for err in record.get("errors", []):
        payload.append(
            {
                "name": "run_completed_without_error",
                "verdict": Verdict.UNEVALUATED.value,
                "detail": "{} on {}: {}".format(
                    err["action"], err["document_id"][:8], err["error"]
                ),
            }
        )
    (out_dir / "verdicts.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", type=Path, default=TASKS)
    ap.add_argument("--task", help="run a single task by id")
    ap.add_argument("--live-read", action="store_true", help="read the live book first")
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    tasks = yaml.safe_load(args.tasks.read_text(encoding="utf-8"))
    if args.task:
        tasks = [t for t in tasks if t["id"] == args.task]
        if not tasks:
            print(f"no task {args.task!r}", file=sys.stderr)
            return 2

    if args.live_read:
        # Proves the client works against the real platform. Reads only.
        base = "https://agentswitch.theschoolofai.in"
        from scripts.discover import login

        token = login(
            base, os.environ.get("AS_EMAIL", ""), os.environ.get("AS_PASSWORD_SURYODAYA", "")
        )
        if token:
            c = McpClient(HttpTransport(base, token))
            c.connect()
            live_docs = c.page("EsignDocument.list")
            c.close()
            print(f"live read OK: {len(live_docs)} documents on the real book\n")

    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    root = RUNS / stamp
    failures = 0

    for task in tasks:
        out_dir = root / task["id"]
        execute_task(task, out_dir)
        verdicts = score_from_disk(task, out_dir)

        worst = Verdict.APPROVE
        for v in verdicts:
            if v["verdict"] == Verdict.UNEVALUATED.value:
                worst = Verdict.UNEVALUATED
                break
            if v["verdict"] == Verdict.REVISE.value:
                worst = Verdict.REVISE
        if worst is not Verdict.APPROVE:
            failures += 1

        print(f"{task['id']:<28} {worst.value}")
        for v in verdicts:
            mark = {"approve": "  ok  ", "revise": " FAIL ", "unevaluated": " ???? "}[v["verdict"]]
            line = f"  [{mark}] {v['name']}"
            print(line + (f" -- {v['detail']}" if v["detail"] else ""))

    print(f"\nruns/{stamp}/   {len(tasks) - failures}/{len(tasks)} tasks approved")
    if failures:
        print("unevaluated is never a pass", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
