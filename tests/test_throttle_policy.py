"""Throttle level policy (plan Task 3; binding v2 B1, B4, precedence, reset exception, dedupe)."""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location("qr_core_pol", Path(__file__).resolve().parents[1] / "core.py")
core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(core)
CFG = dict(core.DEFAULTS)
H = 3600.0
M = 60.0
NOW = 1_800_000_000.0
SESSION_RESET = NOW + 3 * H
WEEK_RESET = NOW + 3 * 24 * H

# Neutral example level specs (config, not code defaults).
LEVELS = {
    "fast": {"provider": "anthropic", "model": "opus-x",
             "fallback": [{"provider": "custom:internal", "model": "glm-x"},
                          {"provider": "openai-codex", "model": "codex-x"}]},
    "constrained": {"provider": "custom:internal", "model": "glm-x",
                    "fallback": [{"provider": "openai-codex", "model": "codex-x"}]},
}


def tcfg(**over):
    t = core.throttle_config({"levels": LEVELS, "profiles": ["worker"], **over})
    return t


def feed(st, t, session, week=40.0, session_reset=SESSION_RESET, week_reset=WEEK_RESET):
    core.apply_refresh(st, {"anthropic": {"fetched_at": t, "windows": [
        {"label": "Current session", "used_percent": session, "resets_at": session_reset},
        {"label": "Current week", "used_percent": week, "resets_at": week_reset}]}}, t, CFG)


def ramp(st, start, n, first, step, **kw):
    """n points every 5 min from ``first`` rising by ``step`` per point; returns the last time."""
    t = start
    for i in range(n):
        t = start + i * 5 * M
        feed(st, t, first + step * i, **kw)
    return t


def step(st, now, t=None):
    return core.throttle_step(st, now, CFG, t or tcfg())


def test_defaults_start_fast_and_carry_no_model_specs():
    assert core.throttle_config({})["levels"] == {}
    st = core.empty_state()
    d = step(st, NOW)
    assert d["level"] == "fast"
    assert st["throttle"]["level"] == "fast"


def test_rule_a_projection_enters_constrained():
    st = core.empty_state()
    # 12 points over 55 min: 30 → 52 (24 %/h), 3 h to reset -> projected ~124
    t = ramp(st, NOW - 55 * M, 12, 30, 2)
    d = step(st, t)
    assert d["level"] == "constrained" and d["rule"] == "a"
    assert d["windows"]["Current session"]["projected"] > 95


def test_rule_b_used_enters_constrained_without_a_forecast():
    st = core.empty_state()
    feed(st, NOW, 86)
    d = step(st, NOW)
    assert d["level"] == "constrained" and d["rule"] == "b"


def test_rule_c_acceleration_enters_below_the_projection_threshold():
    st = core.empty_state()
    reset = NOW + 3.5 * H
    # 2 h slow burn at 3 %/h then 30 min at 12 %/h: projected in [85, 95)
    t = ramp(st, NOW - 2.5 * H, 25, 60, 0.25, session_reset=reset)
    t = ramp(st, t + 5 * M, 6, 66 + 1, 1, session_reset=reset)
    f = core.forecast_state(st, t, CFG)["anthropic"]["Current session"]
    assert 85 <= f["projected_at_reset"] < 95 and f["r_short"] >= 1.5 * f["rate_pct_h"]
    d = step(st, t)
    assert d["level"] == "constrained" and d["rule"] == "c"


def test_rule_c_needs_acceleration():
    st = core.empty_state()
    reset = NOW + 2 * H
    t = ramp(st, NOW - 2.5 * H, 31, 60, 0.4, session_reset=reset)  # steady ~4.8 %/h -> ~82..
    f = core.forecast_state(st, t, CFG)["anthropic"]["Current session"]
    assert f["r_short"] < 1.5 * f["rate_pct_h"]
    assert step(st, t)["level"] == "fast"


def test_reset_exception_suppresses_escalation_but_not_a_block():
    st = core.empty_state()
    soon = NOW + 10 * M
    feed(st, NOW, 96, session_reset=soon)
    assert step(st, NOW)["level"] == "fast"
    core.record_error(st, provider="anthropic", status_code=429, reason="rate_limit",
                      message="usage limit reached", model="m", now=NOW, cfg=CFG)
    d = step(st, NOW + 1)
    assert d["level"] == "constrained" and d["source"] == "block"


def test_block_while_constrained_expires_at_window_reset():
    st = core.empty_state()
    feed(st, NOW, 50, session_reset=NOW + H)
    core.record_error(st, provider="anthropic", status_code=429, reason="rate_limit",
                      message="usage limit reached", model="m", now=NOW, cfg=CFG)
    assert st["blocks"]["anthropic"]["until"] == NOW + H
    assert step(st, NOW + 60)["source"] == "block"
    later = NOW + H + 60
    feed(st, later, 5, session_reset=NOW + 6 * H)
    core.recompute(st, later, CFG)
    d = step(st, later)
    assert d["source"] == "policy" and d["level"] == "fast"


def test_leave_needs_short_projection_used_and_dwell():
    st = core.empty_state()
    feed(st, NOW, 86)
    assert step(st, NOW)["level"] == "constrained"
    # burn stopped and usage is below leave_used, but the dwell has not passed
    ramp(st, NOW + 5 * M, 4, 70, 0)
    assert step(st, NOW + 20 * M)["level"] == "constrained"
    feed(st, NOW + 31 * M, 70)
    d = step(st, NOW + 31 * M)
    assert d["level"] == "fast" and d["rule"] == "leave"


def test_leave_blocked_while_short_rate_still_projects_high():
    st = core.empty_state()
    feed(st, NOW - 2 * H, 86)
    step(st, NOW - 2 * H)
    # used 60..71 over 55 min but burning at 12 %/h -> projected_short >> 80
    t = ramp(st, NOW - 55 * M, 12, 60, 1)
    assert step(st, t)["level"] == "constrained"


def test_unknown_forecast_counts_as_satisfied_for_leave():
    st = core.empty_state()
    feed(st, NOW, 86)
    step(st, NOW)
    feed(st, NOW + 31 * M, 70)          # one point: forecast unknown, used < leave_used
    f = core.forecast_state(st, NOW + 31 * M, CFG)["anthropic"]["Current session"]
    assert f["status"] == "unknown"
    assert step(st, NOW + 31 * M)["level"] == "fast"


def test_no_oscillation_under_jitter():
    st = core.empty_state()
    levels = []
    t = NOW
    for i, used in enumerate([84, 86, 84, 86, 84, 86, 84, 86] * 4):
        t = NOW + i * 5 * M
        feed(st, t, used, session_reset=t + 4 * H)
        levels.append(step(st, t)["level"])
    changes = sum(1 for a, b in zip(levels, levels[1:]) if a != b)
    assert changes == 1 and levels[-1] == "constrained"
    # 94 ↔ 96 jitter of the projection around enter_projected (95), at the pure policy level
    t = tcfg()
    level, since, levels = "fast", 0.0, []
    for i, projected in enumerate([94, 96] * 10):
        now = NOW + i * 5 * M
        win = {"label": "Current week", "used": 50.0, "resets_at": WEEK_RESET,
               "forecast": {"status": "ok", "projected_at_reset": projected, "projected_short": projected,
                            "rate_pct_h": 1.0, "r_short": 1.0}}
        new, _rule, _label = core.throttle_policy(level, since, [win], now, t)
        if new != level:
            level, since = new, now
        levels.append(level)
    assert sum(1 for a, b in zip(levels, levels[1:]) if a != b) == 1 and levels[-1] == "constrained"


def test_rule_a_needs_the_short_rate_to_confirm_a_long_projection():
    """An early weekly burst can keep the cycle-average projection high after recent burn subsides.
    A high long projection with a low short projection must not re-enter after a leave, or the
    policy limit-cycles enter → dwell → leave → enter."""
    st = core.empty_state()
    feed(st, NOW, 86)
    assert step(st, NOW)["level"] == "constrained"
    feed(st, NOW + 31 * M, 70)                    # burn stopped: rule leave
    t = step(st, NOW + 31 * M)
    assert t["level"] == "fast" and t["rule"] == "leave"
    # identical later snapshot: long projection still ~196 (early in a weekly cycle), short ~23
    t = tcfg()
    win = {"label": "Current week", "used": 24.0, "resets_at": WEEK_RESET,
           "forecast": {"status": "ok", "projected_at_reset": 196.0, "projected_short": 23.0,
                        "rate_pct_h": 1.2, "r_short": 0.0}}
    levels_seen = []
    level, since = "fast", NOW + 31 * M           # where the leave above left the provider
    for i in range(6):                            # several dwell periods: no new usage either way
        now = NOW + 62 * M + i * 10 * M
        new, rule, _label = core.throttle_policy(level, since, [win], now, t)
        levels_seen.append(new)
        if new != level:
            level, since = new, now
    assert levels_seen == ["fast"] * 6            # stays fast: 0 flips on an unchanged snapshot
    # a genuine steady burn keeps the short projection high and still escalates
    win["forecast"]["projected_short"] = 197.0
    new, rule, _label = core.throttle_policy("fast", 0.0, [win], NOW, t)
    assert (new, rule) == ("constrained", "a")


def test_stale_data_holds_then_escalates_when_last_known_usage_was_high():
    st = core.empty_state()
    feed(st, NOW, 78)
    assert step(st, NOW)["level"] == "fast"
    assert step(st, NOW + 45 * M)["source"] == "stale_hold"
    d = step(st, NOW + 61 * M)
    assert d["level"] == "constrained" and d["source"] == "stale"
    pending = [l for l in st["pending"] if "throttle" in l]
    assert len(pending) == 1 and "stale usage data" in pending[0]
    step(st, NOW + 66 * M)
    step(st, NOW + 71 * M)
    assert len([l for l in st["pending"] if "throttle" in l]) == 1  # once per incident


def test_stale_data_returns_to_fast_only_when_every_window_was_low():
    st = core.empty_state()
    feed(st, NOW, 86)
    assert step(st, NOW)["level"] == "constrained"
    feed(st, NOW + 31 * M, 70)
    assert step(st, NOW + 31 * M)["level"] == "fast"
    d = step(st, NOW + 31 * M + 61 * M)
    assert d["level"] == "fast" and d["source"] == "stale"
    st2 = core.empty_state()
    assert step(st2, NOW)["level"] == "fast"  # never any data


def test_stale_ignores_windows_known_to_have_reset():
    st = core.empty_state()
    feed(st, NOW, 90, session_reset=NOW + 40 * M)
    step(st, NOW)
    d = step(st, NOW + 2 * H)
    assert d["level"] == "fast"


def test_precedence_hold_over_disable_over_block_over_policy():
    st = core.empty_state()
    feed(st, NOW, 86)
    core.record_error(st, provider="anthropic", status_code=429, reason="rate_limit",
                      message="usage limit reached", model="m", now=NOW, cfg=CFG)
    assert step(st, NOW)["source"] == "block"
    core.throttle_disable(st, "fast", NOW)
    d = step(st, NOW + 1)
    assert (d["level"], d["source"]) == ("fast", "disable")
    core.throttle_hold(st, "constrained", NOW + 2 * H, NOW + 2)
    d = step(st, NOW + 3)
    assert (d["level"], d["source"]) == ("constrained", "hold")
    d = step(st, NOW + 2 * H + 1)        # hold expired -> disable again
    assert (d["level"], d["source"]) == ("fast", "disable")
    assert "hold" not in st["throttle"]
    core.throttle_disable(st, None, NOW + 2 * H + 2)   # clear
    assert step(st, NOW + 2 * H + 3)["source"] in ("block", "policy", "stale", "stale_hold")


def test_stale_never_overrides_hold_or_block():
    st = core.empty_state()
    feed(st, NOW, 10)
    core.throttle_hold(st, "fast", NOW + 5 * H, NOW)
    assert step(st, NOW + 3 * H)["source"] == "hold"
    st = core.empty_state()
    feed(st, NOW, 10, session_reset=None)
    core.record_error(st, provider="anthropic", status_code=402, reason="billing", message="x",
                      model="m", now=NOW, cfg=CFG)
    assert step(st, NOW + 2 * H)["source"] == "block"


def test_transition_alert_is_queued_exactly_once():
    st = core.empty_state()
    feed(st, NOW, 86)
    st["pending"] = []
    for i in range(5):
        step(st, NOW + i * 60)
    lines = [l for l in core.drain_pending(st) if "throttle" in l]
    assert len(lines) == 1
    assert lines[0].startswith("quota-router: throttle (shadow) fast → constrained")
    assert "Current session" in lines[0] and "rule b" in lines[0]
    live = tcfg(enabled=True)
    st = core.empty_state()
    feed(st, NOW, 86)
    core.throttle_step(st, NOW, CFG, live)
    assert any(l.startswith("quota-router: throttle fast → constrained") for l in st["pending"])


def test_decisions_are_logged_for_the_shadow_report():
    st = core.empty_state()
    t = ramp(st, NOW - 55 * M, 12, 30, 2)
    step(st, t)
    rec = st["throttle"]["log"][-1]
    assert rec["level"] == "constrained" and rec["rule"] == "a"
    w = rec["windows"]["Current session"]
    assert set(w) >= {"used", "projected", "projected_short", "r_long", "r_short"}
    for i in range(core.THROTTLE_LOG_MAX + 3):
        step(st, t + i)
    assert len(st["throttle"]["log"]) == core.THROTTLE_LOG_MAX


def test_constrained_chain_must_not_contain_anthropic():
    assert core.validate_levels(LEVELS) == []
    leaky = {**LEVELS, "constrained": {**LEVELS["constrained"], "fallback": [
        {"provider": "anthropic", "model": "opus-x"}, {"provider": "openai-codex", "model": "codex-x"}]}}
    errs = core.validate_levels(leaky)
    assert errs and "anthropic" in errs[0]
    assert core.validate_levels({"fast": LEVELS["fast"]})  # both levels required
    assert core.validate_levels({**LEVELS, "constrained": {"provider": "anthropic", "model": "x"}})


def test_glm_outage_while_constrained_yields_zero_anthropic_sessions():
    """Policy level: GLM failing is not an Anthropic signal, the level stays constrained, and the
    constrained chain walked with GLM down never reaches Anthropic."""
    st = core.empty_state()
    feed(st, NOW, 86)
    assert step(st, NOW)["level"] == "constrained"
    sessions = []
    for i in range(20):
        t = NOW + i * 5 * M
        core.record_error(st, provider="custom:internal", status_code=429, reason="rate_limit",
                          message="usage limit", model="glm-x", now=t, cfg=CFG)
        feed(st, t, 86)
        level = step(st, t)["level"]
        chain = core.level_chain(LEVELS[level])
        sessions.append(next(p for p, _m in chain if p != "custom:internal"))
    assert "anthropic" not in sessions and set(sessions) == {"openai-codex"}
