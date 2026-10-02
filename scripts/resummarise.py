"""Rebuild summary.json and fingerprint.json from already-saved discovery output.

Parsing improves faster than the platform changes, and every live call lands on a
database twenty-six teams are using. Re-read from disk instead.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from discover import OUT, fingerprint, summarise  # noqa: E402

KEYS = (
    "auth_me",
    "tools_list",
    "agent_tools",
    "schemas",
    "accounting_locale",
    "denied",
    "esign_census",
)


def main() -> int:
    prints = {}
    for d in sorted(p for p in OUT.iterdir() if p.is_dir()):
        data = {"instance": d.name}
        for k in KEYS:
            f = d / (k + ".json")
            if f.exists():
                data[k] = json.loads(f.read_text(encoding="utf-8"))
        s = summarise(data)
        (d / "summary.json").write_text(
            json.dumps(s, indent=2, sort_keys=True), encoding="utf-8"
        )
        prints[d.name] = fingerprint(data)
        print(f"{d.name}: {s['entity_count']} entities, {len(s['esign_entities'])} esign")

    (OUT / "fingerprint.json").write_text(
        json.dumps(prints, indent=2, sort_keys=True), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
