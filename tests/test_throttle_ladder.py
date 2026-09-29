"""Throttle ladder: N ordered levels gated by per-provider policy state.

Each gate provider runs the unchanged fast/constrained policy (rules a/b/c, leave, dwell, stale,
blocks) on its own forecast windows; the selected level is the first whose gate providers are all
``ok``. The legacy ``levels: {fast, constrained}`` + ``protected_provider`` config maps to the
two-rung ladder ``[fast gated by protected_provider, constrained]`` and must decide exactly as the
0.3.0 code did (see ``legacy_step`` below, a verbatim copy of that code used as the oracle).
"""
import copy
import importlib.util
import json
import random
from datetime import datetime
from pathlib import Path

import pytest

_root = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("qr_core_ladder", _root / "core.py")
core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(core)
CFG = dict(core.DEFAULTS)
H = 3600.0
M = 60.0
NOW = 1_800_000_000.0
CODEX, CLAUDE = "openai-codex", "anthropic"

LADDER = [
    {"name": "codex", "gate": [CODEX], "provider": CODEX, "model": "codex-x",
     "fallback": [{"provider": "custom:internal", "model": "glm-x"}]},
    {"name": "claude", "gate": [CLAUDE], "provider": CLAUDE, "model": "opus-x",
     "fallback": [{"provider": "custom:internal", "model": "glm-x"}]},
    {"name": "internal", "provider": "custom:internal", "model": "glm-x",
     "fallback": [{"provider": "backup", "model": "backup-x"}]},
]
LEVELS = {
    "fast": {"provider": "anthropic", "model": "opus-x",
             "fallback": [{"provider": "custom:internal", "model": "glm-x"},
                          {"provider": "openai-codex", "model": "codex-x"}]},
    "constrained": {"provider": "custom:internal", "model": "glm-x",
                    "fallback": [{"provider": "openai-codex", "model": "codex-x"}]},
}


def lcfg(**over):
    return core.throttle_config({"ladder": LADDER, "profiles": ["worker"], **over})


def feed(st, t, codex=None, claude=None, codex_reset=None, claude_reset=None):
    """One refresh at ``t``. ``codex`` = (Session, Weekly) used %, ``claude`` = (Current session,
    Current week) used %; ``None`` means that provider's fetch failed (no snapshot folded in)."""
    snaps = {}
    if codex is not None:
        s_reset = codex_reset or t + 3 * H
        snaps[CODEX] = {"fetched_at": t, "windows": [
            {"label": "Session", "used_percent": codex[0], "resets_at": s_reset},
            {"label": "Weekly", "used_percent": codex[1], "resets_at": NOW + 4 * 24 * H}]}
    if claude is not None:
        s_reset = claude_reset or t + 3 * H
        snaps[CLAUDE] = {"fetched_at": t, "windows": [
            {"label": "Current session", "used_percent": claude[0], "resets_at": s_reset},
            {"label": "Current week", "used_percent": claude[1], "resets_at": NOW + 4 * 24 * H}]}
    core.apply_refresh(st, snaps, t, CFG)


def step(st, now, t=None):
    return core.throttle_step(st, now, CFG, t or lcfg())


def pstate(st, provider):
    return ((st.get("throttle") or {}).get("providers") or {}).get(provider) or {}


def throttle_lines(st):
    return [ln for ln in core.drain_pending(st) if " throttle" in ln]


# ── selection over per-provider state ─────────────────────────────────────────

def test_codex_constrained_and_anthropic_ok_selects_the_claude_level():
    st = core.empty_state()
    feed(st, NOW, codex=(20, 90), claude=(10, 30))
    d = step(st, NOW)
    assert d["level"] == "claude", d
    assert pstate(st, CODEX).get("state") == "constrained" and pstate(st, CODEX).get("rule") == "b"
    assert pstate(st, CLAUDE).get("state") == "ok"


def test_both_constrained_selects_the_floor():
    st = core.empty_state()
    feed(st, NOW, codex=(20, 90), claude=(88, 30))
    d = step(st, NOW)
    assert d["level"] == "internal", d
    assert pstate(st, CODEX).get("state") == pstate(st, CLAUDE).get("state") == "constrained"


def test_first_level_while_its_gate_is_ok_whatever_the_later_gates_say():
    st = core.empty_state()
    feed(st, NOW, codex=(20, 30), claude=(88, 30))
    d = step(st, NOW)
    assert d["level"] == "codex", d
    assert pstate(st, CLAUDE).get("state") == "constrained"


def test_codex_recovers_after_its_reset_and_returns_only_after_min_dwell():
    t = lcfg(min_dwell_s=H)
    st = core.empty_state()
    feed(st, NOW, codex=(90, 30), claude=(10, 30), codex_reset=NOW + 40 * M)
    assert step(st, NOW, t)["level"] == "claude"
    # the Session window reset at NOW+40m: usage is low again, but the 1 h dwell has not passed
    later = NOW + 45 * M
    feed(st, later, codex=(2, 30), claude=(10, 30), codex_reset=NOW + 5 * H + 40 * M)
    d = step(st, later, t)
    assert d["level"] == "claude", d
    assert (pstate(st, CODEX).get("state"), pstate(st, CODEX).get("rule")) == ("constrained", "dwell")
    later = NOW + 61 * M
    feed(st, later, codex=(3, 30), claude=(10, 30), codex_reset=NOW + 5 * H + 40 * M)
    d = step(st, later, t)
    assert d["level"] == "codex", d
    assert (pstate(st, CODEX).get("state"), pstate(st, CODEX).get("rule")) == ("ok", "leave")


def test_hysteresis_and_dwell_are_independent_per_provider():
    st = core.empty_state()
    feed(st, NOW, codex=(10, 30), claude=(90, 30))
    assert step(st, NOW)["level"] == "codex"
    feed(st, NOW + 20 * M, codex=(10, 90), claude=(90, 30))
    assert step(st, NOW + 20 * M)["level"] == "internal"
    assert pstate(st, CLAUDE).get("since") == NOW and pstate(st, CODEX).get("since") == NOW + 20 * M
    # 31 min after anthropic entered, 11 min after codex did: only anthropic may leave
    feed(st, NOW + 31 * M, codex=(10, 10), claude=(10, 10))
    d = step(st, NOW + 31 * M)
    assert d["level"] == "claude", d
    assert pstate(st, CLAUDE).get("state") == "ok"
    assert (pstate(st, CODEX).get("state"), pstate(st, CODEX).get("rule")) == ("constrained", "dwell")
    feed(st, NOW + 51 * M, codex=(10, 10), claude=(10, 10))
    assert step(st, NOW + 51 * M)["level"] == "codex"


def test_jitter_on_one_provider_never_moves_the_other():
    st = core.empty_state()
    states = []
    for i, used in enumerate([84, 86, 84, 86, 84, 86] * 3):
        t = NOW + i * 5 * M
        feed(st, t, codex=(10, 20), claude=(used, 30), claude_reset=t + 4 * H)
        d = step(st, t)
        states.append(pstate(st, CLAUDE).get("state"))
        assert d["level"] == "codex"
        assert pstate(st, CODEX).get("state") == "ok" and pstate(st, CODEX).get("since") == NOW
    assert states[0] == "ok" and set(states[1:]) == {"constrained"}, states


def test_stale_data_for_one_provider_only():
    st = core.empty_state()
    feed(st, NOW, codex=(10, 80), claude=(10, 30))
    assert step(st, NOW)["level"] == "codex"
    throttle_lines(st)
    feed(st, NOW + 45 * M, claude=(10, 30))           # codex fetch failing from here on
    step(st, NOW + 45 * M)
    assert (pstate(st, CODEX).get("state"), pstate(st, CODEX).get("source")) == ("ok", "stale_hold")
    assert pstate(st, CLAUDE).get("source") == "policy"
    feed(st, NOW + 61 * M, claude=(10, 30))
    d = step(st, NOW + 61 * M)
    assert d["level"] == "claude", d
    assert (pstate(st, CODEX).get("state"), pstate(st, CODEX).get("source")) == ("constrained", "stale")
    assert (pstate(st, CLAUDE).get("state"), pstate(st, CLAUDE).get("source")) == ("ok", "policy")
    lines = throttle_lines(st)
    assert lines == [
        "quota-router: throttle (shadow) stale usage data for openai-codex (61m old); ok → constrained "
        "(Weekly last 80%)",
        "quota-router: throttle (shadow) codex → claude (openai-codex constrained)"]
    for later in (NOW + 66 * M, NOW + 71 * M):
        feed(st, later, claude=(10, 30))
        step(st, later)
    assert throttle_lines(st) == []                   # once per incident


def test_an_observed_block_constrains_only_that_provider():
    st = core.empty_state()
    feed(st, NOW, codex=(50, 30), claude=(10, 30), codex_reset=NOW + H)
    assert step(st, NOW)["level"] == "codex"
    core.record_error(st, provider=CODEX, status_code=429, reason="rate_limit",
                      message="usage limit reached", model="codex-x", now=NOW + M, cfg=CFG)
    d = step(st, NOW + M)
    assert d["level"] == "claude", d
    assert (pstate(st, CODEX).get("state"), pstate(st, CODEX).get("source")) == ("constrained", "block")
    assert (pstate(st, CLAUDE).get("state"), pstate(st, CLAUDE).get("source")) == ("ok", "policy")
    assert core.clear_block_on_success(st, CODEX, NOW + 5 * M)
    feed(st, NOW + 10 * M, codex=(50, 30), claude=(10, 30), codex_reset=NOW + H)
    assert step(st, NOW + 10 * M)["level"] == "claude"        # dwell since the block
    feed(st, NOW + 32 * M, codex=(50, 30), claude=(10, 30), codex_reset=NOW + H)
    assert step(st, NOW + 32 * M)["level"] == "codex"


def test_one_alert_per_provider_change_and_one_per_level_change():
    st = core.empty_state()
    feed(st, NOW, codex=(20, 30), claude=(10, 30))
    step(st, NOW, lcfg(enabled=True))
    throttle_lines(st)
    feed(st, NOW + 5 * M, codex=(20, 90), claude=(10, 30))
    for i in range(3):
        step(st, NOW + 5 * M + i, lcfg(enabled=True))
    assert throttle_lines(st) == [
        "quota-router: throttle openai-codex ok → constrained (Weekly 90%; rule b)",
        "quota-router: throttle codex → claude (openai-codex constrained)"]
    feed(st, NOW + 10 * M, codex=(20, 90), claude=(90, 30))
    step(st, NOW + 10 * M)
    assert throttle_lines(st) == [
        "quota-router: throttle (shadow) anthropic ok → constrained (Current session 90%; rule b)",
        "quota-router: throttle (shadow) claude → internal (anthropic constrained)"]


def test_decision_log_records_every_provider():
    st = core.empty_state()
    feed(st, NOW, codex=(20, 90), claude=(10, 30))
    step(st, NOW)
    rec = st["throttle"]["log"][-1]
    assert rec["level"] == "claude", rec
    per = rec.get("providers") or {}
    assert (per.get(CODEX) or {}).get("state") == "constrained" and per[CODEX]["rule"] == "b", rec
    assert set(per[CODEX]["windows"]) == {"Session", "Weekly"}
    assert set(per[CLAUDE]["windows"]) == {"Current session", "Current week"}


# ── hold / disable with ladder names ───────────────────────────────────────────

def test_a_held_ladder_level_forces_the_provider_states_it_implies():
    st = core.empty_state()
    feed(st, NOW, codex=(10, 30), claude=(10, 30))
    st["throttle"] = {"hold": {"level": "claude", "until": NOW + H, "since": NOW}}   # as `throttle hold` writes it
    d = step(st, NOW + 1)
    assert (d["level"], d["source"]) == ("claude", "hold")
    # holding "claude" means "treat codex as capped": the states say so, and survive the hold
    assert pstate(st, CODEX).get("state") == "constrained" and pstate(st, CLAUDE).get("state") == "ok"
    st["throttle"]["disabled"] = {"level": "internal", "since": NOW + 2}
    d = step(st, NOW + H + 1)                                  # hold expired: disable wins
    assert (d["level"], d["source"]) == ("internal", "disable")
    assert pstate(st, CODEX).get("state") == pstate(st, CLAUDE).get("state") == "constrained"
    del st["throttle"]["disabled"]
    feed(st, NOW + H + 2, codex=(10, 30), claude=(10, 30))
    assert step(st, NOW + H + 2)["level"] == "codex"            # both dwells ran out long ago


def test_a_hold_or_disable_naming_a_level_the_ladder_does_not_have_is_ignored_with_one_alert():
    st = core.empty_state()
    feed(st, NOW, codex=(20, 90), claude=(10, 30))
    st["throttle"] = {"hold": {"level": "constrained", "until": NOW + H, "since": NOW},  # left by `levels:`
                      "disabled": {"level": "fast", "since": NOW}}
    d = step(st, NOW)
    assert (d["level"], d["source"]) == ("claude", "policy"), d
    assert throttle_lines(st)[0] == ("quota-router: throttle (shadow) ignoring hold 'constrained' and disable "
                                     "'fast': not a ladder level (codex, claude, internal)")
    step(st, NOW + 60)
    assert throttle_lines(st) == []
    assert st["throttle"]["hold"]["level"] == "constrained"          # kept, for a config revert


def test_a_single_gate_ladder_alerts_like_the_legacy_levels():
    ladder = [{"name": "claude", "gate": [CLAUDE], "provider": CLAUDE, "model": "opus-x"},
              {"name": "internal", "provider": "custom:internal", "model": "glm-x"}]
    st = core.empty_state()
    feed(st, NOW, claude=(88, 30))
    d = step(st, NOW, core.throttle_config({"ladder": ladder, "profiles": []}))
    assert (d["level"], d["source"], d["rule"], d["label"]) == ("internal", "policy", "b", "Current session")
    assert throttle_lines(st) == ["quota-router: throttle (shadow) claude → internal (Current session 88%; rule b)"]
    assert "providers" not in st["throttle"]["log"][-1]


# ── validation (generalised B1), through the child's own refusal ───────────────

def _refusal(levels, level="codex"):
    """apply_one validates before it reads anything, so an invalid ladder never touches a file."""
    tspec = importlib.util.spec_from_file_location("qr_thr_validate", _root / "throttle.py")
    thr = importlib.util.module_from_spec(tspec)
    tspec.loader.exec_module(thr)
    r = thr.apply_one(Path("/nonexistent/config.yaml"), level, levels, backup_dir=Path("/nonexistent"),
                      label="w", now=NOW)
    return r.get("message", "") if r.get("result") == "error" else None


def test_validation_rejects_a_chain_with_a_provider_that_gates_another_level_only():
    leaky = [LADDER[0], dict(LADDER[1], fallback=[{"provider": CODEX, "model": "codex-x"}]), LADDER[2]]
    msg = _refusal(leaky)
    assert msg and msg.startswith("throttle.ladder.claude must not contain openai-codex"), msg
    floor_leak = [*LADDER[:2], dict(LADDER[2], fallback=[{"provider": CLAUDE, "model": "opus-x"}])]
    msg = _refusal(floor_leak)
    assert msg and msg.startswith("throttle.ladder.internal must not contain anthropic"), msg
    primary_leak = [dict(LADDER[0], provider=CLAUDE), *LADDER[1:]]
    msg = _refusal(primary_leak)
    assert msg and msg.startswith("throttle.ladder.codex must not contain anthropic"), msg


def test_validation_rejects_a_ladder_whose_last_level_is_gated():
    msg = _refusal([*LADDER[:2], dict(LADDER[2], gate=[CLAUDE])])
    assert msg == "throttle.ladder.internal is the floor and must have no gate", msg


@pytest.mark.parametrize("bad, expect", [
    ([LADDER[2]], "throttle.ladder needs at least two levels"),
    ([LADDER[0], dict(LADDER[1], gate=[]), LADDER[2]], "throttle.ladder.claude has no gate"),
    ([LADDER[0], dict(LADDER[1], name="codex"), LADDER[2]], "throttle.ladder has two levels named 'codex'"),
    ([dict(LADDER[0], gate=["custom:internal"]), *LADDER[1:]], "throttle.ladder.codex.gate: custom:internal"),
    ([dict(LADDER[0], gate=CODEX), *LADDER[1:]], "throttle.ladder.codex.gate must be a list"),
    ([dict(LADDER[0], fallbacks=[]), *LADDER[1:]], "throttle.ladder.codex has unknown keys: fallbacks"),
    ([dict(LADDER[0], model=""), *LADDER[1:]], "throttle.ladder.codex needs provider and model"),
])
def test_validation_rejects_other_malformed_ladders(bad, expect):
    msg = _refusal(bad)
    assert msg and msg.startswith(expect), msg


def test_a_valid_ladder_passes_validation_including_a_level_containing_its_own_gate_provider():
    both = [dict(LADDER[0], gate=[CODEX, CLAUDE], fallback=[{"provider": CLAUDE, "model": "opus-x"}]),
            *LADDER[1:]]
    for ladder in (LADDER, both):
        msg = _refusal(ladder)
        assert msg is None or not msg.startswith("throttle."), msg     # it got past validation


# ── legacy equivalence: the 0.3.0 throttle_step as the oracle ──────────────────

def legacy_step(state: dict, now: float, cfg: dict, t: dict) -> dict:
    """``core.throttle_step`` as of 0.3.0 (be638de), verbatim apart from ``core.`` prefixes and the
    level tuple spelled out. Every helper it calls is unchanged by the ladder change."""
    levels = ("fast", "constrained")
    th = state.setdefault("throttle", {})
    level = th.get("level") if th.get("level") in levels else "fast"
    since = float(th.get("since") or 0.0)
    provider = t["protected_provider"]
    windows, fetched_at = core._throttle_windows(state, now, cfg, provider)
    hold = th.get("hold")
    if hold and float(hold.get("until") or 0) <= now:
        th.pop("hold", None)
        hold = None
    rule = label = None
    stale_alerted = False
    stale_age = None if fetched_at is None else now - fetched_at
    if hold:
        new, source = hold["level"], "hold"
    elif th.get("disabled"):
        new, source = th["disabled"]["level"], "disable"
    elif core.active_block((state.get("blocks") or {}).get(provider), now):
        new, source = "constrained", "block"
    elif stale_age is None or stale_age > t["stale_hold_s"]:
        known = [w for w in windows if not (w.get("resets_at") and w["resets_at"] <= now)]
        high = [w for w in known if w["used"] >= t["leave_used"]]
        new, source = ("constrained" if high else "fast"), "stale"
        if high and th.get("stale_alerted_for") != fetched_at:
            th["stale_alerted_for"] = fetched_at
            stale_alerted = True
            tag = "" if t.get("enabled") else " (shadow)"
            core.queue(state, f"quota-router: throttle{tag} stale usage data ({int((stale_age or 0) // 60)}m old); "
                              f"{level} → constrained ({high[0]['label']} last {high[0]['used']:.0f}%)")
    elif stale_age > cfg["stale_after_s"]:
        new, source = level, "stale_hold"
    else:
        new, rule, label = core.throttle_policy(level, since, windows, now, t)
        source = "policy"
    if source != "stale":
        th.pop("stale_alerted_for", None)
    changed = new != level
    if changed:
        tag = "" if t.get("enabled") else " (shadow)"
        if not stale_alerted:
            core.queue(state, f"quota-router: throttle{tag} {level} → {new} ({core._why(source, rule, label, windows)})")
        th["since"] = now
    elif not th.get("since"):
        th["since"] = now
    th.update(level=new, source=source, rule=rule, decided_at=now)
    wsum = {}
    for w in windows:
        f = w.get("forecast") or {}
        ok = f.get("status") == "ok"
        wsum[w["label"]] = {"used": w["used"], "projected": f.get("projected_at_reset") if ok else None,
                            "projected_short": f.get("projected_short") if ok else None,
                            "r_long": f.get("rate_pct_h") if ok else None,
                            "r_short": f.get("r_short") if ok else None}
    decision = {"level": new, "previous": level, "changed": changed, "source": source, "rule": rule,
                "label": label, "windows": wsum, "at": now}
    log = th.get("log") if isinstance(th.get("log"), list) else []
    th["log"] = (log + [{k: decision[k] for k in ("at", "level", "source", "rule", "label", "windows")}]
                 )[-core.THROTTLE_LOG_MAX:]
    return decision


DECISION_KEYS = ("level", "previous", "changed", "source", "rule", "label", "windows", "at")
TH_KEYS = ("level", "since", "source", "rule", "decided_at", "hold", "disabled", "stale_alerted_for")


def _feed_one(st, provider, t, a, b, a_reset, b_reset):
    labels = {"anthropic": ("Current session", "Current week"), "openai-codex": ("Session", "Weekly")}[provider]
    core.apply_refresh(st, {provider: {"fetched_at": t, "windows": [
        {"label": labels[0], "used_percent": a, "resets_at": a_reset},
        {"label": labels[1], "used_percent": b, "resets_at": b_reset}]}}, t, CFG)


def _run_pair(rows, t, provider="anthropic"):
    """Apply the same rows to two states; step one with the oracle and one with the new code; compare
    every decision, the recorded throttle fields, the log and the queued throttle lines.

    ``feed``/``block``/``clear`` act on the protected provider; ``feedp``/``blockp``/``clearp`` name
    one; ``protect`` switches ``protected_provider`` on the live config, as editing config.yaml does."""
    old, new = core.empty_state(), core.empty_state()
    steps = 0
    for minute, op, *args in rows:
        now = NOW + minute * M
        if op == "protect":
            t, provider = dict(t, protected_provider=args[0]), args[0]
            continue
        target = args[0] if op in ("feedp", "blockp", "clearp") else provider
        args = args[1:] if op in ("feedp", "blockp", "clearp") else args
        for st in (old, new):
            if op in ("feed", "feedp"):   # (session used, session reset minute=+180, week used=40)
                a, a_reset, b = (list(args) + [None, None])[:3]
                _feed_one(st, target, now, a, 40.0 if b is None else b,
                          NOW + (minute + 180 if a_reset is None else a_reset) * M, NOW + 5 * 24 * H)
            elif op in ("block", "blockp"):
                core.record_error(st, provider=target, status_code=429, reason="rate_limit",
                                  message="usage limit reached", model="m", now=now, cfg=CFG)
            elif op in ("clear", "clearp"):
                core.clear_block_on_success(st, target, now)
            elif op == "hold":
                core.throttle_hold(st, args[0], now + args[1] * M, now)
            elif op == "unhold":
                core.throttle_clear_hold(st)
            elif op == "disable":
                core.throttle_disable(st, args[0], now)
            elif op == "undisable":
                core.throttle_disable(st, None, now)
        if op != "step":
            continue
        steps += 1
        d_old, d_new = legacy_step(old, now, CFG, t), core.throttle_step(new, now, CFG, t)
        where = f"minute {minute}"
        assert {k: d_new[k] for k in DECISION_KEYS} == {k: d_old[k] for k in DECISION_KEYS}, f"{where}: decision"
        assert {k: new["throttle"].get(k) for k in TH_KEYS} == {k: old["throttle"].get(k) for k in TH_KEYS}, \
            f"{where}: recorded throttle fields"
        assert new["throttle"]["log"][-1] == old["throttle"]["log"][-1], f"{where}: log record"
        assert new["pending"] == old["pending"], f"{where}: queued lines"
        mapped = "constrained" if new["throttle"]["level"] == "constrained" else "ok"
        assert pstate(new, provider).get("state") == mapped, f"{where}: {provider} state is not the level's"
    assert steps
    return old, new


def _ramp(start, n, first, step_, every=5):
    rows = []
    for i in range(n):
        rows += [(start + i * every, "feed", first + step_ * i), (start + i * every, "step")]
    return rows


SCENARIOS = {
    "rule b in, leave after dwell": [
        (0, "feed", 86), (0, "step"), (5, "feed", 70), (5, "step"), (20, "step"), (31, "feed", 70), (31, "step")],
    "rule a ramp": _ramp(-55, 12, 30, 2),
    "rule c acceleration": [
        row for i in range(25) for row in ((-150 + 5 * i, "feed", 60 + 0.25 * i, 210), (-150 + 5 * i, "step"))] + [
        row for i in range(6) for row in ((-25 + 5 * i, "feed", 67 + i, 210), (-25 + 5 * i, "step"))],
    "reset exception then block": [
        (0, "feed", 96, 10), (0, "step"), (0, "block"), (0.1, "step"), (5, "step")],
    "block expires at the window reset": [
        (0, "feed", 50, 60), (0, "block"), (1, "step"), (61, "feed", 5, 360), (61, "step"), (95, "step")],
    "stale hold, stale escalate once, fresh again": [
        (0, "feed", 78), (0, "step"), (45, "step"), (61, "step"), (66, "step"), (71, "step"),
        (80, "feed", 60), (80, "step"), (112, "step"), (145, "step")],
    "stale while constrained": [
        (0, "feed", 86), (0, "step"), (31, "step"), (61, "step"), (95, "step"), (100, "feed", 10), (100, "step")],
    "stale low returns to fast": [(0, "feed", 86), (0, "step"), (31, "feed", 70), (31, "step"), (92, "step")],
    "never any data": [(0, "step"), (90, "step")],
    "precedence hold > disable > block > policy": [
        (0, "feed", 86), (0, "block"), (0, "step"), (0.1, "disable", "fast"), (0.2, "step"),
        (0.3, "hold", "constrained", 120), (0.4, "step"), (120.5, "step"), (120.6, "undisable"), (120.7, "step")],
    "short hold of constrained while low keeps the dwell": [
        (0, "feed", 10), (0, "step"), (1, "hold", "constrained", 10), (1, "step"), (11, "feed", 10),
        (11, "step"), (25, "feed", 10), (25, "step"), (32, "feed", 10), (32, "step")],
    "hold fast while high": [
        (0, "feed", 90), (0, "hold", "fast", 30), (0, "step"), (10, "feed", 92), (10, "step"),
        (31, "feed", 92), (31, "step"), (35, "unhold"), (35, "step")],
    "jitter 84/86": [row for i, u in enumerate([84, 86] * 8) for row in ((5 * i, "feed", u), (5 * i, "step"))],
}


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_legacy_config_decides_exactly_as_the_old_code(name, enabled):
    t = core.throttle_config({"levels": LEVELS, "profiles": ["worker"], "enabled": enabled})
    _run_pair(SCENARIOS[name], t)


def _random_rows(seed, n=240):
    rnd = random.Random(seed)
    rows, used, minute, reset = [], 30.0, 0.0, 180.0
    for _ in range(n):
        minute += rnd.choice([1, 5, 5, 5, 15, 40, 70])
        if minute >= reset:
            used, reset = rnd.uniform(0, 20), minute + rnd.choice([35, 120, 300])
        used = min(100.0, max(0.0, used + rnd.uniform(-3, 9)))
        r = rnd.random()
        if r < 0.75:
            rows.append((minute, "feed", round(used, 2), reset, rnd.choice([40, 80, 90])))
        elif r < 0.80:
            rows.append((minute, "block"))
        elif r < 0.84:
            rows.append((minute, "clear"))
        elif r < 0.88:
            rows.append((minute, "hold", rnd.choice(["fast", "constrained"]), rnd.choice([5, 30, 90])))
        elif r < 0.90:
            rows.append((minute, "unhold"))
        elif r < 0.93:
            rows.append((minute, "disable", rnd.choice(["fast", "constrained"])))
        elif r < 0.96:
            rows.append((minute, "undisable"))
        rows.append((minute, "step"))
    return rows


@pytest.mark.parametrize("seed", range(12))
def test_legacy_config_matches_the_old_code_over_random_replays(seed):
    t = core.throttle_config({"levels": LEVELS, "profiles": [], "enabled": seed % 2 == 0,
                              "min_dwell_s": 20 * M if seed % 3 == 0 else 30 * M})
    old, _new = _run_pair(_random_rows(seed), t)
    sources = {rec["source"] for rec in old["throttle"]["log"]}
    assert len(sources) >= 3, sources          # the replay exercised more than one branch


def test_switching_protected_provider_on_a_live_legacy_config_carries_the_level_and_dwell_over():
    """0.3.0 keeps one level and one ``since`` whatever the protected provider is, so switching
    ``protected_provider`` hands both to the new provider, and switching back ignores that provider's
    own earlier state."""
    rows = [(0, "feedp", CLAUDE, 86), (0, "feedp", CODEX, 10), (0, "step"),          # anthropic: rule b
            (5, "protect", CODEX), (5, "feedp", CODEX, 10), (5, "step"),             # constrained since 0: dwell
            (20, "feedp", CODEX, 10), (20, "step"),
            (31, "feedp", CODEX, 10), (31, "step"),                                  # dwell over: leave
            (40, "feedp", CODEX, 90), (40, "step"),                                  # codex: rule b, since 40
            (45, "protect", CLAUDE), (45, "feedp", CLAUDE, 10), (45, "step"),        # since 40, not 0: dwell
            (71, "feedp", CLAUDE, 10), (71, "step")]                                 # 31 min after 40: leave
    old, new = _run_pair(rows, core.throttle_config({"levels": LEVELS, "profiles": []}))
    assert [r["rule"] for r in old["throttle"]["log"]] == ["b", "dwell", "dwell", "leave", "b", "dwell", "leave"]
    assert [r["level"] for r in new["throttle"]["log"]][-2:] == ["constrained", "fast"]


def _random_switch_rows(seed, n=300):
    """Both providers fed, blocked and cleared at random, with ``protected_provider`` switched live
    (including an odd spelling core has no windows for)."""
    rnd = random.Random(1000 + seed)
    rows, minute = [], 0.0
    used, reset = {CLAUDE: 30.0, CODEX: 30.0}, {CLAUDE: 180.0, CODEX: 120.0}
    for _ in range(n):
        minute += rnd.choice([1, 5, 5, 5, 15, 40, 70])
        for p in used:
            if minute >= reset[p]:
                used[p], reset[p] = rnd.uniform(0, 20), minute + rnd.choice([35, 120, 300])
            used[p] = min(100.0, max(0.0, used[p] + rnd.uniform(-3, 9)))
        p = rnd.choice([CLAUDE, CODEX])
        r = rnd.random()
        if r < 0.65:
            rows.append((minute, "feedp", p, round(used[p], 2), reset[p], rnd.choice([40, 80, 90])))
        elif r < 0.73:
            rows.append((minute, "protect", rnd.choice([CLAUDE, CODEX, CLAUDE, CODEX, "Anthropic"])))
        elif r < 0.77:
            rows.append((minute, "blockp", p))
        elif r < 0.80:
            rows.append((minute, "clearp", p))
        elif r < 0.84:
            rows.append((minute, "hold", rnd.choice(["fast", "constrained"]), rnd.choice([5, 30, 90])))
        elif r < 0.86:
            rows.append((minute, "unhold"))
        elif r < 0.89:
            rows.append((minute, "disable", rnd.choice(["fast", "constrained"])))
        elif r < 0.92:
            rows.append((minute, "undisable"))
        rows.append((minute, "step"))
    return rows


@pytest.mark.parametrize("seed", range(12))
def test_legacy_config_matches_the_old_code_across_protected_provider_switches(seed):
    rows = _random_switch_rows(seed)
    t = core.throttle_config({"levels": LEVELS, "profiles": [], "enabled": seed % 2 == 0})
    old, _new = _run_pair(rows, t)
    assert sum(1 for r in rows if r[1] == "protect") >= 10
    log = old["throttle"]["log"]
    assert {rec["rule"] for rec in log} & {"a", "b", "c"}                          # a policy entry happened
    assert len({rec["source"] for rec in log}) >= 3                              # and more than one branch ran


def test_legacy_config_protecting_codex_matches_the_old_code():
    t = core.throttle_config({"levels": LEVELS, "profiles": [], "protected_provider": CODEX})
    _run_pair(SCENARIOS["rule b in, leave after dwell"] + [(40, "feed", 90), (40, "step")], t, provider=CODEX)


# ── legacy state migration ─────────────────────────────────────────────────────

def _old_state_file(tmp_path, *, hold_and_disable=False):
    """A 0.3.0 state file: constrained since NOW (rule b); then either a stale step that sent the
    stale alert for this snapshot, or a disable (fast) and a one-hour hold (constrained)."""
    st = core.empty_state()
    t = core.throttle_config({"levels": LEVELS, "profiles": []})
    feed(st, NOW, claude=(86, 30))
    legacy_step(st, NOW, CFG, t)
    if hold_and_disable:
        core.throttle_disable(st, "fast", NOW + M)
        core.throttle_hold(st, "constrained", NOW + H, NOW + M)
    else:
        legacy_step(st, NOW + 2 * H, CFG, t)
        assert st["throttle"]["stale_alerted_for"] == NOW
    core.drain_pending(st)
    path = tmp_path / "state.json"
    path.write_text(json.dumps(st))
    return path, t


def _continue_pair(path, t, rows):
    old, new = core.read_state(path), core.read_state(path)
    assert "providers" not in new["throttle"]
    for minute, op, *args in rows:
        now = NOW + minute * M
        if op == "feed":
            for st in (old, new):
                feed(st, now, claude=tuple(args))
            continue
        d_old, d_new = legacy_step(old, now, CFG, t), core.throttle_step(new, now, CFG, t)
        assert {k: d_new[k] for k in DECISION_KEYS} == {k: d_old[k] for k in DECISION_KEYS}, minute
        assert {k: new["throttle"].get(k) for k in TH_KEYS} == {k: old["throttle"].get(k) for k in TH_KEYS}
        assert new["pending"] == old["pending"], minute
    return old, new


def test_old_state_file_migrates_to_per_provider_state_and_keeps_the_stale_dedupe(tmp_path):
    path, t = _old_state_file(tmp_path)
    old, new = _continue_pair(path, t, [(125, "step")])
    assert new["pending"] == []                                # the stale alert is not re-sent
    migrated = (new["throttle"].get("providers") or {}).get("anthropic") or {}
    assert (migrated.get("state"), migrated.get("since"), migrated.get("stale_alerted_for")) == \
        ("constrained", NOW, NOW), migrated
    assert new["throttle"].get("stale_alerted_for") == NOW                   # still where 0.3.0 reads it
    old, new = _continue_pair(path, t, [(125, "step"), (130, "feed", 10, 10), (130, "step")])
    assert new["throttle"]["level"] == "fast"


def test_old_state_file_keeps_its_hold_and_disable(tmp_path):
    path, t = _old_state_file(tmp_path, hold_and_disable=True)
    old, new = _continue_pair(path, t, [(5, "step"), (61, "step"), (65, "feed", 10, 10), (65, "step")])
    assert [r["source"] for r in new["throttle"]["log"][-3:]] == ["hold", "disable", "disable"]
    assert new["throttle"]["level"] == "fast" and "hold" not in new["throttle"]
    assert ((new["throttle"].get("providers") or {}).get("anthropic") or {}).get("state") == "ok"


def test_old_state_dwell_survives_the_migration():
    st = core.empty_state()
    t = core.throttle_config({"levels": LEVELS, "profiles": []})
    feed(st, NOW, claude=(86, 30))
    legacy_step(st, NOW, CFG, t)
    assert "providers" not in st["throttle"]
    feed(st, NOW + 20 * M, claude=(10, 10))
    d = core.throttle_step(st, NOW + 20 * M, CFG, t)
    assert (d["level"], d["rule"]) == ("constrained", "dwell")
    assert pstate(st, "anthropic").get("since") == NOW
    feed(st, NOW + 31 * M, claude=(10, 10))
    assert core.throttle_step(st, NOW + 31 * M, CFG, t)["level"] == "fast"


def test_a_downgrade_to_0_3_0_and_back_keeps_the_level_0_3_0_decided():
    """0.3.0 ignores ``throttle.providers``; after it decides, 0.4.0 must re-read its level."""
    t = core.throttle_config({"levels": LEVELS, "profiles": []})
    st = core.empty_state()
    feed(st, NOW, claude=(10, 30))
    assert core.throttle_step(st, NOW, CFG, t)["level"] == "fast"            # 0.4.0
    feed(st, NOW + 5 * M, claude=(86, 30))
    assert legacy_step(st, NOW + 5 * M, CFG, t)["level"] == "constrained"    # downgraded: 0.3.0 decides
    ref = copy.deepcopy(st)
    for now in (NOW + 10 * M, NOW + 36 * M):                                   # upgraded again
        feed(st, now, claude=(10, 10))
        feed(ref, now, claude=(10, 10))
        d_new, d_old = core.throttle_step(st, now, CFG, t), legacy_step(ref, now, CFG, t)
        assert {k: d_new[k] for k in DECISION_KEYS} == {k: d_old[k] for k in DECISION_KEYS}, now
    assert [r["rule"] for r in st["throttle"]["log"][-2:]] == ["dwell", "leave"]


def test_a_downgrade_to_0_3_0_does_not_resend_the_stale_alert():
    """With one gate provider the stale-alert dedupe is also kept where 0.3.0 reads it."""
    t = core.throttle_config({"levels": LEVELS, "profiles": []})
    st = core.empty_state()
    feed(st, NOW, claude=(86, 30))
    core.throttle_step(st, NOW, CFG, t)
    core.throttle_step(st, NOW + 2 * H, CFG, t)                     # stale, last known high: alert once
    assert [ln for ln in core.drain_pending(st) if "stale usage data" in ln]
    assert st["throttle"].get("stale_alerted_for") == NOW
    legacy_step(st, NOW + 2 * H + 5 * M, CFG, t)                    # downgraded: 0.3.0, same snapshot
    assert core.drain_pending(st) == []
    core.throttle_step(st, NOW + 2 * H + 10 * M, CFG, t)            # upgraded again
    assert core.drain_pending(st) == []


def test_switching_a_constrained_legacy_state_to_a_ladder_keeps_anthropic_constrained():
    st = core.empty_state()
    t = core.throttle_config({"levels": LEVELS, "profiles": []})
    feed(st, NOW, codex=(10, 10), claude=(86, 30))
    legacy_step(st, NOW, CFG, t)
    feed(st, NOW + 5 * M, codex=(90, 10), claude=(10, 10))
    d = step(st, NOW + 5 * M)
    assert d["previous"] == "codex" and d["level"] == "internal", d   # anthropic still in its dwell
    assert pstate(st, CLAUDE).get("since") == NOW


# ── apply-one / reconcile across N levels (temp HERMES_HOME) ───────────────────

import os                    # noqa: E402  (apply section)
import subprocess            # noqa: E402
import sys                   # noqa: E402

REAL_STATE = Path.home() / ".hermes" / "cache" / "quota-router" / "state.json"
CODEX_YAML = """\
# worker profile
model:
  default: codex-x
  provider: openai-codex
display:
  skin: example
fallback_providers:
  - provider: custom:internal
    model: glm-x
"""
ROOT_YAML = """\
model:
  default: opus-x
  provider: anthropic
kanban:
  max_in_progress: 64
"""


def _load(name, file, monkeypatch):
    spec = importlib.util.spec_from_file_location(name, _root / file)
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, mod)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def env(tmp_path, monkeypatch):
    real_mtime = REAL_STATE.stat().st_mtime if REAL_STATE.exists() else None
    home = tmp_path / ".hermes"
    (home / "profiles" / "worker").mkdir(parents=True)
    (home / "profiles" / "worker" / "config.yaml").write_text(CODEX_YAML)
    (home / "config.yaml").write_text(ROOT_YAML)
    monkeypatch.setenv("HERMES_HOME", str(home))
    c = _load("quota_router_core", "core.py", monkeypatch)
    c.state_path = lambda hermes_home=None: home / "cache" / "quota-router" / "state.json"
    thr = _load("quota_router_throttle", "throttle.py", monkeypatch)
    plugin = _load("qr_plugin_laddercase", "__init__.py", monkeypatch)

    class Env:
        pass

    e = Env()
    e.home, e.core, e.thr, e.plugin = home, c, thr, plugin
    e.profile_cfg = home / "profiles" / "worker" / "config.yaml"
    e.root_cfg = home / "config.yaml"
    e.calls = []
    e.now = NOW

    def runner(profile, argv):
        """In-process stand-in for `hermes -p <profile> quota-router throttle apply-one ...`."""
        e.calls.append((profile, list(argv)))
        path = e.root_cfg if profile == "default" else home / "profiles" / profile / "config.yaml"
        args = thr.parse_apply_one_argv(argv)
        res = thr.apply_one(path, args["level"], args["levels"], kanban=args["kanban"],
                            backup_dir=home / "backups" / "quota-throttle", label=profile, now=e.now)
        return 0, thr.RESULT_PREFIX + json.dumps(res) + "\n", ""

    e.runner = runner
    yield e
    if real_mtime is not None:
        assert REAL_STATE.stat().st_mtime == real_mtime, "real state file was touched"


def etcfg(e, **over):
    return e.core.throttle_config({"ladder": LADDER, "profiles": ["worker"], "enabled": True, **over})


def efeed(e, now, codex=(10, 20), claude=(10, 20)):
    e.now = now
    snaps = {CODEX: {"fetched_at": now, "windows": [
        {"label": "Session", "used_percent": codex[0], "resets_at": now + 3 * H},
        {"label": "Weekly", "used_percent": codex[1], "resets_at": now + 3 * 24 * H}]},
        CLAUDE: {"fetched_at": now, "windows": [
            {"label": "Current session", "used_percent": claude[0], "resets_at": now + 3 * H},
            {"label": "Current week", "used_percent": claude[1], "resets_at": now + 3 * 24 * H}]}}
    e.plugin.refresh(now=now, snapshots=snaps, drain=False)


def eapply(e, t=None, now=None):
    e.now = now if now is not None else e.now
    return e.thr.run_apply(e.core.state_path(), dict(e.core.DEFAULTS), t or etcfg(e), e.runner, e.now)


def model_of(e, path=None):
    raw = e.thr.read_raw(path or e.profile_cfg)
    return raw["model"]["provider"], raw["model"]["default"], [
        (f["provider"], f["model"]) for f in raw.get("fallback_providers") or []]


def test_apply_one_moves_between_any_two_of_three_levels(env):
    bdir = env.home / "b"
    r = env.thr.apply_one(env.profile_cfg, "codex", LADDER, backup_dir=bdir, label="worker", now=NOW)
    assert r == {"result": "noop", "actual": "codex"}
    r = env.thr.apply_one(env.profile_cfg, "internal", LADDER, backup_dir=bdir, label="worker", now=NOW)
    assert (r["result"], r["actual"]) == ("applied", "codex"), r
    assert model_of(env) == ("custom:internal", "glm-x", [("backup", "backup-x")])
    r = env.thr.apply_one(env.profile_cfg, "claude", LADDER, backup_dir=bdir, label="worker", now=NOW)
    assert (r["result"], r["actual"]) == ("applied", "internal"), r
    assert model_of(env) == ("anthropic", "opus-x", [("custom:internal", "glm-x")])
    r = env.thr.apply_one(env.profile_cfg, "codex", LADDER, backup_dir=bdir, label="worker", now=NOW)
    assert (r["result"], r["actual"]) == ("applied", "claude"), r
    assert model_of(env) == ("openai-codex", "codex-x", [("custom:internal", "glm-x")])
    assert len(list(bdir.iterdir())) == 3
    assert env.thr.read_raw(env.profile_cfg)["display"] == {"skin": "example"}


def test_apply_one_skips_a_config_matching_no_ladder_level(env):
    env.profile_cfg.write_text(CODEX_YAML.replace("default: codex-x", "default: hand-picked"))
    before = env.profile_cfg.read_text()
    r = env.thr.apply_one(env.profile_cfg, "claude", LADDER, backup_dir=env.home / "b", label="w", now=NOW)
    assert r == {"result": "drift", "actual": None}
    assert env.profile_cfg.read_text() == before


def test_apply_one_refuses_an_invalid_ladder(env):
    leaky = [LADDER[0], dict(LADDER[1], fallback=[{"provider": CODEX, "model": "codex-x"}]), LADDER[2]]
    r = env.thr.apply_one(env.profile_cfg, "claude", leaky, backup_dir=env.home / "b", label="w", now=NOW)
    assert r["result"] == "error" and "must not contain openai-codex" in r["message"], r
    assert env.profile_cfg.read_text() == CODEX_YAML
    r = env.thr.apply_one(env.profile_cfg, "fast", LADDER, backup_dir=env.home / "b", label="w", now=NOW)
    assert r == {"result": "error", "actual": None, "message": "unknown level 'fast'"}


def test_run_apply_follows_the_ladder_and_alerts_once_per_change(env):
    efeed(env, NOW)
    assert eapply(env) == []
    assert [c[0] for c in env.calls] == ["worker"] and model_of(env)[0] == "openai-codex"
    efeed(env, NOW + 5 * M, codex=(10, 90))
    lines = eapply(env)
    assert model_of(env)[0] == "anthropic"
    assert [ln for ln in lines if "throttle" in ln] == [
        "quota-router: throttle openai-codex ok → constrained (Weekly 90%; rule b)",
        "quota-router: throttle codex → claude (openai-codex constrained)"]
    efeed(env, NOW + 10 * M, codex=(10, 90), claude=(90, 20))
    eapply(env)
    assert model_of(env)[0] == "custom:internal"
    assert env.core.read_state(env.core.state_path())["throttle"]["applied"]["worker"]["level"] == "internal"
    assert eapply(env, now=NOW + 11 * M) == []


def test_run_apply_skips_a_hand_edit_and_alerts_once(env):
    efeed(env, NOW, codex=(10, 90))
    env.profile_cfg.write_text(CODEX_YAML.replace("default: codex-x", "default: hand-picked"))
    lines = eapply(env)
    assert [ln for ln in lines if "skipped" in ln] == ["quota-router: throttle skipped worker (manual model change)"]
    assert eapply(env, now=NOW + 5 * M) == []
    assert model_of(env)[1] == "hand-picked"


def test_run_apply_refuses_an_invalid_ladder_and_writes_nothing(env):
    efeed(env, NOW, codex=(10, 90))
    gated_floor = [*LADDER[:2], dict(LADDER[2], gate=[CLAUDE])]
    lines = eapply(env, etcfg(env, ladder=gated_floor))
    assert env.calls == []
    assert [ln for ln in lines if "config invalid" in ln] == [
        "quota-router: throttle config invalid, not applying: "
        "throttle.ladder.internal is the floor and must have no gate"]
    assert eapply(env, etcfg(env, ladder=gated_floor), now=NOW + 60) == []
    leaky = [LADDER[0], dict(LADDER[1], fallback=[{"provider": CODEX, "model": "codex-x"}]), LADDER[2]]
    lines = eapply(env, etcfg(env, ladder=leaky), now=NOW + 120)
    assert env.calls == []
    assert len([ln for ln in lines if "config invalid" in ln and "must not contain openai-codex" in ln]) == 1
    lines = eapply(env, etcfg(env, ladder={"name": "codex"}), now=NOW + 150)
    assert [ln for ln in lines if "config invalid" in ln] == [
        "quota-router: throttle config invalid, not applying: "
        "throttle.ladder must be a list of levels, highest preference first"]
    no_codex_windows = dict(env.core.DEFAULTS, forecast_windows={CLAUDE: CFG["forecast_windows"][CLAUDE]})
    lines = env.thr.run_apply(env.core.state_path(), no_codex_windows, etcfg(env), env.runner, NOW + 180)
    assert env.calls == []
    assert [ln for ln in lines if "config invalid" in ln] == [
        "quota-router: throttle config invalid, not applying: throttle.ladder.codex gates on openai-codex, "
        "which has no forecast_windows, so its usage can never constrain it"]


def test_legacy_levels_are_refused_with_the_old_message(env):
    efeed(env, NOW)
    leaky = {**LEVELS, "constrained": {**LEVELS["constrained"], "fallback": [
        {"provider": "anthropic", "model": "opus-x"}]}}
    t = env.core.throttle_config({"levels": leaky, "profiles": ["worker"], "enabled": True})
    lines = eapply(env, t)
    assert env.calls == []
    assert [ln for ln in lines if "config invalid" in ln] == [
        "quota-router: throttle config invalid, not applying: "
        "throttle.levels.constrained must not contain anthropic (primary or fallback)"]
    both = env.core.throttle_config({"levels": LEVELS, "ladder": LADDER, "profiles": ["worker"], "enabled": True})
    lines = eapply(env, both, now=NOW + 60)
    assert [ln for ln in lines if "config invalid" in ln] == [
        "quota-router: throttle config invalid, not applying: set throttle.ladder or throttle.levels, not both"]


def test_kanban_key_is_managed_when_every_level_sets_it(env):
    efeed(env, NOW, codex=(10, 90))
    some = [dict(LADDER[0], max_in_progress=64), LADDER[1], LADDER[2]]
    eapply(env, etcfg(env, ladder=some))
    assert [c[0] for c in env.calls] == ["worker"]
    env.calls.clear()
    every = [dict(LADDER[0], max_in_progress=64), dict(LADDER[1], max_in_progress=40),
             dict(LADDER[2], max_in_progress=24)]
    eapply(env, etcfg(env, ladder=every), now=NOW + 60)
    assert [c[0] for c in env.calls] == ["worker", "default"]
    assert env.thr.read_raw(env.root_cfg)["kanban"]["max_in_progress"] == 40


def test_status_shows_each_provider_the_selected_level_and_leaks_per_gate_provider(env):
    import sqlite3
    efeed(env, NOW, codex=(10, 90))
    eapply(env)
    db = env.home / "profiles" / "worker" / "state.db"
    con = sqlite3.connect(db)
    con.execute("create table sessions (id text, started_at real, billing_provider text, model text)")
    con.executemany("insert into sessions values (?,?,?,?)", [
        ("a", NOW - 10, "openai-codex", "codex-x"),     # before codex was constrained
        ("b", NOW + 10, "anthropic", "opus-x"),         # the claude level: not a leak
        ("c", NOW + 20, "openai-codex", "codex-x")])    # leak
    con.commit()
    con.close()
    s = env.thr.throttle_status(env.core.read_state(env.core.state_path()), etcfg(env), env.home, NOW + 30)
    assert s["level"] == "claude", s
    assert [(r["name"], r["gate"]) for r in s.get("ladder") or []] == [
        ("codex", [CODEX]), ("claude", [CLAUDE]), ("internal", [])], s
    per = s.get("providers") or {}
    assert per[CODEX]["state"] == "constrained" and per[CODEX]["rule"] == "b"
    assert per[CLAUDE]["state"] == "ok"
    assert per[CODEX]["windows"]["Weekly"]["used"] == 90
    rec = s["profiles"]["worker"]
    assert rec.get("leaked_by_provider") == {CODEX: 1, CLAUDE: 0} and rec["leaked_sessions"] == 1, rec
    text = env.thr.render_throttle_status(s)
    assert "throttle: claude" in text
    assert "openai-codex: constrained" in text and "anthropic: ok" in text
    assert "leak 1 openai-codex, 0 anthropic sessions while constrained" in text


def test_legacy_status_text_is_unchanged(env):
    efeed(env, NOW, claude=(90, 40))
    t = env.core.throttle_config({"levels": LEVELS, "profiles": ["worker"], "enabled": True})
    lvl = {"fast": {**LEVELS["fast"], "fallback": LEVELS["fast"]["fallback"]}, "constrained": LEVELS["constrained"]}
    env.profile_cfg.write_text("model:\n  default: opus-x\n  provider: anthropic\nfallback_providers:\n"
                               "  - provider: custom:internal\n    model: glm-x\n  - provider: openai-codex\n"
                               "    model: codex-x\n")
    eapply(env, env.core.throttle_config({"levels": lvl, "profiles": ["worker"], "enabled": True}))
    s = env.thr.throttle_status(env.core.read_state(env.core.state_path()), t, env.home, NOW + 30)
    at = datetime.fromtimestamp(NOW).strftime("%a %H:%M")
    assert env.thr.render_throttle_status(s) == "\n".join([
        f"throttle: constrained  since {at}  source=policy rule=b",
        "    Current session   90.0%  → unknown",
        "    Current week      40.0%  → unknown",
        "  worker: applied; at constrained; leak ? anthropic sessions while constrained"])


def _cli(e, monkeypatch, t, *argv):
    import argparse
    parser = argparse.ArgumentParser()
    e.plugin._cli_setup(parser)
    args = parser.parse_args(list(argv))
    monkeypatch.setattr(e.plugin, "_throttle_cfg", lambda: t)
    monkeypatch.setattr(e.thr, "subprocess_runner", e.runner)
    return e.plugin._cli_handler(args)


def test_cli_hold_and_disable_take_ladder_level_names(env, monkeypatch, capsys):
    t = etcfg(env)
    efeed(env, NOW)
    monkeypatch.setattr(env.plugin.time, "time", lambda: env.now)
    assert _cli(env, monkeypatch, t, "throttle", "hold", "claude", "--for", "1h") == 0
    st = env.core.read_state(env.core.state_path())
    assert st["throttle"]["hold"]["level"] == "claude" and st["throttle"]["level"] == "claude"
    assert model_of(env)[0] == "anthropic"                      # a hold applies at once
    assert _cli(env, monkeypatch, t, "throttle", "hold", "fast", "--for", "1h") == 2
    assert _cli(env, monkeypatch, t, "throttle", "hold", "--clear") == 0
    assert _cli(env, monkeypatch, t, "throttle", "disable", "--level", "internal") == 0
    assert model_of(env)[0] == "custom:internal"
    assert _cli(env, monkeypatch, t, "throttle", "disable") == 0          # default: the first level
    st = env.core.read_state(env.core.state_path())
    assert st["throttle"]["disabled"]["level"] == "codex" and model_of(env)[0] == "openai-codex"
    capsys.readouterr()
    assert _cli(env, monkeypatch, t, "throttle", "status", "--json") == 0
    out = json.loads(capsys.readouterr().out.strip().split(" ", 1)[1])
    assert out["level"] == "codex" and set(out["providers"]) == {CODEX, CLAUDE}


def test_cli_apply_one_accepts_a_ladder(env, monkeypatch, capsys):
    import argparse
    argv = env.thr.apply_one_argv("internal", LADDER, kanban=False)
    monkeypatch.setenv("HERMES_HOME", str(env.home / "profiles" / "worker"))
    parser = argparse.ArgumentParser()
    env.plugin._cli_setup(parser)
    assert env.plugin._cli_handler(parser.parse_args(argv)) == 0
    res = env.thr.parse_result(capsys.readouterr().out)
    assert res == {"result": "applied", "actual": "codex"}, res
    assert model_of(env)[0] == "custom:internal"


HERMES_BIN = Path(os.environ.get("QR_HERMES_BIN", str(Path.home() / ".local" / "bin" / "hermes")))


@pytest.mark.skipif(not HERMES_BIN.exists() or os.environ.get("QR_SKIP_LIVE") == "1",
                    reason="no hermes CLI")
def test_live_apply_one_with_a_ladder_under_hermes_p(env):
    worker = env.home / "profiles" / "worker"
    (worker / "plugins").mkdir()
    (worker / "plugins" / "quota-router").symlink_to(_root, target_is_directory=True)
    env.profile_cfg.write_text(CODEX_YAML + "plugins:\n  enabled:\n    - quota-router\n")
    child_env = {k: v for k, v in os.environ.items() if not k.startswith("HERMES_")}
    child_env["HERMES_HOME"] = str(env.home)
    argv = env.thr.apply_one_argv("claude", LADDER, kanban=False)
    r = subprocess.run([str(HERMES_BIN), "-p", "worker", "quota-router", *argv],
                       capture_output=True, text=True, timeout=120, env=child_env)
    res = env.thr.parse_result(r.stdout)
    assert r.returncode == 0 and res == {"result": "applied", "actual": "codex"}, (r.stdout, r.stderr[-2000:])
    assert model_of(env) == ("anthropic", "opus-x", [("custom:internal", "glm-x")])


# ── each gate provider is the legacy policy, run once per provider ──────────────

def test_each_gate_provider_decides_exactly_as_a_legacy_throttle_protecting_it():
    """The per-provider policy is the legacy policy run once per provider: a ladder's provider
    states equal a legacy config protecting that provider, step for step."""
    rnd = random.Random(7)
    ladder_st = core.empty_state()
    legacy = {p: core.empty_state() for p in (CODEX, CLAUDE)}
    used = {CODEX: 30.0, CLAUDE: 30.0}
    for i in range(200):
        now = NOW + i * 5 * M
        for p in used:
            used[p] = min(100.0, max(0.0, used[p] + rnd.uniform(-4, 5)))
        snap = {}
        if rnd.random() > 0.1:
            snap["codex"] = (used[CODEX], 40)
        if rnd.random() > 0.1:
            snap["claude"] = (used[CLAUDE], 40)
        for st in (ladder_st, *legacy.values()):
            feed(st, now, **snap)
        step(ladder_st, now)
        for p, st in legacy.items():
            d = core.throttle_step(st, now, CFG, core.throttle_config(
                {"levels": LEVELS, "profiles": [], "protected_provider": p}))
            assert pstate(ladder_st, p).get("state") == ("ok" if d["level"] == "fast" else "constrained"), (i, p)
            assert pstate(ladder_st, p).get("since") == st["throttle"]["since"], (i, p)
    assert {pstate(ladder_st, p).get("source") for p in legacy} and ladder_st["throttle"]["log"]
