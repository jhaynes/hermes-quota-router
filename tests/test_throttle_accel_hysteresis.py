"""Rule (c) hysteresis: an acceleration entry must not be undone while the acceleration persists.

Rule (c) enters on ``r_short >= accel_ratio * r_long``; leaving only checked the short projection
and dwell, which an acceleration entry never trips. So once dwell expired the throttle left on the
same data that put it in, and re-entered at the next refresh. The retained 2026-10-09 anthropic
history reproduces this: six level changes in under three hours, each a full-context cache miss.
"""
import importlib.util
import json
from pathlib import Path

import pytest

_root = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("qr_accel_hysteresis", _root / "core.py")
assert _spec is not None and _spec.loader is not None
core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(core)

NOW = 1_800_000_000.0
H = 3600.0
FIXTURE = json.loads((_root / "tests" / "fixtures" / "accel_flap_anthropic_20261009.json").read_text())


def win(r_long, r_short, long=110.0, short=50.0, resets_in=5 * 24 * H, allowance=None):
    w = {"label": "Current week", "used": 27.0, "resets_at": NOW + resets_in,
         "forecast": {"status": "ok", "rate_pct_h": r_long, "r_short": r_short,
                      "projected_at_reset": long, "projected_short": short}}
    if allowance is not None:
        w["projection_allowance"] = allowance
    return w


T = core.throttle_config({})
PAST_DWELL = NOW - T["min_dwell_s"]


def test_acceleration_entry_is_rule_c():
    assert core.throttle_policy("fast", 0.0, [win(0.65, 0.99)], NOW, T) == ("constrained", "c", "Current week")


@pytest.mark.parametrize("ratio", [1.5, 1.7, 1.3])
def test_does_not_leave_while_acceleration_persists(ratio):
    """Ratios at/above the entry threshold, and inside the hysteresis band, keep the level."""
    got = core.throttle_policy("constrained", PAST_DWELL, [win(1.0, ratio)], NOW, T)
    assert got == ("constrained", "hold_accel", "Current week")


def test_leaves_once_acceleration_falls_below_the_leave_ratio():
    assert core.throttle_policy("constrained", PAST_DWELL, [win(1.0, 1.24)], NOW, T) == ("fast", "leave", None)


def test_leaves_when_long_projection_falls_below_early_threshold():
    w = win(1.0, 1.7, long=T["early_projected"] - 0.01)
    assert core.throttle_policy("constrained", PAST_DWELL, [w], NOW, T) == ("fast", "leave", None)


def test_a_window_about_to_reset_does_not_hold():
    """Mirrors entry: a window resetting within ignore_reset_within_s cannot keep the level."""
    w = win(1.0, 1.7, resets_in=T["ignore_reset_within_s"] - 1)
    assert core.throttle_policy("constrained", PAST_DWELL, [w], NOW, T) == ("fast", "leave", None)


def test_hold_respects_projection_allowance_like_entry():
    """With a banked-refill allowance, rule (c) also needs the short projection; so does the hold."""
    t = core.throttle_config({"leave_projected": 95.0})   # below hold_projected's 195, above c's 185
    low = win(1.0, 1.7, long=200.0, short=100.0, allowance=100.0)
    high = win(1.0, 1.7, long=200.0, short=190.0, allowance=100.0)
    assert core.throttle_policy("constrained", PAST_DWELL, [low], NOW, t) == ("fast", "leave", None)
    assert core.throttle_policy("constrained", PAST_DWELL, [high], NOW, t)[1] == "hold_accel"


def test_leave_ratio_is_configurable_and_capped_at_the_entry_ratio():
    loose = core.throttle_config({"leave_accel_ratio": 1.1})
    assert core.throttle_policy("constrained", PAST_DWELL, [win(1.0, 1.2)], NOW, loose)[1] == "hold_accel"
    # A leave ratio above the entry ratio would recreate the gap; it is capped at accel_ratio.
    inverted = core.throttle_config({"leave_accel_ratio": 3.0})
    assert core.throttle_policy("constrained", PAST_DWELL, [win(1.0, 1.5)], NOW, inverted)[1] == "hold_accel"


def test_dwell_still_applies_before_the_hysteresis_check_matters():
    assert core.throttle_policy("constrained", NOW - 60, [win(1.0, 1.0)], NOW, T) == ("constrained", "dwell", None)


def _replay(user):
    cfg = dict(core.DEFAULTS)
    t = core.throttle_config(user)
    st = core.empty_state()
    prev, changes = None, []
    for at, used, resets_at in FIXTURE["points"]:
        core.apply_refresh(st, {FIXTURE["provider"]: {"fetched_at": at, "windows": [
            {"label": FIXTURE["label"], "used_percent": used, "resets_at": resets_at}]}}, at, cfg)
        d = core.throttle_step(st, at, cfg, t)
        if d["level"] != prev:
            changes.append((at, d["level"], d["rule"]))
            prev = d["level"]
    return changes


SPEC = {"provider": "anthropic", "model": "premium", "fallback": []}
FLOOR = {"provider": "openai-codex", "model": "other", "fallback": []}


@pytest.mark.parametrize("user", [
    {"levels": {"fast": SPEC, "constrained": FLOOR}},
    {"leave_projected": 95.0, "ladder": [{"name": "claude", "gate": ["anthropic"], **SPEC},
                                         {"name": "codex", **FLOOR}]},
], ids=["legacy", "live-ladder"])
def test_retained_history_enters_once_instead_of_flapping(user):
    last = FIXTURE["points"][-1][0]
    window = [c for c in _replay(user) if c[0] >= last - 4 * H]
    # Unfixed: c/leave/c/leave/c/leave/c between 13:01 and 15:34. Fixed: one entry, held.
    assert len(window) <= 2, window
    assert window[-1][1] != "fast" if window else True
