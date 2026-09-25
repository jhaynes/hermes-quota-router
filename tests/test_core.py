"""Behaviour tests for quota-router core policy. Run: python -m pytest -q tests/"""
import importlib.util
import json
import sys
import threading
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("qr_core", Path(__file__).resolve().parents[1] / "core.py")
core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(core)
CFG = dict(core.DEFAULTS)
H = 3600.0
NOW = 1_800_000_000.0


def snap(*windows, fetched=NOW):
    return {"fetched_at": fetched, "windows": [
        {"label": l, "used_percent": u, "resets_at": r} for (l, u, r) in windows]}


def test_root_home_maps_profile_to_root(tmp_path):
    prof = tmp_path / ".hermes" / "profiles" / "builder"
    assert core.root_home(prof) == tmp_path / ".hermes"
    assert core.root_home(tmp_path / ".hermes") == tmp_path / ".hermes"
    assert core.state_path(prof) == core.state_path(tmp_path / ".hermes")


def test_tiers_and_mode_follow_usage():
    st = core.empty_state()
    core.apply_refresh(st, {"anthropic": snap(("week", 30, NOW + 5 * 24 * H)),
                            "openai-codex": snap(("week", 100, NOW + 10 * H))}, NOW, CFG)
    assert st["providers"]["anthropic"]["tier"] == "ok"
    assert st["providers"]["openai-codex"]["tier"] == "exhausted"
    assert st["mode"] == "shift"
    core.apply_refresh(st, {"anthropic": snap(("week", 88, NOW + 5 * 24 * H), fetched=NOW + 60)},
                       NOW + 60, CFG)
    assert st["providers"]["anthropic"]["tier"] == "tight"
    assert st["mode"] == "internal"


def test_hysteresis_no_flapping():
    st = core.empty_state()
    lines = []
    t = NOW
    for i, used in enumerate([86, 84, 86, 84, 80, 86]):
        t = NOW + i * 60
        lines += core.apply_refresh(st, {"anthropic": snap(("week", used, NOW + 99 * H), fetched=t)}, t, CFG)
    transitions = [l for l in lines if "anthropic ok → tight" in l or "tight → ok" in l]
    assert transitions == ["quota-router: anthropic ok → tight"]


def test_deescalation_waits_for_dwell_then_leaves_below_leave_threshold():
    st = core.empty_state()
    core.apply_refresh(st, {"anthropic": snap(("week", 90, NOW + 99 * H))}, NOW, CFG)
    assert st["providers"]["anthropic"]["tier"] == "tight"
    core.apply_refresh(st, {"anthropic": snap(("week", 10, NOW + 99 * H), fetched=NOW + 60)}, NOW + 60, CFG)
    assert st["providers"]["anthropic"]["tier"] == "tight"  # dwell
    t = NOW + CFG["min_dwell_s"] + 1
    core.apply_refresh(st, {"anthropic": snap(("week", 10, NOW + 99 * H), fetched=t)}, t, CFG)
    assert st["providers"]["anthropic"]["tier"] == "ok"


def test_window_resetting_soon_cannot_escalate():
    st = core.empty_state()
    core.apply_refresh(st, {"anthropic": snap(("5h", 95, NOW + 10 * 60))}, NOW, CFG)
    assert st["providers"]["anthropic"]["tier"] == "ok"


def test_stale_snapshot_fails_open():
    st = core.empty_state()
    core.apply_refresh(st, {"anthropic": snap(("week", 99, NOW + 99 * H)),
                            "openai-codex": snap(("week", 99, NOW + 99 * H))}, NOW, CFG)
    assert st["mode"] == "internal"
    later = NOW + CFG["stale_after_s"] + 5
    core.recompute(st, later, CFG)
    assert st["mode"] == "normal"


def test_observed_block_escalates_expires_at_reset_and_clears_on_success():
    st = core.empty_state()
    core.apply_refresh(st, {"openai-codex": snap(("week", 60, NOW + 5 * H))}, NOW, CFG)
    line = core.record_error(st, provider="openai-codex", status_code=429, reason="rate_limit",
                             message="You've hit your usage limit.", model="gpt", now=NOW, cfg=CFG)
    assert line and "block" in line
    core.recompute(st, NOW, CFG)
    assert st["providers"]["openai-codex"]["tier"] == "exhausted"
    assert st["blocks"]["openai-codex"]["until"] == NOW + 5 * H
    # Duplicate report is quiet.
    assert core.record_error(st, provider="openai-codex", status_code=429, reason="rate_limit",
                             message="usage limit", model="gpt", now=NOW + 1, cfg=CFG) is None
    # Success clears it, and the tier returns to the usage-implied tier without waiting out dwell.
    assert core.clear_block_on_success(st, "openai-codex", NOW + 2)
    assert "openai-codex" not in st["blocks"]
    core.recompute(st, NOW + 2, CFG)
    assert st["providers"]["openai-codex"]["tier"] == "ok"


def test_block_without_reset_uses_ttl_and_expires():
    st = core.empty_state()
    core.record_error(st, provider="anthropic", status_code=429, reason="billing", message="x",
                      model="m", now=NOW, cfg=CFG)
    assert st["blocks"]["anthropic"]["until"] == NOW + CFG["block_ttl_s"]
    core.recompute(st, NOW + CFG["block_ttl_s"] + 1, CFG)
    assert "anthropic" not in st["blocks"]


def test_transient_rpm_429_and_internal_endpoint_do_not_block():
    st = core.empty_state()
    assert core.record_error(st, provider="anthropic", status_code=429, reason="rate_limit",
                             message="Too many requests, please slow down", model="m", now=NOW, cfg=CFG) is None
    assert core.record_error(st, provider="custom", status_code=429, reason="rate_limit",
                             message="usage limit", model="glm", now=NOW, cfg=CFG) is None
    assert st["blocks"] == {}
    assert len(st["errors"]) == 2  # still logged for diagnosis
    assert core.record_error(st, provider="anthropic", status_code=500, reason="server_error",
                             message="boom", model="m", now=NOW, cfg=CFG) is None
    assert len(st["errors"]) == 2


def test_alerts_fire_once_per_threshold_per_cycle_and_rearm_after_reset():
    st = core.empty_state()
    reset = NOW + 2 * H
    l1 = core.apply_refresh(st, {"anthropic": snap(("week", 91, reset))}, NOW, CFG)
    assert sum("≥75%" in l for l in l1) == 1 and sum("≥90%" in l for l in l1) == 1
    l2 = core.apply_refresh(st, {"anthropic": snap(("week", 92, reset), fetched=NOW + 60)}, NOW + 60, CFG)
    assert not [l for l in l2 if "⚠️" in l]
    nxt = reset + 7 * 24 * H
    l3 = core.apply_refresh(st, {"anthropic": snap(("week", 80, nxt), fetched=reset + 60)}, reset + 60, CFG)
    assert sum("≥75%" in l for l in l3) == 1


def test_real_usage_json_shape_normalizes():
    real = {"provider": "anthropic", "fetched_at": "2026-09-25T20:19:52.697578+00:00", "windows": [
        {"label": "Current session", "used_percent": 34.0, "resets_at": "2026-09-25T20:19:59.631758+00:00"},
        {"label": "Current week", "used_percent": 30.0, "resets_at": "2026-10-01T09:59:59.631786+00:00"}]}
    n = core.normalize_snapshot(real, NOW)
    assert [w["used"] for w in n["windows"]] == [34.0, 30.0]
    assert all(isinstance(w["resets_at"], float) for w in n["windows"])


def test_locked_state_is_safe_under_concurrency(tmp_path):
    path = tmp_path / "cache" / "quota-router" / "state.json"

    def bump(i):
        with core.locked_state(path) as st:
            st["errors"].append({"i": i})

    ts = [threading.Thread(target=bump, args=(i,)) for i in range(20)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(json.loads(path.read_text())["errors"]) == 20


def test_corrupt_state_file_recovers(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json")
    assert core.read_state(path)["mode"] == "normal"
