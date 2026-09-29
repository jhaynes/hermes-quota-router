"""Forecast pure function (plan Task 2, binding v2 B5)."""
import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("qr_core_fc", Path(__file__).resolve().parents[1] / "core.py")
core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(core)
CFG = dict(core.DEFAULTS)
H = 3600.0
M = 60.0
NOW = 1_800_000_000.0
SESSION = core.DEFAULTS["forecast_kinds"]["session"]
WEEKLY = core.DEFAULTS["forecast_kinds"]["weekly"]


def series(start, step_s, values, resets_at):
    return [[start + i * step_s, float(v), resets_at] for i, v in enumerate(values)]


def fc(points, now, kind=SESSION):
    return core.forecast(points, now, kind, CFG)


def test_linear_session_series_matches_hand_least_squares():
    reset = NOW + 3 * H
    # 10%/h for 2 h sampled every 5 min: 30 → 50
    pts = series(NOW - 2 * H, 5 * M, [30 + 10 * i / 12 for i in range(25)], reset)
    f = fc(pts, NOW)
    assert f["status"] == "ok"
    assert f["rate_pct_h"] == pytest.approx(10.0, abs=0.01)
    assert f["used"] == pytest.approx(50.0)
    assert f["projected_at_reset"] == pytest.approx(50 + 10 * 3, abs=0.05)
    assert f["confidence"] == 25
    assert f["accel"] == pytest.approx(0.0, abs=0.01)


def test_flat_series_projects_current_usage():
    pts = series(NOW - H, 5 * M, [40] * 13, NOW + 2 * H)
    f = fc(pts, NOW)
    assert f["rate_pct_h"] == pytest.approx(0.0)
    assert f["projected_at_reset"] == pytest.approx(40.0)


def test_decreasing_series_never_projects_below_current_usage():
    pts = series(NOW - H, 5 * M, [60 - i for i in range(13)], NOW + 2 * H)
    f = fc(pts, NOW)
    assert f["rate_pct_h"] < 0
    assert f["projected_at_reset"] == pytest.approx(48.0)


def test_projection_is_clamped_at_200():
    pts = series(NOW - H, 5 * M, [20 + 5 * i for i in range(13)], NOW + 3.9 * H)
    assert fc(pts, NOW)["projected_at_reset"] == 200.0


def test_too_few_points_or_too_short_span_is_unknown():
    reset = NOW + 3 * H
    three = series(NOW - H, 30 * M, [30, 35, 40], reset)
    assert fc(three, NOW)["status"] == "unknown"
    short = series(NOW - 35 * M, 5 * M, [30, 31, 32, 33, 34, 35, 36, 37], reset)  # 35 min span
    f = fc(short, NOW)
    assert f["status"] == "unknown" and "span" in f["reason"]


def test_low_usage_is_unknown():
    pts = series(NOW - 2 * H, 5 * M, [5 + i * 0.5 for i in range(25)], NOW + 3 * H)
    f = fc(pts, NOW)
    assert f["status"] == "unknown" and "used" in f["reason"]


def test_null_reset_is_unknown():
    pts = series(NOW - 2 * H, 5 * M, [30 + i for i in range(25)], None)
    f = fc(pts, NOW)
    assert f["status"] == "unknown" and "reset" in f["reason"]
    assert fc([], NOW)["status"] == "unknown"


def test_reset_boundary_uses_only_the_current_cycle():
    old = series(NOW - 3 * H, 5 * M, [80 + i * 0.5 for i in range(24)], NOW - H)  # previous 5h cycle
    new = series(NOW - 55 * M, 5 * M, [20, 22, 24, 26, 28, 30, 32, 34, 36, 38, 40, 42], NOW + 4 * H)
    f = fc(old + new, NOW)
    assert f["status"] == "ok"
    assert f["confidence"] == 12
    assert f["rate_pct_h"] == pytest.approx(24.0, abs=0.01)


def test_post_reset_burst_stays_unknown_until_40_minutes_and_20_percent():
    reset = NOW + 5 * H
    start = NOW
    burst = [3 + 6 * i for i in range(8)]  # 6% per 5 min right after a reset
    pts = series(start, 5 * M, burst, reset)
    # 35 min in: span < 40 min -> unknown even though used > 20
    assert fc(pts, start + 35 * M)["status"] == "unknown"
    slow = series(start, 5 * M, [1 + i for i in range(10)], reset)  # 45 min, only 10%
    assert fc(slow, start + 45 * M)["status"] == "unknown"
    ok = series(start, 5 * M, [3 + 6 * i for i in range(9)], reset)  # 40 min, 51%
    assert fc(ok, start + 40 * M)["status"] == "ok"


def test_accelerating_series_has_positive_accel_and_short_rate():
    reset = NOW + 2 * H
    slow = [30 + 5 * i / 12 for i in range(25)]                   # 2 h at 5%/h
    fast = [slow[-1] + 20 * (i + 1) / 12 for i in range(6)]        # last 30 min at 20%/h
    pts = series(NOW - 2.5 * H, 5 * M, slow + fast, reset)
    f = fc(pts, NOW)
    assert f["status"] == "ok"
    assert f["r_short"] > 1.5 * f["rate_pct_h"] > 0
    assert f["accel"] == pytest.approx(f["r_short"] - f["rate_pct_h"])
    assert f["projected_short"] > f["projected_at_reset"]


def test_weekly_long_rate_is_the_cycle_average_so_quiet_nights_count():
    period = WEEKLY["period_s"]
    start = NOW - 5.25 * 24 * H          # cycle began 5d6h ago -> reset in 1d18h
    reset = start + period
    pts, used, t = [], 0.0, start
    while t <= NOW:
        hour_of_day = ((t - start) / H) % 24
        pts.append([t, used, reset])
        if hour_of_day < 10:              # 10 h daytime burn at 1 %/h, 14 h quiet night
            used += 1.0 * (30 * M / H)
        t += 30 * M
    f = core.forecast(pts, NOW, WEEKLY, CFG)
    assert f["status"] == "ok"
    elapsed_h = (NOW - start) / H
    assert f["rate_pct_h"] == pytest.approx(f["used"] / elapsed_h, rel=1e-6)
    assert f["used"] == pytest.approx(56.0)
    assert f["r_short"] == pytest.approx(1.0)  # daytime burn in the last 6 h
    assert f["projected_at_reset"] < 80        # a daytime-slope projection would say ~98


def test_malformed_points_are_ignored():
    good = series(NOW - H, 5 * M, [30 + i for i in range(13)], NOW + 2 * H)
    bad = good[:3] + [["x", 1, 2], None, [NOW, "y", NOW]] + good[3:]
    assert fc(bad, NOW)["confidence"] == 13


def test_forecast_state_covers_configured_core_windows_only():
    st = core.empty_state()
    reset = NOW + 3 * H
    st["history"] = {"anthropic": {
        "Current session": series(NOW - H, 5 * M, [30 + i for i in range(13)], reset),
        "Fable week (model)": series(NOW - H, 5 * M, [30] * 13, reset)},
        "openai-codex": {"Weekly": series(NOW - H, 5 * M, [30] * 13, reset)}}
    # openai-codex has default forecast windows since 0.4.0; configure none for it here
    anthropic_only = dict(CFG, forecast_windows={"anthropic": CFG["forecast_windows"]["anthropic"]})
    out = core.forecast_state(st, NOW, anthropic_only)
    assert set(out) == {"anthropic"}
    assert set(out["anthropic"]) == {"Current session"}
    assert out["anthropic"]["Current session"]["kind"] == "session"


def test_forecast_appears_in_status_json_and_text():
    st = core.empty_state()
    reset = NOW + 3 * H
    for i in range(13):
        t = NOW - H + i * 5 * M
        core.apply_refresh(st, {"anthropic": {"fetched_at": t, "windows": [
            {"label": "Current session", "used_percent": 30 + i, "resets_at": reset},
            {"label": "Fable week (model)", "used_percent": 5, "resets_at": reset, "scope": "Fable"}]}},
            t, CFG)
    payload = core.status_payload(st, NOW, CFG, "host")
    wins = {w["label"]: w for w in payload["providers"]["anthropic"]["windows"]}
    f = wins["Current session"]["forecast"]
    assert f["status"] == "ok" and f["rate_pct_h"] == pytest.approx(12.0, abs=0.01)
    assert set(f) >= {"rate_pct_h", "projected_at_reset", "accel", "confidence"}
    assert wins["Fable week (model)"]["forecast"] is None
    text = core.render_status(st, NOW, CFG)
    assert "→" in text and "12.0%/h" in text


def test_codex_session_and_weekly_are_forecast_by_default():
    assert core.DEFAULTS["forecast_windows"].get("openai-codex") == {"Session": "session", "Weekly": "weekly"}
    st = core.empty_state()
    reset = NOW + 3 * H
    for i in range(13):
        t = NOW - H + i * 5 * M
        core.apply_refresh(st, {"openai-codex": {"fetched_at": t, "windows": [
            {"label": "Session", "used_percent": 30 + i, "resets_at": reset},
            {"label": "Weekly", "used_percent": 40, "resets_at": NOW + 4 * 24 * H}]}}, t, CFG)
    out = core.forecast_state(st, NOW, CFG)["openai-codex"]
    assert out["Session"]["kind"] == "session" and out["Session"]["status"] == "ok"
    assert out["Session"]["rate_pct_h"] == pytest.approx(12.0, abs=0.01)
    assert out["Weekly"]["kind"] == "weekly"
    wins = {w["label"]: w for w in core.status_payload(st, NOW, CFG, "host")["providers"]["openai-codex"]["windows"]}
    assert wins["Session"]["forecast"]["status"] == "ok"
