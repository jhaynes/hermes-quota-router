#!/usr/bin/env python3
"""Group a profile's kanban worker sessions by model: card outcome, review rejects, turns.

Read-only over ``<profile>/state.db`` and the kanban board databases (opened ``mode=ro``). The
numbers confound model with task (different cards went to different models); they are a cheap first
signal for the weekly report, not a benchmark.

    python eval/outcomes.py --state-db ~/.hermes/profiles/<p>/state.db \
        --kanban-db ~/.hermes/kanban/boards/<board>/kanban.db [--days 7] [--json]
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

TASK_RE = re.compile(r"\bwork kanban task (t_[0-9a-f]{6,})\b")
REJECT_KINDS = ("changes_requested",)


def _ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True, timeout=5)
    con.execute("select 1 from sqlite_master limit 1")
    return con


def _sessions(state_db: Path, since: float) -> list[dict]:
    con = _ro(state_db)
    try:
        rows = con.execute("""
            select s.id, s.model, coalesce(s.billing_provider, ''), coalesce(s.api_call_count, 0),
                   (select m.content from messages m where m.session_id = s.id and m.role = 'user'
                    order by m.id limit 1)
            from sessions s
            where s.source = 'kanban' and s.parent_session_id is null and s.started_at >= ?
            order by s.started_at""", (since,)).fetchall()
    finally:
        con.close()
    out = []
    for sid, model, provider, turns, first in rows:
        m = TASK_RE.search(first or "")
        out.append({"id": sid, "model": model or "?", "provider": provider or "?", "turns": int(turns),
                    "task": m.group(1) if m else None})
    return out


def _task_facts(kanban_dbs: list[Path], errors: list[str]) -> dict:
    facts: dict = {}
    for db in kanban_dbs:
        try:
            con = _ro(db)
        except sqlite3.Error as exc:
            errors.append(f"{db}: {exc}")
            continue
        try:
            for tid, status in con.execute("select id, status from tasks"):
                facts.setdefault(tid, {"status": status, "rejects": 0})
            q = "select task_id, count(*) from task_events where kind in (%s) group by task_id" % (
                ",".join("?" * len(REJECT_KINDS)))
            for tid, n in con.execute(q, REJECT_KINDS):
                if tid in facts:
                    facts[tid]["rejects"] += int(n)
        except sqlite3.Error as exc:
            errors.append(f"{db}: {exc}")
        finally:
            con.close()
    return facts


def build_report(state_db: Path, kanban_dbs: list[Path], since: float) -> dict:
    errors: list[str] = []
    try:
        sessions = _sessions(Path(state_db), since)
    except sqlite3.Error as exc:
        return {"since": since, "models": {}, "errors": [f"{state_db}: {exc}"]}
    facts = _task_facts([Path(p) for p in kanban_dbs], errors)
    models: dict = {}
    for s in sessions:
        key = f"{s['provider']}/{s['model']}"
        g = models.setdefault(key, {"sessions": 0, "tasks": set(), "turns_total": 0, "unlinked_sessions": 0})
        g["sessions"] += 1
        g["turns_total"] += s["turns"]
        if s["task"]:
            g["tasks"].add(s["task"])
        else:
            g["unlinked_sessions"] += 1
    out = {}
    for key, g in sorted(models.items()):
        outcomes: dict = {}
        rejects = 0
        for tid in sorted(g["tasks"]):
            f = facts.get(tid)
            status = f["status"] if f else "unknown"
            outcomes[status] = outcomes.get(status, 0) + 1
            rejects += f["rejects"] if f else 0
        out[key] = {"sessions": g["sessions"], "tasks": len(g["tasks"]), "outcomes": outcomes,
                    "review_rejects": rejects, "unlinked_sessions": g["unlinked_sessions"],
                    "turns": {"total": g["turns_total"], "mean_per_session": g["turns_total"] / g["sessions"]}}
    return {"since": since, "models": out, "errors": errors}


def render_markdown(report: dict) -> str:
    lines = ["## Builder outcomes by model", "",
             "Real sessions, grouped by the model that ran them. Model is confounded with task: "
             "different cards went to different models. A card retried on two models counts under both.", "",
             "| model | sessions | cards | outcomes | review rejects | turns (mean/session) |",
             "|---|---|---|---|---|---|"]
    for key, g in report["models"].items():
        oc = ", ".join(f"{k} {v}" for k, v in sorted(g["outcomes"].items())) or "-"
        lines.append(f"| {key} | {g['sessions']} | {g['tasks']} | {oc} | {g['review_rejects']} | "
                     f"{g['turns']['mean_per_session']:.1f} |")
    for e in report.get("errors") or []:
        lines.append(f"\n_error: {e}_")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--state-db", required=True, type=Path)
    ap.add_argument("--kanban-db", action="append", default=[], type=Path)
    ap.add_argument("--days", type=float, default=7.0)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    report = build_report(a.state_db.expanduser(), [p.expanduser() for p in a.kanban_db],
                          since=time.time() - a.days * 86400)
    print(json.dumps(report, indent=1) if a.json else render_markdown(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
