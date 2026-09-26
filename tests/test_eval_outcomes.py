"""eval/outcomes.py: builder sessions grouped by model (plan Task 6; v2 "cheap first step")."""
import importlib.util
import sqlite3
from pathlib import Path

_root = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("qr_outcomes", _root / "eval" / "outcomes.py")
outcomes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(outcomes)
NOW = 1_800_000_000.0


def make_state_db(path):
    con = sqlite3.connect(path)
    con.executescript("""
        create table sessions (id text primary key, source text, model text, billing_provider text,
            parent_session_id text, started_at real, ended_at real, api_call_count integer,
            tool_call_count integer, message_count integer);
        create table messages (id integer primary key, session_id text, role text, content text,
            timestamp real);
    """)
    rows = [  # id, source, model, provider, parent, started, api_calls
        ("s1", "kanban", "opus-x", "anthropic", None, NOW - 900, 40),
        ("s2", "kanban", "opus-x", "anthropic", None, NOW - 800, 20),   # retry of t_bbbbbbbb
        ("s3", "kanban", "glm-x", "custom", None, NOW - 700, 70),
        ("s4", "kanban", "glm-x", "custom", "s3", NOW - 690, 5),       # delegate child: not a card run
        ("s5", "cli", "opus-x", "anthropic", None, NOW - 600, 9),       # interactive: ignored
        ("s6", "kanban", "glm-x", "custom", None, NOW - 50 * 86400, 9),  # outside --since
        ("s7", "kanban", "opus-x", "anthropic", None, NOW - 500, 11),   # no task id
    ]
    con.executemany("insert into sessions values (?,?,?,?,?,?,?,?,?,?)",
                    [(i, s, m, p, par, st, st + 60, a, a, 2 * a) for (i, s, m, p, par, st, a) in rows])
    msgs = {"s1": "work kanban task t_aaaaaaaa", "s2": "work kanban task t_bbbbbbbb",
            "s3": "work kanban task t_cccccccc", "s4": "work kanban task t_cccccccc",
            "s5": "hello", "s6": "work kanban task t_dddddddd", "s7": "something else"}
    con.executemany("insert into messages (session_id, role, content, timestamp) values (?,?,?,?)",
                    [(s, "user", c, NOW) for s, c in msgs.items()])
    con.commit()
    con.close()


def make_kanban_db(path):
    con = sqlite3.connect(path)
    con.executescript("""
        create table tasks (id text primary key, status text, assignee text);
        create table task_events (id integer primary key, task_id text, kind text, created_at integer);
    """)
    con.executemany("insert into tasks values (?,?,?)", [
        ("t_aaaaaaaa", "done", "builder"), ("t_bbbbbbbb", "blocked", "builder"),
        ("t_cccccccc", "done", "builder")])
    con.executemany("insert into task_events (task_id, kind, created_at) values (?,?,?)", [
        ("t_aaaaaaaa", "changes_requested", 1), ("t_aaaaaaaa", "changes_requested", 2),
        ("t_cccccccc", "completed", 3)])
    con.commit()
    con.close()


def test_groups_top_level_kanban_sessions_by_model(tmp_path):
    sdb, kdb = tmp_path / "state.db", tmp_path / "kanban.db"
    make_state_db(sdb)
    make_kanban_db(kdb)
    mtimes = (sdb.stat().st_mtime, kdb.stat().st_mtime)
    report = outcomes.build_report(sdb, [kdb], since=NOW - 30 * 86400)
    assert (sdb.stat().st_mtime, kdb.stat().st_mtime) == mtimes   # read-only
    by = report["models"]
    assert set(by) == {"anthropic/opus-x", "custom/glm-x"}
    opus = by["anthropic/opus-x"]
    assert opus["sessions"] == 3 and opus["tasks"] == 2
    assert opus["outcomes"] == {"done": 1, "blocked": 1}
    assert opus["review_rejects"] == 2
    assert opus["turns"] == {"total": 71, "mean_per_session": 71 / 3}
    assert opus["unlinked_sessions"] == 1
    glm = by["custom/glm-x"]
    assert glm["sessions"] == 1 and glm["outcomes"] == {"done": 1} and glm["review_rejects"] == 0
    assert glm["turns"]["total"] == 70


def test_tasks_on_boards_we_cannot_see_count_as_unknown(tmp_path):
    sdb = tmp_path / "state.db"
    make_state_db(sdb)
    report = outcomes.build_report(sdb, [], since=NOW - 30 * 86400)
    assert report["models"]["custom/glm-x"]["outcomes"] == {"unknown": 1}


def test_missing_or_bad_database_is_reported_not_raised(tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_text("not sqlite")
    report = outcomes.build_report(tmp_path / "missing.db", [bad], since=0)
    assert report["models"] == {}
    assert report["errors"] and all(isinstance(e, str) for e in report["errors"])


def test_markdown_rendering(tmp_path):
    sdb, kdb = tmp_path / "state.db", tmp_path / "kanban.db"
    make_state_db(sdb)
    make_kanban_db(kdb)
    md = outcomes.render_markdown(outcomes.build_report(sdb, [kdb], since=NOW - 30 * 86400))
    assert "| anthropic/opus-x |" in md and "confounded" in md
