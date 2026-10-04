"""CLI entry point.

  python run.py "Record the latest invoice from MedSupply Co in the ERP"
  python run.py "..." --auto-approve      # approve policy gates automatically (testing only)
  python run.py "..." --no-procedures     # force the agent to work it out from scratch
  python run.py --resume <run_id>         # continue a run after a crash or pause
"""
import argparse
import json
import sqlite3
import time

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from ai_operator import config, llm
from ai_operator.graph import build
from ai_operator.tools.browser import BROWSER


def ask(payload: dict, auto_approve: bool):
    print()
    if payload["type"] == "question":
        print(f"  ? Operator asks: {payload['question']}")
        return input("  > ")
    print(f"  ! APPROVAL NEEDED ({payload['rule']}) from {payload['approver']}")
    print(f"    Reason: {payload['reason']}")
    print(f"    Action: {payload['action']}")
    print(f"    Facts:  {json.dumps(payload['facts'])}")
    if auto_approve:
        print("    (auto-approved)")
        return {"approved": True, "note": "auto-approved in test mode"}
    ans = input("    Approve? [y/N] + optional note: ").strip()
    return {"approved": ans.lower().startswith("y"), "note": ans[1:].strip() or None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("request", nargs="?")
    ap.add_argument("--resume")
    ap.add_argument("--auto-approve", action="store_true")
    ap.add_argument("--no-procedures", action="store_true")
    a = ap.parse_args()

    saver = SqliteSaver(sqlite3.connect(config.STATE_DB, check_same_thread=False))
    graph = build(saver)
    run_id = a.resume or time.strftime("run_%Y%m%d_%H%M%S")
    cfg = {"configurable": {"thread_id": run_id}, "recursion_limit": 200}
    print(f"Run {run_id}")
    if a.resume:
        result = graph.invoke(None, cfg)
    else:
        result = graph.invoke({"request": a.request, "run_id": run_id,
                               "use_procedures": not a.no_procedures}, cfg)
    try:
        while result.get("__interrupt__"):
            answer = ask(result["__interrupt__"][0].value, a.auto_approve)
            result = graph.invoke(Command(resume=answer), cfg)
    finally:
        BROWSER.close()
    print(f"\nStatus: {result.get('status')}")
    if (result.get("finish") or {}).get("summary"):
        print(f"Summary: {result['finish']['summary']}")
    for line in (result.get("verification") or {}).get("details", []):
        print(f"  {line}")
    print(f"LLM calls: {llm.STATS['calls']} (cache hits {llm.STATS['cache_hits']})")
    print(f"Report: runs/{run_id}/report.md")


if __name__ == "__main__":
    main()
