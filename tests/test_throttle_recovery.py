"""Regression: recent-rate recovery must not re-enter on an old long projection."""
import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("qr_recovery", Path(__file__).resolve().parents[1] / "core.py")
assert _spec is not None and _spec.loader is not None
core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(core)
NOW = 1_800_000_000.0
H = 3600.0


@pytest.mark.parametrize("provider,label", [("openai-codex", "Weekly"), ("anthropic", "Current week")])
@pytest.mark.parametrize("ladder", [False, True])
def test_weekly_recovery_stays_recovered_with_fresh_flat_samples(provider, label, ladder):
    cfg = dict(core.DEFAULTS)
    reset = NOW + 6 * 24 * H
    st = core.empty_state()
    # First-day burst followed by six quiet hours: long projection stays high, short is flat.
    st["history"] = {provider: {label: [[NOW - i * H, 24.0, reset] for i in range(8, -1, -1)]}}
    spec = {"provider": provider, "model": "premium", "fallback": []}
    floor = {"provider": "custom:internal", "model": "internal", "fallback": []}
    user = {"protected_provider": provider}
    if ladder:
        user["ladder"] = [{"name": "fast", "gate": [provider], **spec},
                          {"name": "constrained", **floor}]
    else:
        user["levels"] = {"fast": spec, "constrained": floor}
    t = core.throttle_config(user)
    st["throttle"] = {"level": "constrained", "since": NOW - t["min_dwell_s"],
                      "providers": {provider: {"state": "constrained", "since": NOW - t["min_dwell_s"]}}}
    decisions = []
    for i in range(9):
        now = NOW + i * 15 * 60
        core.apply_refresh(st, {provider: {"fetched_at": now, "windows": [
            {"label": label, "used_percent": 24.0, "resets_at": reset}]}}, now, cfg)
        decision = core.throttle_step(st, now, cfg, t)
        window = decision["windows"][label]
        assert window["projected"] >= t["enter_projected"]
        assert window["projected_short"] <= t["leave_projected"]
        decisions.append((decision["level"], decision["rule"]))
    assert decisions == [("fast", "leave")] + [("fast", None)] * 8


@pytest.mark.parametrize("long,short,expected", [
    (95.0, 95.0, "a"), (95.0, 94.99, None), (94.99, 95.0, None),
    (196.0, 23.0, None), (94.0, 196.0, "c"),
])
def test_projection_confirmation_boundaries(long, short, expected):
    t = core.throttle_config({})
    win = {"label": "Weekly", "used": 24.0, "resets_at": NOW + 6 * 24 * H,
           "forecast": {"status": "ok", "projected_at_reset": long, "projected_short": short,
                        "rate_pct_h": 1.0, "r_short": 2.0 if short == 196.0 else 1.0}}
    level, rule, _ = core.throttle_policy("fast", 0.0, [win], NOW, t)
    assert rule == expected
    assert level == ("constrained" if expected else "fast")
