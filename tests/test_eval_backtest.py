"""eval/backtest.py: replay history through the throttle policy over a threshold grid (plan Task 6; v2
"Eval validity")."""
import importlib.util
import sys
from pathlib import Path

_root = Path(__file__).resolve().parents[1]
_cs = importlib.util.spec_from_file_location("quota_router_core", _root / "core.py")
core = importlib.util.module_from_spec(_cs)
sys.modules.setdefault("quota_router_core", core)
_cs.loader.exec_module(core)
_bs = importlib.util.spec_from_file_location("qr_backtest", _root / "eval" / "backtest.py")
bt = importlib.util.module_from_spec(_bs)
_bs.loader.exec_module(bt)

H = 3600.0
M = 60.0
T0 = 1_800_000_000.0
CYCLE = 5 * H
REAL_SPIKE, FALSE_SPIKE = 10, 20


def synthetic_week(days=7.0):
    """Session window: 5h cycles sampled every 5 min at a gentle 5 %/h, except one real spike
    (22 %/h, reaches 97% before its reset) and one false spike (30 %/h for 1 h, then flat at 30%).
    Weekly window: 0 → 50% over the week."""
    session, week = [], []
    t = T0
    while t < T0 + days * 24 * H:
        k = int((t - T0) // CYCLE)
        start = T0 + k * CYCLE
        h = (t - start) / H
        if k == REAL_SPIKE:
            used = min(22 * h, 100)
        elif k == FALSE_SPIKE:
            used = 30 * min(h, 1.0)
        else:
            used = 5 * h
        session.append([t, round(used, 3), start + CYCLE])
        week.append([t, round(50 * (t - T0) / (7 * 24 * H), 3), T0 + 7 * 24 * H])
        t += 5 * M
    return {"Current session": session, "Current week": week}


def base_t(**over):
    return core.throttle_config({"profiles": [], **over})


def test_replay_catches_the_real_spike_and_flags_the_false_one():
    hist = synthetic_week()
    res = bt.replay(hist, [], base_t(), dict(core.DEFAULTS))
    score = bt.score(res, hist, [], base_t())
    real_start = T0 + REAL_SPIKE * CYCLE
    false_start = T0 + FALSE_SPIKE * CYCLE
    periods = score["periods"]
    assert any(real_start <= p["start"] < real_start + H for p in periods)
    assert any(false_start <= p["start"] < false_start + H for p in periods)
    assert score["misses"] == []
    assert [fa["start"] for fa in score["false_alarms"]] == [
        p["start"] for p in periods if false_start <= p["start"] < false_start + CYCLE]
    assert 0 < score["constrained_hours"] < 10
    assert score["opus_hours_given_up"] == score["constrained_hours"]


def test_a_late_reaction_is_a_miss():
    hist = synthetic_week()
    t = base_t(enter_projected=200.0, early_projected=200.0, enter_used=99.0)
    score = bt.score(bt.replay(hist, [], t, dict(core.DEFAULTS)), hist, [], t)
    assert len(score["misses"]) == 1
    assert score["misses"][0]["label"] == "Current session"


def test_a_block_while_fast_is_a_miss():
    hist = synthetic_week(days=2.0)
    block_at = T0 + 3 * CYCLE + H
    blocks = [{"since": block_at, "until": block_at + 2 * H}]
    t = base_t()
    res = bt.replay(hist, blocks, t, dict(core.DEFAULTS))
    assert any(r["source"] == "block" for r in res)
    score = bt.score(res, hist, blocks, t)
    assert [m["kind"] for m in score["misses"]] == ["block"]


def test_grid_is_bounded_by_floors_and_ceilings_and_keeps_leave_below_enter():
    grid = bt.grid(base_t(enter_projected=98.0, leave_projected=72.0))
    enters = sorted({g["enter_projected"] for g in grid})
    leaves = sorted({g["leave_projected"] for g in grid})
    assert enters == [93.0, 98.0, 100.0]
    assert leaves == [70.0, 72.0, 77.0]
    assert all(g["leave_projected"] < g["enter_projected"] for g in grid)
    assert bt.grid(base_t())[0] == {"enter_projected": 95.0, "leave_projected": 80.0}  # current first


def row(ep, lp, misses, hours):
    return {"params": {"enter_projected": ep, "leave_projected": lp}, "misses": misses, "false_alarms": 0,
            "constrained_hours": hours}


CURRENT = {"enter_projected": 95.0, "leave_projected": 80.0}


def test_proposal_minimises_misses_then_constrained_time_and_auto_tightens():
    rows = [row(95, 80, 2, 5.0), row(90, 80, 0, 9.0), row(90, 75, 0, 11.0), row(100, 85, 3, 1.0)]
    p = bt.propose(rows, CURRENT, days=8)
    assert p["best"] == {"enter_projected": 90.0, "leave_projected": 80.0}
    assert p["auto"] == {"enter_projected": 90.0}
    assert p["judgement"] == {}


def test_loosening_needs_three_weeks_and_is_labelled_judgement():
    rows = [row(95, 80, 0, 9.0), row(100, 85, 0, 2.0)]
    p = bt.propose(rows, CURRENT, days=10)
    assert p["auto"] == {}
    assert p["judgement"] == {"enter_projected": 100.0, "leave_projected": 85.0}
    assert "Justin's judgement" in p["note"] and "3 weeks" in p["note"]
    p = bt.propose(rows, CURRENT, days=22)
    assert p["judgement"] == {"enter_projected": 100.0, "leave_projected": 85.0}
    assert "Justin's judgement" in p["note"] and "3 weeks" not in p["note"]


def test_run_skips_until_seven_days_of_history():
    out = bt.run(synthetic_week(days=3.0), [], base_t(), dict(core.DEFAULTS))
    assert out["skipped"].startswith("needs 7 days of history")
    out = bt.run(synthetic_week(days=7.2), [], base_t(), dict(core.DEFAULTS))
    assert "skipped" not in out
    assert out["grid"][0]["params"] == CURRENT
    assert len(out["grid"]) == 9
    md = bt.render_markdown(out)
    assert "biased" in md and "| enter_projected |" in md


def _state_history(days, cadence_s):
    """History as the plugin records it: every fetch folded through core.apply_refresh, which
    prunes to the 7-day retention window."""
    st = core.empty_state()
    t = T0
    while t < T0 + days * 24 * H:
        start = T0 + ((t - T0) // CYCLE) * CYCLE
        core.apply_refresh(st, {"anthropic": {"fetched_at": t, "windows": [
            {"label": "Current session", "used_percent": 5 * (t - start) / H, "resets_at": start + CYCLE},
            {"label": "Current week", "used_percent": 40.0, "resets_at": T0 + 30 * 24 * H}]}},
            t, dict(core.DEFAULTS))
        t += cadence_s
    return st["history"]["anthropic"]


def test_run_accepts_a_full_retention_window_recorded_by_the_plugin():
    for cadence in (5 * M + 1, 15 * M + 7):          # real fetch times never align with the cutoff
        out = bt.run(_state_history(9, cadence), [], base_t(), dict(core.DEFAULTS))
        assert "skipped" not in out, (cadence, out.get("skipped"))
    out = bt.run(_state_history(6.5, 15 * M), [], base_t(), dict(core.DEFAULTS))
    assert out["skipped"].startswith("needs 7 days of history")


def test_malformed_history_is_ignored():
    hist = synthetic_week(days=1.0)
    hist["Current session"][5] = ["bad"]
    hist["Nonsense"] = "x"
    res = bt.replay(hist, [], base_t(), dict(core.DEFAULTS))
    assert res and all(r["level"] in ("fast", "constrained") for r in res)


def test_history_from_state_file_is_read_only(tmp_path):
    import json
    st = core.empty_state()
    st["history"] = {"anthropic": synthetic_week(days=1.0)}
    p = tmp_path / "state.json"
    p.write_text(json.dumps(st))
    before = p.stat().st_mtime
    hist, blocks = bt.load_state(p, "anthropic")
    assert set(hist) == {"Current session", "Current week"} and blocks == []
    assert p.stat().st_mtime == before


LADDER = [
    {"name": "codex", "gate": ["openai-codex"], "provider": "openai-codex", "model": "codex-x"},
    {"name": "claude", "gate": ["anthropic"], "provider": "anthropic", "model": "opus-x"},
    {"name": "internal", "provider": "custom:internal", "model": "glm-x"},
]


def test_a_ladder_is_back_tested_once_per_gate_provider(tmp_path, capsys):
    import json
    st = core.empty_state()
    t = T0
    while t < T0 + 7.2 * 24 * H:
        start = T0 + ((t - T0) // CYCLE) * CYCLE
        core.apply_refresh(st, {
            "anthropic": {"fetched_at": t, "windows": [
                {"label": "Current session", "used_percent": 5 * (t - start) / H, "resets_at": start + CYCLE}]},
            "openai-codex": {"fetched_at": t, "windows": [
                {"label": "Session", "used_percent": 22 * (t - start) / H, "resets_at": start + CYCLE}]}},
            t, dict(core.DEFAULTS))
        t += 15 * M
    path = tmp_path / "state.json"
    path.write_text(json.dumps(st))
    assert bt.main(["--state", str(path), "--throttle-json", json.dumps({"ladder": LADDER}), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out) == {"openai-codex", "anthropic"}
    codex, claude = out["openai-codex"]["grid"][0], out["anthropic"]["grid"][0]
    assert codex["constrained_hours"] > 0 and claude["constrained_hours"] == 0   # only codex burns fast
    assert bt.main(["--state", str(path), "--throttle-json", json.dumps({"ladder": LADDER})]) == 0
    md = capsys.readouterr().out
    assert "## Throttle back-test (openai-codex)" in md and "## Throttle back-test (anthropic)" in md
    assert bt.main(["--state", str(path), "--json"]) == 0             # no ladder: one report, as before
    assert "grid" in json.loads(capsys.readouterr().out)
