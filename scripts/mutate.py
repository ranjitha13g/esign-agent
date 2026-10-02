"""Mutation testing: break the code on purpose, check the suite notices.

A green suite proves nothing on its own. What matters is whether it goes red when the
behaviour it claims to protect actually changes. Each mutation below is a plausible
regression -- an inverted guard, a dropped check, a loosened threshold. A mutation that
survives means no test is really watching that line.

    uv run python scripts/mutate.py            # every mutation
    uv run python scripts/mutate.py --list
    uv run python scripts/mutate.py --only policy
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Mutation:
    group: str
    name: str
    path: str
    old: str
    new: str


MUTATIONS = [
    # -- policy: the judgement ------------------------------------------------
    Mutation("policy", "chase terminal documents",
             "agent/policy.py", "    if not doc.is_chaseable:", "    if False:"),
    Mutation("policy", "chase drafts too",
             "agent/policy.py", "    if doc.is_unsent:", "    if False:"),
    Mutation("policy", "chase superseded documents",
             "agent/policy.py", "    if doc.replaced_by:", "    if False:"),
    Mutation("policy", "ignore the age bar",
             "agent/policy.py", "    if age < stale_after_days:", "    if False:"),
    Mutation("policy", "never stop after repeated chases",
             "agent/policy.py", "    if chases_so_far >= max_chases:", "    if False:"),
    Mutation("policy", "chase expired documents",
             "agent/policy.py", "    if doc.is_expired(now):", "    if False:"),
    Mutation("policy", "chase everyone, ignoring sequential order",
             "domain/esign.py", "        if self.signing_order != \"sequential\":",
             "        if True:"),
    Mutation("policy", "treat sent-with-no-signers as fine",
             "agent/policy.py", "    if not doc.signers:", "    if False:"),
    Mutation("policy", "reissue on first contact",
             "agent/policy.py", "    if chases_so_far >= 1 and all(not s.has_opened for s in targets):",
             "    if all(not s.has_opened for s in targets):"),
    Mutation("policy", "ignore dead signing links",
             "agent/policy.py", "    if all(s.link_is_dead(now) for s in targets):", "    if False:"),
    Mutation("policy", "widen chaseable to any state",
             "domain/esign.py", 'DOC_CHASEABLE = frozenset({"sent", "viewed"})',
             'DOC_CHASEABLE = frozenset({"sent", "viewed", "voided", "expired", "archived"})'),

    # -- client: the platform traps -------------------------------------------
    Mutation("client", "trust HTTP 200, ignore the error envelope",
             "mcp/client.py", '        if "error" in body and body["error"] is not None:',
             "        if False:"),
    Mutation("client", "ignore the isError flag",
             "mcp/client.py", '        if result.get("isError"):', "        if False:"),
    Mutation("client", "skip closed-schema validation",
             "mcp/client.py", "        if not schema:", "        if True:"),
    Mutation("client", "attach idempotency keys to reads too",
             "mcp/client.py", "        if tool.is_write and idempotency_key is not None:",
             "        if idempotency_key is not None:"),
    Mutation("client", "stop paging after the first page",
             "mcp/client.py", "            if not batch or len(batch) < page_size or (total is not None and offset >= total):",
             "            if True:"),

    # -- transport: replay fidelity -------------------------------------------
    Mutation("transport", "collapse repeated calls onto one response",
             "mcp/transport.py", "        return queue.popleft()", "        return queue[0]"),
    Mutation("transport", "ignore arguments when keying",
             "mcp/transport.py", '            "arguments": params.get("arguments") or {},',
             '            "arguments": {},'),

    # -- retry ----------------------------------------------------------------
    Mutation("retry", "retry unsafe writes as well",
             "mcp/retry.py", "    if not retriable:", "    if False:"),
    Mutation("retry", "raise the attempt ceiling",
             "mcp/retry.py", "MAX_ATTEMPTS = 3", "MAX_ATTEMPTS = 50"),

    # -- executor -------------------------------------------------------------
    Mutation("executor", "act without re-reading first",
             "agent/executor.py", '        if row.get("status") != "sent":', "        if False:"),
    Mutation("executor", "send even in dry-run",
             "agent/executor.py", "        if self.mode is Mode.DRY_RUN:", "        if False:"),
    Mutation("executor", "append a chase record instead of updating",
             "agent/executor.py", "        if existing:\n            self.mcp.call(\n                \"AgentMemory.update\",",
             "        if False:\n            self.mcp.call(\n                \"AgentMemory.update\","),

    # -- planner --------------------------------------------------------------
    Mutation("planner", "read the wall clock instead of the injected one",
             "agent/planner.py", "        self.now = now or dt.datetime.now(dt.UTC)",
             "        self.now = dt.datetime.now(dt.UTC)"),
    Mutation("planner", "hard-slice oversize tool results",
             "agent/planner.py", "        if len(blob) <= MAX_TOOL_RESULT:\n            return blob\n\n        if isinstance(payload, dict)",
             "        if len(blob) <= MAX_TOOL_RESULT:\n            return blob\n        return blob[:MAX_TOOL_RESULT]\n\n        if isinstance(payload, dict)"),
    Mutation("planner", "let the loop spin without limit",
             "agent/planner.py", "MAX_TURNS = 12", "MAX_TURNS = 10_000"),

    # -- harness --------------------------------------------------------------
    Mutation("harness", "swallow a verifier that raises",
             "harness/verify.py", "            checks.append(Check(name, Verdict.UNEVALUATED, f\"{type(exc).__name__}: {exc}\"))",
             "            checks.append(Check(name, Verdict.APPROVE, ''))"),
    Mutation("harness", "do not report a run that errored",
             "harness/runner.py", '    for err in record.get("errors", []):', "    for err in []:"),
]


# Both gates CI runs. The harness task set is not pytest, so checking pytest alone
# reports every harness-covered behaviour as unguarded.
GATES = (
    ["uv", "run", "pytest", "-q", "-x", "--no-header"],
    ["uv", "run", "python", "-m", "harness.runner"],
)


def run_suite(cwd: Path) -> tuple[bool, str]:
    log = ""
    for cmd in GATES:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
        log += (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode != 0:
            return False, log
    return True, log


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="limit to one group")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    muts = [m for m in MUTATIONS if not args.only or m.group == args.only]
    if args.list:
        for m in muts:
            print(f"  {m.group:10} {m.name}")
        return 0

    work = Path(tempfile.mkdtemp())
    shutil.copytree(ROOT, work / "repo", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(".git", ".venv", "runs", "__pycache__",
                                                  ".pytest_cache", ".ruff_cache"))
    repo = work / "repo"

    print(f"Baseline suite in {repo}")
    ok, out = run_suite(repo)
    if not ok:
        print("Baseline is already red -- fix that first.\n" + out[-1500:])
        return 2
    print("Baseline green.\n")

    survived = []
    for m in muts:
        target = repo / m.path
        original = target.read_text(encoding="utf-8")
        if m.old not in original:
            print(f"  [SKIP   ] {m.group:10} {m.name}  (anchor not found in {m.path})")
            survived.append((m, "anchor missing"))
            continue
        target.write_text(original.replace(m.old, m.new, 1), encoding="utf-8")
        killed, _ = run_suite(repo)
        target.write_text(original, encoding="utf-8")

        if killed:
            print(f"  [SURVIVED] {m.group:10} {m.name}")
            survived.append((m, "no test noticed"))
        else:
            print(f"  [killed  ] {m.group:10} {m.name}")

    print(f"\n{len(muts) - len(survived)}/{len(muts)} mutations killed")
    if survived:
        print("\nSurvivors -- nothing is watching these:")
        for m, why in survived:
            print(f"  {m.group:10} {m.name:52} ({why})")
    return 1 if survived else 0


if __name__ == "__main__":
    raise SystemExit(main())
