"""Sawtooth regression: an old burst inside a weekly window's short lookback must not
extrapolate across the whole remaining cycle and pin the router (RED → GREEN)."""
import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("qr_core_saw", Path(__file__).resolve().parents[1] / "core.py")
assert _spec is not None and _spec.loader is not None
core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(core)
CFG = dict(core.DEFAULTS)
WEEKLY = core.DEFAULTS["forecast_kinds"]["weekly"]
NOW = 1_800_000_000.0
H = 3600.0


def series(start, step_s, values, resets_at):
    return [[start + i * step_s, float(v), resets_at] for i, v in enumerate(values)]


def weekly_window(f):
    return {"label": "Weekly", "used": f["used"], "resets_at": f["resets_at"], "forecast": f}


def live_shaped_history():
    """The Oct 4 2026 incident: +4.1%/week morning burn (21.9→26 over 6h), flat ever
    since (2.7h), weekly reset 143.5h out. r_short stays 0.74%/h until the burn ages
    out of the 6h lookback; extrapolated across 143h it projects 132% → pin."""
    reset = NOW + 143.5 * H
    ramp = series(NOW - 6 * H, 30 * 60.0, [21.9 + i * (4.1 / 12) for i in range(13)], reset)
    flat = series(NOW - 2.7 * H, 30 * 60.0, [26.0] * 6, reset)
    return ramp + flat


def test_uncapped_code_pinned_the_router_this_repro_shapes_the_incident():
    """Documents the incident shape: +4.1%/week morning burn, flat after, 143h tail.
    Pre-fix, projected_short extrapolated r_short across the whole tail (132%) and the
    policy pinned both directions. Kept as a shape check on r_short itself."""
    f = core.forecast(live_shaped_history(), NOW, WEEKLY, CFG)
    assert f["status"] == "ok"
    assert f["r_short"] == pytest.approx(0.74, abs=0.1)
    assert f["used"] == pytest.approx(26.0)


def test_short_projection_is_capped_at_the_short_horizon():
    """GREEN target: projected_short extrapolates r_short no further than
    ``forecast_short_horizon_s`` (default 24h) ahead, so a decaying burst cannot
    claim the whole 143h tail. 26 + 0.74 × 24 ≈ 44 — under every threshold."""
    assert CFG["forecast_short_horizon_s"] == 24 * H
    f = core.forecast(live_shaped_history(), NOW, WEEKLY, CFG)
    cap_h = CFG["forecast_short_horizon_s"] / H
    assert f["projected_short"] == pytest.approx(min(max(f["used"] + f["r_short"] * cap_h, f["used"]), 200.0), abs=0.5)
    assert f["projected_short"] < 80
    t = core.throttle_config({})
    lvl, rule, _ = core.throttle_policy("fast", 0.0, [weekly_window(f)], NOW, t)
    assert (lvl, rule) == ("fast", None)
    lvl, rule, _ = core.throttle_policy("constrained", 0.0, [weekly_window(f)], NOW, t)
    assert (lvl, rule) == ("fast", "leave")


def test_cap_never_shortens_a_genuinely_short_tail():
    """Session windows (5h cycle) always have tails shorter than the 24h cap, so their
    short projections — and every existing session forecast — are unchanged."""
    reset = NOW + 2 * H
    pts = series(NOW - H, 5 * 60.0, [30 + i for i in range(13)], reset)
    f = core.forecast(pts, NOW, core.DEFAULTS["forecast_kinds"]["session"], CFG)
    assert f["status"] == "ok"
    assert f["r_short"] == pytest.approx(12.0, abs=0.01)
    assert f["projected_short"] == pytest.approx(42 + 12 * 2, abs=0.05)


def test_steady_burn_waits_until_used_climbs_then_rides_the_sawtooth():
    """A steady 1%/h weekly burn caps projected_short at used + 24 — rule a waits until
    used nears the leave floor (~71%) instead of pinning from hour one. Rule b still
    fires on raw used ≥ enter_used, so genuine heavy usage always constrains."""
    period = WEEKLY["period_s"]
    start = NOW - 2 * 24 * H
    reset = start + period
    pts = series(start, 30 * 60.0, [i * 0.5 for i in range(2 * 48 + 1)], reset)  # 48%, steady 1%/h
    f = core.forecast(pts, NOW, WEEKLY, CFG)
    assert f["status"] == "ok"
    assert f["projected_at_reset"] > 95          # cycle-average still extrapolates fully
    assert f["projected_short"] < 80             # capped short: 48 + 1 × 24 = 72
    t = core.throttle_config({})
    lvl, rule, _ = core.throttle_policy("fast", 0.0, [weekly_window(f)], NOW, t)
    assert (lvl, rule) == ("fast", None)         # ride OpenAI: 70% weekly headroom
    f2 = dict(f, used=86.0)
    lvl, rule, _ = core.throttle_policy("fast", 0.0, [weekly_window(f2)], NOW, t)
    assert (lvl, rule) == ("constrained", "b")   # heavy usage still constrains immediately


@pytest.mark.parametrize("current, expected_rule", [("fast", None), ("constrained", "leave")])
def test_exact_retained_weekly_samples_do_not_pin_throttle(current, expected_rule):
    """Real retained incident samples projected 122.25% on the parent and blocked
    both staying fast and recovery; replay unchanged numbers at their stored clock.
    This pins actual observations, not the synthetic incident shape above.
    """
    import json
    fixture = json.loads((Path(__file__).parent / "fixtures" / "sawtooth_weekly_20261004.json").read_text())
    f = core.forecast(fixture["points"], fixture["now"], WEEKLY, CFG)
    assert f["status"] == "ok"
    assert f["used"] == 26.0
    assert f["r_short"] == pytest.approx(0.6712217047634567)
    assert f["projected_short"] == pytest.approx(42.10932091432296)
    assert f["projected_at_reset"] == pytest.approx(177.55347140512418)
    level, rule, _ = core.throttle_policy(current, 0.0, [weekly_window(f)], fixture["now"], core.throttle_config({}))
    assert (level, rule) == ("fast", expected_rule)


@pytest.mark.parametrize("horizon_h", [0, 6, 24, 200])
def test_configured_horizon_limits_only_short_projection(horizon_h):
    """Custom horizons and zero are supported; a horizon beyond reset must not
    overshoot it, while the cycle-average projection must remain uncapped.
    """
    cfg = dict(CFG, forecast_short_horizon_s=horizon_h * H)
    f = core.forecast(live_shaped_history(), NOW, WEEKLY, cfg)
    hours = min(horizon_h, 143.5)
    assert f["projected_short"] == pytest.approx(min(f["used"] + max(f["r_short"], 0) * hours, 200))
    assert f["projected_at_reset"] == pytest.approx(min(f["used"] + max(f["rate_pct_h"], 0) * 143.5, 200))
