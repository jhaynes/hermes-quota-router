"""Usage history + fetch-outcome ring (plan Task 1; v2 "fetch-outcome ring", B5 cycle keying)."""
import importlib.util
import json
from pathlib import Path

_spec = importlib.util.spec_from_file_location("qr_core_hist", Path(__file__).resolve().parents[1] / "core.py")
core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(core)
CFG = dict(core.DEFAULTS)
H = 3600.0
NOW = 1_800_000_000.0
WEEK_RESET = NOW + 3 * 24 * H


def snap(*windows, fetched=NOW):
    return {"fetched_at": fetched, "windows": [
        {"label": l, "used_percent": u, "resets_at": r} for (l, u, r) in windows]}


def hist(st, label, provider="anthropic"):
    return st["history"][provider][label]


def test_refresh_appends_one_point_per_core_window():
    st = core.empty_state()
    core.apply_refresh(st, {"anthropic": snap(("Current week", 40.0, WEEK_RESET))}, NOW, CFG)
    core.apply_refresh(st, {"anthropic": snap(("Current week", 41.5, WEEK_RESET), fetched=NOW + 300)},
                       NOW + 300, CFG)
    assert hist(st, "Current week") == [[NOW, 40.0, WEEK_RESET], [NOW + 300, 41.5, WEEK_RESET]]


def test_scoped_and_stale_windows_are_not_recorded():
    st = core.empty_state()
    s = snap(("Current week", 40.0, WEEK_RESET))
    s["windows"] += [{"label": "Fable week (model)", "used_percent": 10.0, "resets_at": WEEK_RESET,
                      "scope": "Fable"},
                     {"label": "Old", "used_percent": 10.0, "resets_at": WEEK_RESET, "stale": True}]
    core.apply_refresh(st, {"anthropic": s}, NOW, CFG)
    assert set(st["history"]["anthropic"]) == {"Current week"}


def test_same_or_older_fetch_time_is_not_appended_twice():
    st = core.empty_state()
    s = snap(("Current week", 40.0, WEEK_RESET))
    core.apply_refresh(st, {"anthropic": s}, NOW, CFG)
    core.apply_refresh(st, {"anthropic": s}, NOW + 60, CFG)  # status --refresh re-folding the same snapshot
    core.apply_refresh(st, {"anthropic": snap(("Current week", 39.0, WEEK_RESET), fetched=NOW - 5)},
                       NOW + 61, CFG)
    assert hist(st, "Current week") == [[NOW, 40.0, WEEK_RESET]]


def test_history_is_capped_by_age_and_count():
    st = core.empty_state()
    for i in range(core.HISTORY_MAX_POINTS + 10):
        t = NOW + i
        core.append_history(st, "anthropic",
                            core.normalize_snapshot(snap(("Current week", 1.0, WEEK_RESET), fetched=t), t), t)
    pts = hist(st, "Current week")
    assert len(pts) == core.HISTORY_MAX_POINTS
    assert pts[-1][0] == NOW + core.HISTORY_MAX_POINTS + 9
    later = NOW + core.HISTORY_MAX_AGE_S + core.HISTORY_MAX_POINTS + 5
    core.append_history(st, "anthropic", core.normalize_snapshot(
        snap(("Current week", 2.0, later + H), fetched=later), later), later)
    pts = hist(st, "Current week")
    assert all(p[0] >= later - core.HISTORY_MAX_AGE_S for p in pts)
    assert pts[-1] == [later, 2.0, later + H]


def test_reset_starts_a_new_cycle_without_deleting_the_previous_one():
    st = core.empty_state()
    r1, r2 = NOW + 2 * H, NOW + 7 * H
    core.apply_refresh(st, {"anthropic": snap(("Current session", 70.0, r1))}, NOW, CFG)
    core.apply_refresh(st, {"anthropic": snap(("Current session", 72.0, r1 + 30), fetched=NOW + 300)},
                       NOW + 300, CFG)  # resets_at jitter within tolerance: same cycle
    core.apply_refresh(st, {"anthropic": snap(("Current session", 3.0, r2), fetched=NOW + 2.1 * H)},
                       NOW + 2.1 * H, CFG)
    pts = hist(st, "Current session")
    assert len(pts) == 3  # previous cycle kept; pruning is by age only
    assert core.current_cycle(pts, CFG) == [[NOW + 2.1 * H, 3.0, r2]]


def test_null_reset_to_value_starts_a_new_cycle():
    pts = [[NOW, 0.0, None], [NOW + 300, 0.0, None], [NOW + 600, 5.0, NOW + 5 * H]]
    assert core.current_cycle(pts, CFG) == [[NOW + 600, 5.0, NOW + 5 * H]]
    assert core.current_cycle(pts[:2], CFG) == pts[:2]
    assert core.current_cycle([], CFG) == []


def test_malformed_history_is_repaired_not_fatal():
    st = core.empty_state()
    st["history"] = {"anthropic": {"Current week": [["x", 1, 2], [NOW - 60, 39.0, WEEK_RESET], "junk",
                                                    [NOW - 30, None, WEEK_RESET], [NOW - 20, 1.0]],
                                   "Other": "not-a-list"},
                     "openai-codex": []}
    core.apply_refresh(st, {"anthropic": snap(("Current week", 40.0, WEEK_RESET))}, NOW, CFG)
    assert hist(st, "Current week") == [[NOW - 60, 39.0, WEEK_RESET], [NOW, 40.0, WEEK_RESET]]
    st["history"] = "garbage"
    core.apply_refresh(st, {"anthropic": snap(("Current week", 41.0, WEEK_RESET), fetched=NOW + 1)},
                       NOW + 1, CFG)
    assert hist(st, "Current week") == [[NOW + 1, 41.0, WEEK_RESET]]


def test_state_file_without_history_loads(tmp_path):
    path = tmp_path / "state.json"
    old = core.empty_state()
    old.pop("history", None)
    old.pop("fetch_log", None)
    path.write_text(json.dumps(old))
    st = core.read_state(path)
    assert st["history"] == {} and st["fetch_log"] == []
    core.apply_refresh(st, {"anthropic": snap(("Current week", 40.0, WEEK_RESET))}, NOW, CFG)
    assert hist(st, "Current week") == [[NOW, 40.0, WEEK_RESET]]


def test_fetch_outcome_ring_records_core_fetches_only():
    st = core.empty_state()
    core.note_fetch(st, "anthropic", ok=True, now=NOW, cfg=CFG)
    core.note_fetch(st, "openai-codex", ok=False, kind="no_data", message="x", now=NOW + 1, cfg=CFG)
    core.note_fetch(st, "anthropic", slot="scoped", ok=False, kind="scoped_error", now=NOW + 2, cfg=CFG)
    assert st["fetch_log"] == [[NOW, "anthropic", "ok"], [NOW + 1, "openai-codex", "no_data"]]


def test_fetch_outcome_ring_is_capped_by_age_and_count():
    st = core.empty_state()
    for i in range(core.FETCH_LOG_MAX + 5):
        core.note_fetch(st, "anthropic", ok=True, now=NOW + i, cfg=CFG)
    assert len(st["fetch_log"]) == core.FETCH_LOG_MAX
    later = NOW + core.FETCH_LOG_MAX_AGE_S + core.FETCH_LOG_MAX + 10
    core.note_fetch(st, "anthropic", ok=False, kind="fetch_error", now=later, cfg=CFG)
    assert st["fetch_log"] == [[later, "anthropic", "fetch_error"]]


def test_malformed_fetch_log_is_repaired():
    st = core.empty_state()
    st["fetch_log"] = {"bad": 1}
    core.note_fetch(st, "anthropic", ok=True, now=NOW, cfg=CFG)
    assert st["fetch_log"] == [[NOW, "anthropic", "ok"]]
