"""throttle stamp-resume: mark throttled profiles' TUI/desktop rows ``follow_profile_config`` so their next
resume follows the router instead of re-pinning the stored route (README "Resumed sessions").

Every test runs against a temp HERMES_HOME. The real state file's mtime is asserted unchanged. One test
runs the real ``hermes`` CLI (``QR_HERMES_BIN``; ``QR_SKIP_LIVE=1`` skips it).
"""
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_root = Path(__file__).resolve().parents[1]
REAL_STATE = Path.home() / ".hermes" / "cache" / "quota-router" / "state.json"
NOW = 1_800_000_000.0

LADDER = [
    {"name": "fast", "gate": ["anthropic"], "provider": "anthropic", "model": "opus-x",
     "fallback": [{"provider": "custom:internal", "model": "glm-x"}]},
    {"name": "constrained", "provider": "custom:internal", "model": "glm-x",
     "fallback": [{"provider": "openai-codex", "model": "codex-x"}]},
]
SCHEMA = ("create table sessions (id text primary key, source text, title text, model text, "
          "billing_provider text, model_config text, hidden integer default 0, started_at real)")


def _load(name, file, monkeypatch):
    spec = importlib.util.spec_from_file_location(name, _root / file)
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, mod)
    spec.loader.exec_module(mod)
    return mod


def fake_contract():
    """Hermes' resume rule (``tui_gateway/server.py`` ``_row_follows_profile`` /
    ``_stored_session_runtime_overrides``): a profile-following row restores no stored route."""
    def check(row):
        mc = json.loads(row.get("model_config") or "{}")
        return bool(mc.get("follow_profile_config")) or (row.get("title") or "").strip() == "Bot Chat"
    return check


@pytest.fixture
def env(tmp_path, monkeypatch):
    real_mtime = REAL_STATE.stat().st_mtime if REAL_STATE.exists() else None
    home = tmp_path / ".hermes"
    (home / "profiles" / "worker").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    core = _load("quota_router_core", "core.py", monkeypatch)
    core.state_path = lambda hermes_home=None: home / "cache" / "quota-router" / "state.json"
    thr = _load("quota_router_throttle", "throttle.py", monkeypatch)
    plugin = _load("qr_plugin_stampcase", "__init__.py", monkeypatch)
    e = SimpleNamespace(home=home, core=core, thr=thr, plugin=plugin,
                        t=core.throttle_config({"ladder": LADDER, "profiles": ["worker", "default"],
                                                "enabled": True, "stamp_resume": True}))
    yield e
    if real_mtime is not None:
        assert REAL_STATE.stat().st_mtime == real_mtime, "real state file was touched"


def make_db(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.execute(SCHEMA)
    for r in rows:
        r = {"source": "tui", "title": "", "billing_provider": None, "model_config": None, "hidden": 0,
             "started_at": NOW, **r}
        con.execute("insert into sessions values (:id, :source, :title, :model, :billing_provider, "
                    ":model_config, :hidden, :started_at)", r)
    con.commit()
    con.close()


def model_configs(path):
    con = sqlite3.connect(str(path))
    def parse(mc):
        try:
            return json.loads(mc) if mc else mc
        except ValueError:
            return mc
    try:
        return {i: parse(mc) for i, mc in con.execute("select id, model_config from sessions")}
    finally:
        con.close()


ROWS = [
    {"id": "glm", "model": "glm-x", "billing_provider": "custom",
     "model_config": json.dumps({"provider": "custom", "base_url": "http://x", "reasoning_effort": "high"})},
    {"id": "opus", "model": "opus-x", "billing_provider": "anthropic", "model_config": None},
    {"id": "desk", "source": "desktop", "model": "codex-x", "billing_provider": "openai-codex", "model_config": "{}"},
    {"id": "picked", "model": "gpt-elsewhere", "billing_provider": "openrouter", "model_config": "{}"},
    {"id": "wrong-prov", "model": "glm-x", "billing_provider": "openrouter", "model_config": "{}"},
    {"id": "bot", "title": "Bot Chat", "model": "glm-x", "model_config": "{}"},
    {"id": "following", "model": "glm-x", "model_config": json.dumps({"follow_profile_config": True})},
    {"id": "prov", "model": "glm-x", "model_config": json.dumps({
        "composer_override_profile": {"model": "opus-x", "provider": "anthropic"}})},
    {"id": "plumb", "model": "glm-x", "model_config": json.dumps({"room_plumbing": True})},
    {"id": "hidden", "model": "glm-x", "hidden": 1, "model_config": "{}"},
    {"id": "bad", "model": "glm-x", "model_config": "{not json"},
    {"id": "nomodel", "model": None, "model_config": "{}"},
    {"id": "cron", "source": "cron", "model": "glm-x", "model_config": "{}"},
]


def test_stamps_only_rows_on_a_ladder_route_and_preserves_their_config(env):
    db = env.home / "profiles" / "worker" / "state.db"
    make_db(db, ROWS)
    res = env.thr.run_stamp(env.home, env.t, contract=fake_contract)
    assert res["refused"] is None
    r = res["profiles"]["worker"]
    assert (r["stamp"], r["already"], r["explicit_pick"], r["off_ladder"], r["plumbing"], r["unparseable"],
            r["no_model"], r["contract"]) == (3, 2, 1, 2, 2, 1, 1, 0)
    assert res["profiles"]["default"]["missing"] is True
    mc = model_configs(db)
    assert mc["glm"] == {"provider": "custom", "base_url": "http://x", "reasoning_effort": "high",
                         "follow_profile_config": True}
    assert mc["opus"] == {"follow_profile_config": True}
    assert mc["desk"] == {"follow_profile_config": True}
    for left in ("picked", "wrong-prov", "bot", "plumb", "hidden", "nomodel", "cron"):
        assert mc[left] in ({}, None, {"room_plumbing": True}), left
    assert "follow_profile_config" not in mc["prov"]


def test_is_idempotent(env):
    db = env.home / "profiles" / "worker" / "state.db"
    make_db(db, ROWS)
    env.thr.run_stamp(env.home, env.t, contract=fake_contract)
    before = model_configs(db)
    again = env.thr.run_stamp(env.home, env.t, contract=fake_contract)["profiles"]["worker"]
    assert again["stamp"] == 0 and again["already"] == 5
    assert model_configs(db) == before


def test_dry_run_writes_nothing(env):
    db = env.home / "profiles" / "worker" / "state.db"
    make_db(db, ROWS)
    before = db.read_bytes()
    res = env.thr.run_stamp(env.home, env.t, dry_run=True, contract=fake_contract)
    assert res["profiles"]["worker"]["stamp"] == 3
    assert db.read_bytes() == before
    assert "would mark 3" in env.thr.render_stamp(res)


def test_root_profile_reads_the_root_state_db(env):
    make_db(env.home / "state.db", [{"id": "r", "model": "opus-x", "billing_provider": "anthropic"}])
    res = env.thr.run_stamp(env.home, env.t, contract=fake_contract)
    assert res["profiles"]["default"]["stamp"] == 1
    assert model_configs(env.home / "state.db")["r"] == {"follow_profile_config": True}


def test_refused_contract_writes_nothing_anywhere(env):
    for p in (env.home / "state.db", env.home / "profiles" / "worker" / "state.db"):
        make_db(p, ROWS)
    snap = {p: p.read_bytes() for p in (env.home / "state.db", env.home / "profiles" / "worker" / "state.db")}

    def refuse():
        raise env.thr.StampRefused("Hermes no longer resumes follow_profile_config rows onto the profile config")

    res = env.thr.run_stamp(env.home, env.t, contract=refuse)
    assert res["refused"].startswith("Hermes no longer")
    assert all(p.read_bytes() == b for p, b in snap.items())
    assert env.thr.stamp_alerts(res) == [
        "quota-router: resume stamp refused, nothing stamped: Hermes no longer resumes "
        "follow_profile_config rows onto the profile config"]


def test_row_the_reader_rejects_is_left_alone(env):
    db = env.home / "profiles" / "worker" / "state.db"
    make_db(db, ROWS)
    res = env.thr.run_stamp(env.home, env.t, contract=lambda: (lambda row: '"base_url"' not in row["model_config"]))
    r = res["profiles"]["worker"]
    assert (r["stamp"], r["contract"]) == (2, 1)
    assert "follow_profile_config" not in model_configs(db)["glm"]


def test_contract_not_loaded_when_nothing_to_stamp(env):
    make_db(env.home / "profiles" / "worker" / "state.db", [ROWS[3], ROWS[5]])

    def boom():
        raise AssertionError("contract loaded with nothing to stamp")

    res = env.thr.run_stamp(env.home, env.t, contract=boom)
    assert res["refused"] is None and res["profiles"]["worker"]["stamp"] == 0


def test_locked_db_reports_the_profile_and_continues(env, monkeypatch):
    monkeypatch.setattr(env.thr, "STAMP_BUSY_TIMEOUT_S", 0.1)
    worker = env.home / "profiles" / "worker" / "state.db"
    make_db(worker, ROWS)
    make_db(env.home / "state.db", [{"id": "r", "model": "opus-x", "billing_provider": "anthropic"}])
    holder = sqlite3.connect(str(worker), isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        res = env.thr.run_stamp(env.home, env.t, contract=fake_contract)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert "locked" in res["profiles"]["worker"]["error"]
    assert res["profiles"]["default"]["stamp"] == 1
    assert "follow_profile_config" not in model_configs(worker)["glm"]


def test_route_matching(env):
    routes = env.thr.ladder_routes(env.t)
    assert ("anthropic", "anthropic", "opus-x") in routes
    m = env.thr.route_matches
    assert m("anthropic", "opus-x", routes)
    assert m("claude", "opus-x", routes)            # alias of the tracked provider
    assert m("custom", "glm-x", routes)             # agent's bare name for a custom entry
    assert m("custom:internal", "glm-x", routes)
    assert m(None, "glm-x", routes)                 # no stored provider: model alone
    assert not m("custom", "opus-x", routes)        # a tracked route is never "custom"
    assert not m("openrouter", "glm-x", routes)
    assert not m("anthropic", "sonnet-x", routes)
    assert not m("anthropic", "", routes)


def test_route_matches_uses_the_watched_provider_list(env, monkeypatch):
    """A bare ``custom`` row never matches a tracked (watched) provider's route; that list is core's."""
    routes = [("acme", "acme", "m1")]
    assert env.thr.route_matches("custom", "m1", routes)
    monkeypatch.setattr(env.core, "WATCHED", ("anthropic", "openai-codex", "acme"))
    assert not env.thr.route_matches("custom", "m1", routes)


def test_cli_is_a_silent_no_op_unless_opted_in(env, monkeypatch, capsys):
    make_db(env.home / "profiles" / "worker" / "state.db", ROWS)
    before = (env.home / "profiles" / "worker" / "state.db").read_bytes()
    monkeypatch.setattr(env.thr, "hermes_stamp_contract", fake_contract)
    for over in ({"stamp_resume": False}, {"enabled": False}):
        monkeypatch.setattr(env.plugin, "_throttle_cfg", lambda o=over: env.core.throttle_config(
            {"ladder": LADDER, "profiles": ["worker"], "enabled": True, "stamp_resume": True, **o}))
        assert env.plugin._stamp_resume(SimpleNamespace(dry_run=False, json=False), env.core, env.thr,
                                        env.plugin._throttle_cfg(), env.core.state_path(), NOW) == 0
        out = capsys.readouterr().out
        assert out.startswith("resume stamp: off") and "quota-router:" not in out
    assert (env.home / "profiles" / "worker" / "state.db").read_bytes() == before


def test_cli_without_a_throttle_block_is_off_even_in_dry_run(env, monkeypatch, capsys):
    """No ``throttle`` block is an opt-out, not an invalid config: rc 0 with or without ``--dry-run``."""
    monkeypatch.setattr(env.thr, "run_stamp", lambda *a, **k: pytest.fail("must not scan without a block"))
    t = env.core.throttle_config({})
    for dry in (False, True):
        assert env.plugin._stamp_resume(SimpleNamespace(dry_run=dry, json=False), env.core, env.thr, t,
                                        env.core.state_path(), NOW) == 0
        assert capsys.readouterr().out == "resume stamp: off (no throttle block)\n"


def test_cli_dry_run_still_errors_on_an_invalid_config(env, capsys):
    t = env.core.throttle_config({"ladder": LADDER, "profiles": ["worker"], "stamp_resume": "yes"})
    assert env.plugin._stamp_resume(SimpleNamespace(dry_run=True, json=False), env.core, env.thr, t,
                                    env.core.state_path(), NOW) == 2
    assert "throttle config invalid (throttle.stamp_resume must be a boolean)" in capsys.readouterr().out


def test_cli_json_off_and_invalid_paths_print_one_qrstamp_line(env, monkeypatch, capsys):
    """``--json`` keeps stdout machine-readable on every path, not only after a scan."""
    monkeypatch.setattr(env.thr, "run_stamp", lambda *a, **k: pytest.fail("must not scan"))
    cases = [
        (env.core.throttle_config({}), False, 0, {"off": "no throttle block"}),
        (env.core.throttle_config({"ladder": LADDER, "profiles": ["worker"], "enabled": False,
                                   "stamp_resume": True}), False, 0,
         {"off": "needs throttle.enabled and throttle.stamp_resume: true"}),
        (env.core.throttle_config({"ladder": LADDER, "profiles": ["worker"], "stamp_resume": "yes"}), True, 2,
         {"error": "throttle config invalid (throttle.stamp_resume must be a boolean)"}),
    ]
    for t, dry, rc, payload in cases:
        assert env.plugin._stamp_resume(SimpleNamespace(dry_run=dry, json=True), env.core, env.thr, t,
                                        env.core.state_path(), NOW) == rc
        out = capsys.readouterr().out.splitlines()
        assert len(out) == 1 and out[0].startswith("QRSTAMP ") and json.loads(out[0][8:]) == payload


def test_alert_items_tag_failures_structurally(env):
    """Dedupe keys off a structural flag, not the message prose."""
    res = {"refused": None, "dry_run": False, "profiles": {
        "a": {"stamp": 2, "error": None}, "b": {"stamp": 0, "error": "OperationalError: database is locked"}}}
    assert env.thr.stamp_alert_items(res) == [
        ("quota-router: resume stamp marked 2 a session(s) to follow config", False),
        ("quota-router: resume stamp for b failed (OperationalError: database is locked)", True)]
    assert env.thr.stamp_alerts(res) == [ln for ln, _ in env.thr.stamp_alert_items(res)]
    assert env.thr.stamp_alert_items({"refused": "x", "profiles": {}}) == [
        ("quota-router: resume stamp refused, nothing stamped: x", True)]


def test_nothing_to_stamp_classifies_each_row_once(env, monkeypatch):
    db = env.home / "profiles" / "worker" / "state.db"
    make_db(db, [{"id": "s", "model": "gpt-elsewhere", "billing_provider": "openrouter"},
                 {"id": "t", "model": "opus-x", "billing_provider": "anthropic",
                  "model_config": json.dumps({"follow_profile_config": True})}])
    calls = []
    real = env.thr.classify_stamp_row
    monkeypatch.setattr(env.thr, "classify_stamp_row", lambda r, routes: calls.append(r["id"]) or real(r, routes))
    res = env.thr.run_stamp(env.home, env.t, contract=lambda: pytest.fail("nothing to stamp"))
    assert res["profiles"]["worker"]["stamp"] == 0 and sorted(calls) == ["s", "t"]


def test_cli_stamps_and_alerts_refusals_once(env, monkeypatch, capsys):
    make_db(env.home / "profiles" / "worker" / "state.db", ROWS)
    t = env.core.throttle_config({"ladder": LADDER, "profiles": ["worker"], "enabled": True, "stamp_resume": True})
    args = SimpleNamespace(dry_run=False, json=False)

    def run():
        assert env.plugin._stamp_resume(args, env.core, env.thr, t, env.core.state_path(), NOW) == 0
        return [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("quota-router:")]

    real_run = env.thr.run_stamp
    monkeypatch.setattr(env.thr, "run_stamp", lambda home, t, dry_run=False: real_run(home, t, dry_run=dry_run,
                                                                                     contract=fake_contract))
    assert run() == ["quota-router: resume stamp marked 3 worker session(s) to follow config"]
    assert run() == []

    def refuse():
        raise env.thr.StampRefused("reader gone")

    (env.home / "profiles" / "worker" / "state.db").unlink()   # fresh unmarked rows, so the contract loads
    make_db(env.home / "profiles" / "worker" / "state.db", ROWS)
    monkeypatch.setattr(env.thr, "run_stamp", lambda home, t, dry_run=False: real_run(home, t, dry_run=dry_run,
                                                                                     contract=refuse))
    assert run() == ["quota-router: resume stamp refused, nothing stamped: reader gone"]
    assert run() == []   # same refusal: already delivered
    st = env.core.read_state(env.core.state_path())
    assert st["throttle"]["resume_stamp"]["last"]["refused"] == "reader gone"


def test_validate_rejects_a_non_boolean_switch(env):
    t = env.core.throttle_config({"ladder": LADDER, "profiles": ["worker"], "stamp_resume": "yes"})
    assert env.core.validate_throttle(t, {}) == ["throttle.stamp_resume must be a boolean"]


HERMES_BIN = Path(os.environ.get("QR_HERMES_BIN", str(Path.home() / ".local" / "bin" / "hermes")))


@pytest.mark.skipif(not HERMES_BIN.exists() or os.environ.get("QR_SKIP_LIVE") == "1", reason="no hermes CLI")
def test_live_cli_stamps_through_the_real_resume_reader(tmp_path):
    """The real ``hermes quota-router throttle stamp-resume`` against a temp HERMES_HOME: the stamp lands
    only because Hermes' own resume reader (``tui_gateway.server``) said the stamped row follows config
    and restores no stored route, and stdout carries the deliverable line (the gateway import's stdout
    redirect was undone)."""
    home = tmp_path / ".hermes"
    (home / "plugins").mkdir(parents=True)
    (home / "plugins" / "quota-router").symlink_to(_root, target_is_directory=True)
    (home / "config.yaml").write_text(json.dumps({
        "model": {"default": "opus-x", "provider": "anthropic"},
        "plugins": {"enabled": ["quota-router"], "entries": {"quota-router": {"throttle": {
            "enabled": True, "stamp_resume": True, "profiles": ["default"], "ladder": LADDER}}}}}))
    make_db(home / "state.db", [ROWS[0], ROWS[3]])
    child_env = {k: v for k, v in os.environ.items() if not k.startswith("HERMES_")}
    child_env["HERMES_HOME"] = str(home)
    r = subprocess.run([str(HERMES_BIN), "quota-router", "throttle", "stamp-resume"],
                       capture_output=True, text=True, timeout=180, env=child_env)
    assert r.returncode == 0, (r.stdout, r.stderr[-2000:])
    assert "quota-router: resume stamp marked 1 default session(s) to follow config" in r.stdout, r.stdout
    mc = model_configs(home / "state.db")
    assert mc["glm"]["follow_profile_config"] is True and mc["glm"]["base_url"] == "http://x"
    assert mc["picked"] == {}
