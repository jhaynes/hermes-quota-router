"""Opt-in routing preference never buys headroom by bypassing quota policy."""
import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core as c

NOW = 2_000_000_000.0


def cfg(**changes):
    throttle = {
        "enabled": True, "prefer_short_windows": True, "profiles": [],
        "ladder": [
            {"name": "codex", "gate": ["openai-codex"], "provider": "openai-codex",
             "model": "gpt-6.1-sol", "fallback": [{"provider": "custom:internal", "model": "glm"}]},
            {"name": "claude", "gate": ["anthropic"], "provider": "anthropic",
             "model": "claude-opus-5-5", "fallback": [{"provider": "custom:internal", "model": "glm"}]},
            {"name": "internal", "provider": "custom:internal", "model": "glm"},
        ],
    }
    throttle.update(changes)
    return c.throttle_config(throttle)


def state():
    s = c.empty_state()
    for p, label in (("openai-codex", "Weekly"), ("anthropic", "Current week")):
        s["providers"][p] = {"snapshot": {
            "fetched_at": NOW, "windows": [
                {"label": label, "used": 10.0, "resets_at": NOW + 5 * 86400},
            ], "unavailable": None,
        }}
    return s


def session(s, provider="anthropic", **changes):
    w = {"label": "Current session" if provider == "anthropic" else "Session",
         "used": 20.0, "resets_at": NOW + 3 * 3600, "scope": "account"}
    w.update(changes)
    s["providers"][provider]["snapshot"]["windows"].append(w)
    return w


def decide(s, config=None, now=NOW):
    return c.throttle_step(s, now, c.DEFAULTS, config or cfg())


def test_prefers_anthropic_session_over_codex_weekly_only():
    s = state()
    session(s)
    d = decide(s)
    assert d["level"] == "claude"
    assert d["source"] == "preference"
    assert d["rule"] == "short_window"
    assert "Current session" in d["label"]
    assert all(p["state"] == "ok" for p in d["providers"].values())
    assert s["throttle"]["log"][-1]["rule"] == "short_window"
    assert any("short window" in a for a in s["pending"])


def test_default_and_false_keep_static_ladder_and_record_shapes():
    s = state()
    session(s)
    raw = cfg()
    raw.pop("prefer_short_windows", None)
    baseline = decide(copy.deepcopy(s), raw)
    explicit = decide(s, cfg(prefer_short_windows=False))
    assert baseline == explicit
    assert baseline["level"] == "codex"


@pytest.mark.parametrize("both", [True, False])
def test_equal_window_classes_use_ladder_order(both):
    s = state()
    if both:
        session(s)
        session(s, "openai-codex")
    assert decide(s)["level"] == "codex"


def test_known_idle_zero_usage_session_without_reset_is_usable():
    s = state()
    session(s, used=0, resets_at=None)
    assert decide(s)["level"] == "claude"


@pytest.mark.parametrize("changes", [
    {"used": None}, {"used": float("nan")}, {"used": float("inf")}, {"used": True},
    {"used": -1}, {"used": 85}, {"used": 100}, {"stale": True}, {"scope": "model"},
    {"scope": "Opus"}, {"resets_at": NOW}, {"resets_at": NOW - 1},
    {"resets_at": None}, {"resets_at": NOW + 7 * 86400},
])
def test_unknown_invalid_expired_or_exhausted_session_never_promotes(changes):
    s = state()
    session(s, **changes)
    assert decide(s)["level"] == "codex"


@pytest.mark.parametrize("field,value", [("fetched_at", NOW - 1801),
                                          ("unavailable", "fetch failed")])
def test_stale_or_unavailable_provider_never_receives_preference(field, value):
    s = state()
    session(s)
    s["providers"]["anthropic"]["snapshot"][field] = value
    assert decide(s)["level"] == "codex"


def test_weekly_guard_still_constrains_provider_with_session_room():
    s = state()
    session(s)
    s["providers"]["anthropic"]["snapshot"]["windows"][0]["used"] = 90
    assert decide(s)["level"] == "codex"


def test_observed_block_beats_short_window():
    s = state()
    session(s)
    s["blocks"]["anthropic"] = {"until": NOW + 3600, "reason": "429"}
    d = decide(s)
    assert d["level"] == "codex"
    assert d["providers"]["anthropic"]["source"] == "block"


@pytest.mark.parametrize("family,expected", [("Opus", "codex"), ("Sonnet", "claude")])
def test_model_family_cap_only_gates_applicable_primary_model(family, expected):
    s = state()
    session(s)
    s["providers"]["anthropic"]["snapshot"]["windows"].append(
        {"label": family + " week", "scope": "model", "used": 90,
         "resets_at": NOW + 5 * 86400})
    assert decide(s)["level"] == expected


def test_model_weekly_forecast_gate_cannot_be_bypassed():
    s = state()
    session(s)
    s["providers"]["anthropic"]["snapshot"]["windows"].append(
        {"label": "Opus week", "scope": "Opus", "used": 50,
         "resets_at": NOW + 5 * 86400})
    s["history"]["anthropic"] = {
        "Opus week": [[NOW - 3 * 3600 + i * 1800, 20 + i * 5, NOW + 5 * 86400] for i in range(7)]}
    assert decide(s)["level"] == "codex"


def test_opt_in_retains_real_model_history_and_forecasts_it():
    s = state()
    session(s)
    cap = {"label": "Opus week", "scope": "model", "used": 20,
           "resets_at": NOW + 5 * 86400}
    s["providers"]["anthropic"]["snapshot"]["windows"].append(cap)
    d = None
    for i in range(7):
        now = NOW - 3 * 3600 + i * 1800
        cap["used"] = 20 + i * 5
        for p in s["providers"].values():
            p["snapshot"]["fetched_at"] = now
        d = decide(s, now=now)
    assert len(s["history"]["anthropic"]["Opus week"]) == 7
    assert d is not None and d["level"] == "codex"
    assert d["providers"]["anthropic"]["windows"]["Opus week"]["projected_short"] >= 95


def test_disable_preference_leaves_model_caps_display_only_as_before():
    s = state()
    session(s, "openai-codex")
    s["providers"]["anthropic"]["snapshot"]["windows"].append(
        {"label": "Opus week", "scope": "model", "used": 99,
         "resets_at": NOW + 5 * 86400})
    d = decide(s, cfg(prefer_short_windows=False))
    assert d["providers"]["anthropic"]["state"] == "ok"


@pytest.mark.parametrize("key", ["hold", "disabled"])
def test_manual_override_wins(key):
    s = state()
    session(s)
    s["throttle"] = {key: {"level": "internal", "until": NOW + 3600}}
    assert decide(s)["level"] == "internal"


def test_preference_switch_dwell_then_reconsideration():
    s = state()
    assert decide(s)["level"] == "codex"
    session(s)
    d = decide(s, now=NOW + 60)
    assert d["level"] == "codex"
    assert d["rule"] == "preference_dwell"
    for p in s["providers"].values():
        p["snapshot"]["fetched_at"] = NOW + 1801
    assert decide(s, now=NOW + 1801)["level"] == "claude"


def test_unsafe_current_route_bypasses_preference_dwell():
    s = state()
    session(s)
    assert decide(s)["level"] == "claude"
    s["providers"]["anthropic"]["snapshot"]["windows"][0]["used"] = 95
    d = decide(s, now=NOW + 60)
    assert d["level"] == "codex"
    assert d["providers"]["anthropic"]["state"] == "constrained"


def test_returns_after_session_replenishes_and_policy_dwell_expires():
    s = state()
    w = session(s, used=95)
    assert decide(s)["level"] == "codex"
    now = NOW + 1801
    w.update(used=5, resets_at=now + 5 * 3600)
    for p in s["providers"].values():
        p["snapshot"]["fetched_at"] = now
    assert decide(s, now=now)["level"] == "claude"


def test_internal_floor_when_both_constrained():
    s = state()
    session(s)
    for p in s["providers"].values():
        p["snapshot"]["windows"][0]["used"] = 90
    assert decide(s)["level"] == "internal"


@pytest.mark.parametrize("value", ["false", 1, None, []])
def test_preference_requires_explicit_boolean(value):
    assert c.validate_throttle(cfg(prefer_short_windows=value), c.DEFAULTS)


def test_shadow_mode_still_selects_and_logs():
    s = state()
    session(s)
    assert decide(s, cfg(enabled=False))["level"] == "claude"
    assert any("(shadow)" in a for a in s["pending"])
