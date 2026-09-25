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


def test_old_state_file_without_pending_still_loads(tmp_path):
    path = tmp_path / "state.json"
    old = core.empty_state()
    old.pop("pending")
    path.write_text(json.dumps(old))
    st = core.read_state(path)
    assert st["pending"] == []


def test_manual_refresh_does_not_swallow_alerts():
    st = core.empty_state()
    core.apply_refresh(st, {"anthropic": snap(("Current week", 80.0, NOW + 3 * 24 * H))}, NOW, CFG)
    assert any("≥75%" in l for l in st["pending"])
    drained = core.drain_pending(st)
    assert any("≥75%" in l for l in drained) and st["pending"] == []
    core.apply_refresh(st, {"anthropic": snap(("Current week", 81.0, NOW + 3 * 24 * H))}, NOW + 60, CFG)
    assert not any("≥75%" in l for l in st["pending"])


def test_pending_is_bounded():
    st = core.empty_state()
    for i in range(80):
        core.queue(st, f"line {i}")
    assert len(st["pending"]) == 50 and st["pending"][-1] == "quota-router: line 79"


def test_queue_prefixes_non_deliverable_lines():
    st = core.empty_state()
    core.queue(st, "hello")
    assert st["pending"] == ["quota-router: hello"]


def test_queue_dedupes_pending_lines():
    st = core.empty_state()
    core.queue(st, "⚠️ same", "⚠️ same")
    core.queue(st, "⚠️ same")
    assert st["pending"] == ["⚠️ same"]


def test_queue_ignores_empty_lines():
    st = core.empty_state()
    core.queue(st, "", None, "quota-router: ok")
    assert st["pending"] == ["quota-router: ok"]


def test_queue_evicts_non_alert_lines_before_alert_lines():
    st = core.empty_state()
    core.queue(st, "⚠️ alert 0")
    for i in range(1, 60):
        core.queue(st, f"quota-router: transition {i}")
    assert "⚠️ alert 0" in st["pending"]
    assert len(st["pending"]) == 50


def test_fetch_failure_recorded_no_alert_before_stale_then_one_alert_then_recover():
    st = core.empty_state()
    core.note_fetch(st, "anthropic", ok=False, kind="fetch_error", message="timeout", now=NOW, cfg=CFG)
    err = st["providers"]["anthropic"]["fetch"]["core"]["error"]
    assert err["kind"] == "fetch_error" and err["since"] == NOW
    assert not st["pending"]
    core.note_fetch(st, "anthropic", ok=False, kind="fetch_error", message="timeout",
                    now=NOW + 10 * 60, cfg=CFG)
    assert not st["pending"]
    core.note_fetch(st, "anthropic", ok=False, kind="fetch_error", message="timeout",
                    now=NOW + 31 * 60, cfg=CFG)
    assert sum("usage unavailable" in l for l in st["pending"]) == 1
    core.note_fetch(st, "anthropic", ok=False, kind="fetch_error", message="timeout",
                    now=NOW + 46 * 60, cfg=CFG)
    assert sum("usage unavailable" in l for l in st["pending"]) == 1
    core.drain_pending(st)
    core.note_fetch(st, "anthropic", ok=True, now=NOW + 60 * 60, cfg=CFG)
    assert st["providers"]["anthropic"]["fetch"]["core"]["error"] is None
    assert st["providers"]["anthropic"]["fetch"]["core"]["last_ok_at"] == NOW + 60 * 60
    assert sum("recovered" in l for l in st["pending"]) == 1


def test_kind_change_mid_incident_keeps_since_and_does_not_realert():
    st = core.empty_state()
    core.note_fetch(st, "anthropic", ok=False, kind="fetch_error", message="a", now=NOW, cfg=CFG)
    core.note_fetch(st, "anthropic", ok=False, kind="unavailable", message="b",
                    now=NOW + 31 * 60, cfg=CFG)
    err = st["providers"]["anthropic"]["fetch"]["core"]["error"]
    assert err["since"] == NOW and err["kind"] == "unavailable"
    assert sum("usage unavailable" in l for l in st["pending"]) == 1
    core.note_fetch(st, "anthropic", ok=False, kind="unavailable", message="c",
                    now=NOW + 45 * 60, cfg=CFG)
    assert sum("usage unavailable" in l for l in st["pending"]) == 1


def test_note_fetch_persistence_across_reload(tmp_path):
    path = tmp_path / "state.json"
    with core.locked_state(path) as st:
        core.note_fetch(st, "anthropic", ok=False, kind="fetch_error", message="x", now=NOW, cfg=CFG)
    with core.locked_state(path) as st:
        core.note_fetch(st, "anthropic", ok=False, kind="fetch_error", message="x",
                        now=NOW + 31 * 60, cfg=CFG)
    st = core.read_state(path)
    assert sum(l.startswith("quota-router: anthropic usage unavailable") for l in st["pending"]) == 1
    with core.locked_state(path) as st:
        core.note_fetch(st, "anthropic", ok=False, kind="fetch_error", message="x",
                        now=NOW + 45 * 60, cfg=CFG)
    st = core.read_state(path)
    assert sum(l.startswith("quota-router: anthropic usage unavailable") for l in st["pending"]) == 1


def test_scoped_slot_never_alerts_and_is_independent_of_core():
    st = core.empty_state()
    core.note_fetch(st, "anthropic", ok=False, kind="scoped_error", message="x",
                    now=NOW, cfg=CFG, slot="scoped")
    core.note_fetch(st, "anthropic", ok=False, kind="scoped_error", message="x",
                    now=NOW + 200 * 60, cfg=CFG, slot="scoped")
    assert not st["pending"]
    assert st["providers"]["anthropic"]["fetch"]["scoped"]["error"] is not None
    assert st["providers"]["anthropic"]["fetch"].get("core") is None


def test_render_status_shows_core_and_scoped_fetch_errors():
    st = core.empty_state()
    core.note_fetch(st, "openai-codex", ok=False, kind="unavailable", message="no token", now=NOW, cfg=CFG)
    core.note_fetch(st, "openai-codex", ok=False, kind="scoped_error", message="boom",
                    now=NOW, cfg=CFG, slot="scoped")
    out = core.render_status(st, NOW + 60, CFG)
    assert "last fetch failed" in out and "unavailable: no token" in out
    assert "scoped-limits fetch failed" in out and "scoped_error: boom" in out


def test_render_status_renders_old_state_without_fetch_key():
    st = core.empty_state()
    core.apply_refresh(st, {"anthropic": snap(("week", 10, NOW + 5 * H))}, NOW, CFG)
    st["providers"]["anthropic"].pop("fetch", None)
    out = core.render_status(st, NOW, CFG)
    assert "anthropic" in out


def test_tier_unaffected_by_fetch_failure_with_stale_snapshot():
    st = core.empty_state()
    core.apply_refresh(st, {"anthropic": snap(("week", 30, NOW + 5 * 24 * H), fetched=NOW)}, NOW, CFG)
    later = NOW + CFG["stale_after_s"] + 5
    core.note_fetch(st, "anthropic", ok=False, kind="fetch_error", message="x", now=later, cfg=CFG)
    core.recompute(st, later, CFG)
    assert st["providers"]["anthropic"]["tier"] == "ok"


def test_refresh_drain_interleave_never_loses_or_duplicates(tmp_path, monkeypatch):
    path = tmp_path / "cache" / "quota-router" / "state.json"
    results = {"drained": [], "errors": []}
    lock = threading.Lock()

    def do_refresh(i, drain):
        try:
            with core.locked_state(path) as st:
                lines = core.apply_refresh(
                    st, {"anthropic": snap((f"w{i}", 76.0 + (i % 5), NOW + (100 + i) * H))},
                    NOW + i, CFG)
                if drain:
                    drained = core.drain_pending(st)
                    with lock:
                        results["drained"].extend(drained)
        except Exception as exc:  # pragma: no cover - surfaced via assertion
            with lock:
                results["errors"].append(exc)

    ts = [threading.Thread(target=do_refresh, args=(i, i % 2 == 0)) for i in range(30)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not results["errors"]
    assert len(results["drained"]) == len(set(results["drained"]))
    final = json.loads(path.read_text())
    alerts = lambda ls: {l for l in ls if l.startswith("⚠️")}
    delivered, left = alerts(results["drained"]), alerts(final["pending"])
    assert not (delivered & left)                      # nothing both delivered and still queued
    assert len(delivered | left) == 30                 # one ≥75% alert per thread's window, none lost
