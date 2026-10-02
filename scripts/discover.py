"""Phase 1 discovery: dump everything seat 18 can see, from every instance it can reach.

Read-only. Writes raw JSON to discovery/<instance>/ and a structural fingerprint that CI
later uses to detect drift. Nothing here writes to the shared database.

    uv run python scripts/discover.py            # both graded books
    uv run python scripts/discover.py --probe    # also try the non-business verticals
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "discovery"

# The two graded books. Same software, same seat, two jurisdictions.
BOOKS = {
    "suryodaya": ("https://agentswitch.theschoolofai.in", "AS_PASSWORD_SURYODAYA"),
    "keystone": ("https://class.agentswitch.theschoolofai.in", "AS_PASSWORD_KEYSTONE"),
}

# Seeded instances where the same tables carry different nouns. Whether our login
# reaches them is unknown -- that is what --probe answers.
VERTICALS = ["school", "clinic", "retail", "agency"]

MCP_PROTOCOL = "2025-11-25"

CRUD_VERBS = {"list", "get", "create", "update", "delete"}


def _safe_json(r: httpx.Response) -> Any:
    try:
        return r.json()
    except ValueError:
        return {"__nonjson__": r.text[:2000]}


class Platform:
    """Thin authenticated client. Every method here is a read."""

    def __init__(self, base: str, token: str) -> None:
        self.base = base.rstrip("/")
        self.http = httpx.Client(
            base_url=self.base,
            headers={"Authorization": "Bearer " + token},
            timeout=60.0,
        )
        self._mcp_id = 0

    def get(self, path: str, **params: Any) -> Any:
        r = self.http.get(path, params=params or None)
        # Record the refusal rather than raising: a 403 is a finding, not a failure.
        if r.status_code >= 400:
            return {"__status__": r.status_code, "__body__": _safe_json(r)}
        return _safe_json(r)

    def mcp(self, method: str, params: dict | None = None) -> Any:
        """JSON-RPC 2.0. A protocol error still returns HTTP 200 -- check the envelope."""
        self._mcp_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._mcp_id,
            "method": method,
            "params": params or {},
        }
        r = self.http.post("/api/mcp", json=payload)
        if r.status_code == 401:
            raise RuntimeError("401 from /api/mcp -- token rejected")
        body = _safe_json(r)
        if isinstance(body, dict) and "error" in body:
            return {"__jsonrpc_error__": body["error"]}
        if isinstance(body, dict):
            return body.get("result")
        return body

    def handshake(self) -> Any:
        init = self.mcp(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "team-18-esign", "version": "0.1"},
            },
        )
        self.mcp("notifications/initialized")
        return init

    def close(self) -> None:
        self.http.close()


def login(base: str, email: str, password: str) -> str | None:
    """Returns a bearer token, or None if the instance refuses us."""
    url = base.rstrip("/") + "/api/auth/login"
    try:
        r = httpx.post(url, json={"email": email, "password": password}, timeout=30.0)
    except httpx.HTTPError as exc:
        print("    unreachable: " + str(exc), file=sys.stderr)
        return None
    if r.status_code != 200:
        print("    login refused: HTTP " + str(r.status_code), file=sys.stderr)
        return None
    return _safe_json(r).get("token")


def discover(name: str, base: str, token: str) -> dict[str, Any]:
    """Pull the full readable surface for one instance."""
    p = Platform(base, token)
    data: dict[str, Any] = {"instance": name, "base_url": base}
    try:
        data["auth_me"] = p.get("/api/auth/me")
        data["mcp_initialize"] = p.handshake()
        data["tools_list"] = p.mcp("tools/list")
        data["agent_tools"] = p.get("/api/agent/tools")
        data["schemas"] = p.get("/api/schemas")
        data["accounting_locale"] = p.get("/api/accounting/locale")

        # The 403 surface. The escalation route named in a refusal is the correct answer
        # to a refusal task, so capture it verbatim rather than inventing it later.
        data["denied"] = {
            entity: p.get("/api/" + entity, limit=1)
            for entity in ("Contract", "SalarySlip")
        }

        # Live census: does the data the policy exists to handle actually exist?
        data["esign_census"] = p.get("/api/EsignDocument", limit=200)
    finally:
        p.close()
    return data


def _tool_names(data: dict[str, Any]) -> list[str]:
    tools = data.get("tools_list")
    if not isinstance(tools, dict):
        return []
    return sorted(t.get("name", "") for t in tools.get("tools", []))


def _entities(data: dict[str, Any]) -> list[dict[str, Any]]:
    """GET /api/schemas answers {"schemas": [...], "total": N}.

    Each entry carries entity, domain (the owning app) and a field list, so the domain
    is what tells us which entities belong to this seat rather than guessing from names.
    """
    schemas = data.get("schemas")
    if isinstance(schemas, dict):
        rows = schemas.get("schemas")
        if isinstance(rows, list):
            return [r for r in rows if isinstance(r, dict)]
    return []


def summarise(data: dict[str, Any]) -> dict[str, Any]:
    """Pull out the handful of answers that decide the architecture."""
    names = _tool_names(data)
    esign_tools = [n for n in names if n.lower().startswith("esign")]
    # One tool per workflow transition, so anything that is not CRUD is a state move.
    transitions = [n for n in esign_tools if n.rsplit(".", 1)[-1] not in CRUD_VERBS]

    rows = _entities(data)
    by_domain: dict[str, list[str]] = {}
    for r in rows:
        by_domain.setdefault(r.get("domain", "?"), []).append(r.get("entity", "?"))
    for v in by_domain.values():
        v.sort()

    me = data.get("auth_me") or {}
    denied = data.get("denied") or {}

    return {
        "instance": data["instance"],
        "roles": me.get("roles"),
        "allowed_apps": me.get("allowed_apps"),
        "locale": data.get("accounting_locale"),
        "tool_count": len(names),
        "esign_tools": esign_tools,
        "esign_transitions": transitions,
        "entity_count": len(rows),
        "domains": sorted(by_domain),
        "esign_entities": by_domain.get("esign", []),
        "agent_entities": by_domain.get("agent", []),
        "crm_entities": by_domain.get("crm", []),
        # The single question the whole build hangs on.
        "reminder_transition_candidates": [
            n
            for n in names
            if any(k in n.lower() for k in ("remind", "notify", "nudge", "chase", "resend"))
        ],
        "denied_status": {
            k: v.get("__status__") for k, v in denied.items() if isinstance(v, dict)
        },
    }


def fingerprint(data: dict[str, Any]) -> str:
    """Hash of the capability surface. CI fails when this moves underneath us."""
    esign = [r for r in _entities(data) if r.get("domain") == "esign"]
    esign.sort(key=lambda r: r.get("entity", ""))
    blob = json.dumps({"tools": _tool_names(data), "esign_schemas": esign}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def write(name: str, data: dict[str, Any]) -> None:
    d = OUT / name
    d.mkdir(parents=True, exist_ok=True)
    for key, value in data.items():
        if key in ("instance", "base_url"):
            continue
        body = json.dumps(value, indent=2, sort_keys=True)
        (d / (key + ".json")).write_text(body, encoding="utf-8")
    summary = json.dumps(summarise(data), indent=2, sort_keys=True)
    (d / "summary.json").write_text(summary, encoding="utf-8")


def check_drift(fresh: dict[str, str]) -> int:
    """Compare the live capability surface against the committed baseline.

    A changed fingerprint means tools/list or the esign schemas moved underneath us, so
    every recorded cassette is now suspect. In a platform twenty-six teams are using,
    that is worth finding from a nightly job rather than from a confusing test failure.
    """
    baseline_path = OUT / "fingerprint.json"
    if not baseline_path.exists():
        print("no committed fingerprint yet -- recording this run as the baseline")
        baseline_path.write_text(json.dumps(fresh, indent=2), encoding="utf-8")
        return 0

    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    drifted = [k for k, v in fresh.items() if baseline.get(k) != v]
    if not drifted:
        print("no drift: capability surface matches the committed fingerprint")
        return 0

    for book in drifted:
        print(
            "DRIFT on {}: baseline {} -> live {}".format(
                book, (baseline.get(book) or "none")[:12], fresh[book][:12]
            ),
            file=sys.stderr,
        )
    print(
        "\nThe tool catalogue or esign schemas changed. Re-record cassettes and "
        "re-read discovery/FINDINGS.md before trusting the task set.",
        file=sys.stderr,
    )
    return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="also try school/clinic/retail/agency")
    ap.add_argument(
        "--check-drift",
        action="store_true",
        help="compare against the committed fingerprint and exit non-zero on change",
    )
    ap.add_argument(
        "--book",
        choices=sorted(BOOKS),
        action="append",
        help="limit to one book (repeatable). Default: both, since both are graded.",
    )
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    email = os.environ.get("AS_EMAIL", "team18@theschoolofai.in")

    wanted = {k: v for k, v in BOOKS.items() if not args.book or k in args.book}

    missing = [var for _, var in wanted.values() if not os.environ.get(var)]
    if missing:
        print("Missing in .env: " + ", ".join(missing), file=sys.stderr)
        print("Copy .env.example to .env and fill in the passwords.", file=sys.stderr)
        return 1

    OUT.mkdir(exist_ok=True)
    prints: dict[str, str] = {}
    summaries: list[dict[str, Any]] = []

    for name, (base, var) in wanted.items():
        print("[" + name + "] " + base)
        token = login(base, email, os.environ[var])
        if not token:
            print("[" + name + "] LOGIN FAILED -- skipping", file=sys.stderr)
            continue
        data = discover(name, base, token)
        write(name, data)
        prints[name] = fingerprint(data)
        s = summarise(data)
        summaries.append(s)
        print(
            "[{}] {} tools, {} entities, apps={}, esign transitions={}".format(
                name,
                s["tool_count"],
                s["entity_count"],
                s["allowed_apps"],
                len(s["esign_transitions"]),
            )
        )

    if args.probe:
        # Unknown whether our login reaches these. Record the answer either way.
        reach: dict[str, bool] = {}
        for v in VERTICALS:
            base = "https://" + v + ".agentswitch.theschoolofai.in"
            print("[probe:" + v + "] " + base)
            tok = login(base, email, os.environ["AS_PASSWORD_SURYODAYA"])
            reach[v] = bool(tok)
            if tok:
                write(v, discover(v, base, tok))
        (OUT / "vertical_access.json").write_text(json.dumps(reach, indent=2), encoding="utf-8")
        ok = [k for k, v in reach.items() if v]
        print("[probe] reachable: " + (", ".join(ok) if ok else "none"))

    (OUT / "summary.json").write_text(json.dumps(summaries, indent=2), encoding="utf-8")

    if args.check_drift:
        # Do not overwrite the baseline we are checking against.
        print("\nWrote " + str(OUT))
        return check_drift(prints)

    # Merge, so discovering one book does not drop the other book's baseline.
    fp_path = OUT / "fingerprint.json"
    existing = json.loads(fp_path.read_text(encoding="utf-8")) if fp_path.exists() else {}
    existing.update(prints)
    fp_path.write_text(json.dumps(existing, indent=2, sort_keys=True), encoding="utf-8")
    print("\nWrote " + str(OUT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
