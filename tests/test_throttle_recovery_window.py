"""Banked weekly headroom is usable only under the existing auto-reset guards.

Synthetic rate shape resembling the incident, NOT retained incident evidence.
Default policy remains unchanged; enabling auto_reset admits at most one future
weekly refill to projection rules, without changing actual-usage or block guards.
"""
import copy
import importlib.util
import json
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("qr_core_recover", Path(__file__).resolve().parents[1] / "core.py")
assert _spec is not None and _spec.loader is not None
core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(core)
NOW = 1_800_000_000.0
H = 3600.0
CFG = dict(core.DEFAULTS, auto_reset={"weekly_threshold": 97.0})
T = core.throttle_config({"ladder": [
    {"name": "codex", "gate": ["openai-codex"], "provider": "openai-codex", "model": "codex"},
    {"name": "internal", "provider": "custom:internal", "model": "internal"},
]})


def incident_shape():
    st = core.empty_state()
    reset = NOW + 152.5 * H
    core.apply_refresh(st, {"openai-codex": {
        "fetched_at": NOW, "details": ["You have 2 resets banked - use /usage reset to activate"],
        "windows": [{"label": "Weekly", "used_percent": 31.0, "resets_at": reset, "scope": "account"}],
    }}, NOW, CFG)
    # A six-hour burst still inside the short lookback, followed by a plateau.
    st["history"]["openai-codex"]["Weekly"] = [
        [NOW - (24 - i) * 900, 4.0 + i * 27.0 / 23, reset] for i in range(24)
    ] + [[NOW - 600, 31.0, reset], [NOW, 31.0, reset]]
    return st


def weekly(st):
    return st["providers"]["openai-codex"]["snapshot"]["windows"][0]


def decision(st, cfg=CFG, constrained=False, now=NOW):
    if constrained:
        st["throttle"] = {"level": "internal", "providers": {"openai-codex": {
            "state": "constrained", "since": NOW - 7200,
        }}}
    return core.throttle_step(st, now, cfg, T)


def test_unconfigured_reproduces_entry_and_hold():
    cfg = dict(core.DEFAULTS)
    d = decision(incident_shape(), cfg)
    assert (d["level"], d["rule"]) == ("internal", "a")
    d = decision(incident_shape(), cfg, constrained=True)
    assert (d["level"], d["rule"]) == ("internal", "hold_projected")


@pytest.mark.parametrize("constrained", [False, True])
def test_eligible_credit_prevents_projection_only_pin(constrained):
    st = incident_shape()
    original_history = copy.deepcopy(st["history"])
    d = decision(st, constrained=constrained)
    assert (d["level"], d["source"], d["rule"]) == (
        "codex", "policy", "leave" if constrained else None)
    w = d["windows"]["Weekly"]
    assert w["projection_allowance"] == 100.0
    assert w["projected"] == 200.0  # Never rewrite displayed forecasts or history.
    assert 95.0 < w["projected_short"] < 180.0
    assert st["history"] == original_history
    assert "auto_reset" not in st  # Planning never reserves or spends a credit.


@pytest.mark.parametrize("banked", [0, 1, 2, 10])
def test_budget_is_zero_or_one_refill_even_with_large_bank(banked):
    st = incident_shape()
    st["providers"]["openai-codex"]["snapshot"]["details"] = [f"You have {banked} resets banked"]
    wins, _ = core._throttle_windows(st, NOW, CFG, "openai-codex")
    assert wins[0].get("projection_allowance", 0) == (100 if banked else 0)


@pytest.mark.parametrize("redeems", [[NOW - 10], [{"at": NOW - 10, "claim": "in flight"}]])
def test_spent_or_reserved_cap_disables_budget(redeems):
    st = incident_shape()
    st["auto_reset"] = {"redeems": redeems}
    d = decision(st)
    assert (d["level"], d["rule"]) == ("internal", "a")
    assert d["windows"]["Weekly"].get("projection_allowance", 0) == 0


def test_expired_reservation_frees_budget_without_mutating_it():
    st = incident_shape()
    st["auto_reset"] = {"redeems": [{"at": NOW - 8 * 24 * H, "claim": "expired"}]}
    original = copy.deepcopy(st["auto_reset"])
    assert decision(st)["level"] == "codex"
    assert st["auto_reset"] == original


@pytest.mark.parametrize("threshold", [None, "bad", float("nan"), float("inf"), 101.0, -1.0])
def test_unreachable_or_invalid_redemption_threshold_gives_no_budget(threshold):
    d = decision(incident_shape(), dict(CFG, auto_reset={"weekly_threshold": threshold}))
    assert d["level"] == "internal"


@pytest.mark.parametrize("cap", [0, -1, None, "bad"])
def test_disabled_or_invalid_cap_gives_no_budget(cap):
    assert decision(incident_shape(), dict(CFG, auto_reset_max_redeems_per_week=cap))["level"] == "internal"


@pytest.mark.parametrize("problem", ["unavailable", "stale_window", "expired_reset", "missing_reset",
                                    "nan_reset", "infinite_reset", "future_snapshot", "nan_snapshot",
                                    "malformed_details", "model_window"])
def test_invalid_or_unusable_snapshot_gives_no_budget(problem):
    st = incident_shape()
    snap = st["providers"]["openai-codex"]["snapshot"]
    if problem == "unavailable":
        snap["unavailable"] = "fetch failed"
    elif problem == "stale_window":
        weekly(st)["stale"] = True
    elif problem == "expired_reset":
        weekly(st)["resets_at"] = NOW - 1
    elif problem == "missing_reset":
        weekly(st)["resets_at"] = None
    elif problem == "nan_reset":
        weekly(st)["resets_at"] = float("nan")
    elif problem == "infinite_reset":
        weekly(st)["resets_at"] = float("inf")
    elif problem == "future_snapshot":
        snap["fetched_at"] = NOW + 1
    elif problem == "nan_snapshot":
        snap["fetched_at"] = float("nan")
    elif problem == "malformed_details":
        snap["details"] = [None]
    elif problem == "model_window":
        weekly(st)["scope"] = "model"
    before = copy.deepcopy(st.get("auto_reset"))
    wins, _ = core._throttle_windows(st, NOW, CFG, "openai-codex")
    assert all(w.get("projection_allowance", 0) == 0 for w in wins)
    assert st.get("auto_reset") == before


@pytest.mark.parametrize("used,constrained,rule", [(85.0, False, "b"), (100.0, False, "b"),
                                                    (75.0, True, "hold_used")])
def test_real_usage_still_constrains(used, constrained, rule):
    st = incident_shape()
    weekly(st)["used"] = used
    d = decision(st, constrained=constrained)
    assert (d["level"], d["rule"]) == ("internal", rule)


def test_session_exhaustion_never_receives_weekly_allowance():
    st = incident_shape()
    st["providers"]["openai-codex"]["snapshot"]["windows"].append(
        {"label": "Session", "used": 100.0, "resets_at": NOW + 3 * H, "scope": "account"})
    d = decision(st)
    assert (d["level"], d["label"], d["rule"]) == ("internal", "Session", "b")
    assert d["windows"]["Session"].get("projection_allowance", 0) == 0


@pytest.mark.parametrize("kind", ["block", "stale_hold", "stale"])
def test_block_and_stale_precedence_unchanged(kind):
    st = incident_shape()
    if kind == "block":
        st["blocks"]["openai-codex"] = {"until": NOW + H}
    elif kind == "stale_hold":
        st["providers"]["openai-codex"]["snapshot"]["fetched_at"] = NOW - 45 * 60
    else:
        st["providers"]["openai-codex"]["snapshot"]["fetched_at"] = NOW - 2 * H
        weekly(st)["used"] = 90.0
    d = decision(st, constrained=True)
    assert (d["level"], d["source"]) == ("internal", kind)


def test_projection_can_still_exhaust_one_additional_allowance():
    st = incident_shape()
    wins, _ = core._throttle_windows(st, NOW, CFG, "openai-codex")
    f = wins[0]["forecast"]
    f.update(projected_at_reset=200.0, projected_short=195.0)
    assert core.throttle_policy("fast", 0, wins, NOW, T) == ("constrained", "a", "Weekly")
    f.update(projected_short=185.0, rate_pct_h=1.0, r_short=2.0)
    assert core.throttle_policy("fast", 0, wins, NOW, T) == ("constrained", "c", "Weekly")
    f.update(projected_short=180.0)
    assert core.throttle_policy("constrained", 0, wins, NOW, T) == ("fast", "leave", None)
    f.update(projected_short=180.01)
    assert core.throttle_policy("constrained", 0, wins, NOW, T) == ("constrained", "hold_projected", "Weekly")


def test_budget_does_not_shorten_dwell():
    st = incident_shape()
    wins, _ = core._throttle_windows(st, NOW, CFG, "openai-codex")
    assert core.throttle_policy("constrained", NOW - 60, wins, NOW, T) == ("constrained", "dwell", None)


def test_anthropic_recovery_does_not_excuse_codex_without_credit():
    st = incident_shape()
    st["providers"]["anthropic"] = {"snapshot": {"fetched_at": NOW, "windows": [
        {"label": "Current week", "used": 100.0, "resets_at": NOW + 8 * H}]}}
    d = decision(st, dict(core.DEFAULTS))
    assert (d["level"], d["rule"]) == ("internal", "a")


def test_shared_redemption_path_still_requires_actual_weekly_exhaustion():
    st = incident_shape()
    original = copy.deepcopy(st)
    assert core.claim_auto_redeem(st, NOW, CFG)[0] is False
    assert st == original
    weekly(st)["used"] = 100.0
    assert core.claim_auto_redeem(st, NOW, CFG)[0] is True
    assert len(st["auto_reset"]["redeems"]) == 1
    wins, _ = core._throttle_windows(st, NOW, CFG, "openai-codex")
    assert wins[0].get("projection_allowance", 0) == 0


@pytest.mark.parametrize("constrained", [False, True])
def test_retained_live_history_selects_codex_without_manual_hold(constrained):
    # All retained points are passed to production cycle/tolerance filtering, not
    # prefiltered by exact reset timestamp. Clock and snapshot come from one capture.
    fixture = json.loads((Path(__file__).parent / "fixtures" / "codex_banked_reset_retained.json").read_text())
    st = core.empty_state()
    st["providers"]["openai-codex"] = {"snapshot": copy.deepcopy(fixture["snapshot"])}
    st["history"]["openai-codex"] = {"Weekly": copy.deepcopy(fixture["history"])}
    st["auto_reset"] = copy.deepcopy(fixture["auto_reset_state"])
    now = fixture["observation_clock"]
    if constrained:
        st["throttle"] = {"level": "internal", "providers": {"openai-codex": {
            "state": "constrained", "since": now - 7200,
        }}}
    t = dict(T, leave_projected=95.0)  # Actual resolved throttle setting at capture.
    before = copy.deepcopy(st)
    d = core.throttle_step(st, now, CFG, t)
    assert (d["level"], d["source"], d["rule"]) == ("codex", "policy", "leave" if constrained else None)
    assert d["windows"]["Weekly"]["used"] == 33.0
    assert d["windows"]["Weekly"]["projected_short"] == pytest.approx(144.34180850344177)
    assert d["windows"]["Weekly"]["projection_allowance"] == 100.0
    assert st["history"] == before["history"]
    assert st["auto_reset"] == before["auto_reset"]
    assert "hold" not in st["throttle"]
