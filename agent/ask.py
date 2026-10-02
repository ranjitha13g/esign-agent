"""Ask the agent a question in plain language.

    uv run python -m agent.ask "What is pending signature and with whom?"
    uv run python -m agent.ask --record chase-stale-week "<the graded request>"

Reads only. The planner has no tool that writes.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.model import AnthropicClient, RecordingModel, ReplayModel  # noqa: E402
from agent.planner import Planner  # noqa: E402
from mcp.client import McpClient  # noqa: E402
from mcp.transport import HttpTransport, RecordingTransport, ReplayTransport  # noqa: E402
from scripts.discover import BOOKS, login  # noqa: E402

GRADED = "What is pending signature and with whom, and chase everything sitting over a week."


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("question", nargs="?", default=GRADED)
    ap.add_argument("--book", choices=sorted(BOOKS), default="suryodaya")
    ap.add_argument("--record", metavar="NAME", help="record this run as a cassette")
    ap.add_argument("--replay", metavar="NAME", help="replay a cassette, no network")
    ap.add_argument("--show-calls", action="store_true")
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    cassettes = ROOT / "cassettes"

    if args.replay:
        mcp_t = ReplayTransport(cassettes / f"{args.replay}.mcp.jsonl")
        model = ReplayModel(cassettes / f"{args.replay}.model.jsonl")
        # Replay the clock too, or ageing drifts and every message changes.
        meta = json.loads((cassettes / f"{args.replay}.meta.json").read_text(encoding="utf-8"))
        now = dt.datetime.fromisoformat(meta["now"])
    else:
        base, var = BOOKS[args.book]
        token = login(base, os.environ.get("AS_EMAIL", ""), os.environ.get(var, ""))
        if not token:
            print(f"could not sign in to {args.book}", file=sys.stderr)
            return 1
        mcp_t = HttpTransport(base, token)
        model = AnthropicClient()
        now = dt.datetime.now(dt.UTC)
        if args.record:
            mcp_t = RecordingTransport(mcp_t, cassettes / f"{args.record}.mcp.jsonl")
            model = RecordingModel(model, cassettes / f"{args.record}.model.jsonl")
            cassettes.mkdir(exist_ok=True)
            (cassettes / f"{args.record}.meta.json").write_text(
                json.dumps({"now": now.isoformat(), "question": args.question}, indent=2),
                encoding="utf-8",
            )

    client = McpClient(mcp_t)
    client.connect()
    try:
        result = Planner(client, model, now=now).ask(args.question)
    finally:
        client.close()

    print("Q:", args.question)
    print()
    print(result.answer)
    if args.show_calls:
        print("\n--- tool calls ---")
        for call in result.tool_calls:
            print(" ", call["name"], call["input"] or "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
