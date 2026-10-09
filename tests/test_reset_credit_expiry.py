"""Banked Codex reset credits carry an expiry; gather it and spend a credit before it lapses.

``GET .../wham/rate-limit-reset-credits`` lists each available "Full reset" credit with
``expires_at`` (30 days after ``granted_at`` on the live account). The usage endpoint only gives
a count, so before this the plugin could not tell that a credit was about to lapse: weekly
exhaustion (the only redeem trigger, capped at one per week) may not recur before expiry.
"""
import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("qr_core_credit_expiry", Path(__file__).resolve().parents[1] / "core.py")
core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(core)

NOW = 1_800_000_000.0
H = 3600.0
D = 24 * H
CFG = dict(core.DEFAULTS, auto_reset={"weekly_threshold": 97.0})


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def payload(*credits, available_count=None):
    return {"credits": list(credits),
            "available_count": len(credits) if available_count is None else available_count,
            "total_earned_count": 0, "immediate_reset_purchase_eligible": False, "history_enabled": True}


def credit(cid, expires_at, granted_at=None, status="available", supported=True):
    return {"id": cid, "reset_type": "codex_rate_limits", "is_supported_by_plan": supported,
            "status": status, "granted_at": iso(granted_at if granted_at is not None
                                                 else (expires_at or NOW) - 30 * D),
            "expires_at": None if expires_at is None else iso(expires_at),
            "redeem_started_at": None, "redeemed_at": None, "title": "Full reset"}


def snap(week, week_reset=NOW + 3 * D, fetched_at=NOW - 60):
    return {"fetched_at": fetched_at, "unavailable": None, "plan": None,
            "details": ["You have 2 resets banked - use /usage reset to activate"],
            "windows": [{"label": "Session", "used": 0.0, "resets_at": NOW + 3 * H, "scope": None},
                        {"label": "Weekly", "used": float(week), "resets_at": week_reset, "scope": None}]}


def state(week=40.0, week_reset=NOW + 3 * D, credits=None, credits_at=NOW - 60):
    st = {"providers": {"openai-codex": {"tier": "ok", "tier_since": NOW, "snapshot": snap(week, week_reset)}}}
    if credits is not None:
        core.apply_reset_credits(st, core.parse_reset_credits(payload(*credits)), credits_at)
    return st


# ── gather ──────────────────────────────────────────────────────────────────────────────────

def test_parse_keeps_spendable_credits_soonest_expiring_first():
    got = core.parse_reset_credits(payload(
        credit("late", NOW + 20 * D), credit("none", None), credit("soon", NOW + 2 * D),
        credit("used", NOW + 1 * D, status="redeemed"), credit("plan", NOW + 1 * D, supported=False),
        {"status": "available", "expires_at": iso(NOW + D)}, "junk", available_count=3))
    assert [c["id"] for c in got["credits"]] == ["soon", "late", "none"]
    assert got["credits"][0]["expires_at"] == pytest.approx(NOW + 2 * D, abs=1e-3)
    assert got["credits"][0]["granted_at"] == pytest.approx(NOW + 2 * D - 30 * D, abs=1e-3)
    assert got["credits"][2]["expires_at"] is None
    assert got["available_count"] == 3


@pytest.mark.parametrize("bad", [None, [], {}, {"credits": "x"}])
def test_parse_rejects_a_payload_without_a_credit_list(bad):
    with pytest.raises(ValueError):
        core.parse_reset_credits(bad)


def test_credits_are_due_when_absent_old_or_marked_after_a_redeem():
    st = state()
    assert core.reset_credits_due(st, NOW, CFG)
    st = state(credits=[credit("a", NOW + 5 * D)], credits_at=NOW - 60)
    assert not core.reset_credits_due(st, NOW, CFG)
    assert core.reset_credits_due(st, NOW - 60 + CFG["reset_credits_refresh_s"], CFG)
    core.settle_auto_redeem(st, NOW, "x", "reset")
    assert core.reset_credits_due(st, NOW, CFG)


def test_list_is_unusable_for_decisions_once_stale_or_older_than_a_redemption():
    st = state(credits=[credit("a", NOW + 5 * D)], credits_at=NOW - 60)
    assert core.usable_reset_credits(st, NOW, CFG) is not None
    assert core.usable_reset_credits(st, NOW - 60 + 2 * CFG["reset_credits_refresh_s"] + 1, CFG) is None
    st["auto_reset"] = {"redeems": [NOW - 30]}   # a spend after the list was fetched
    assert core.usable_reset_credits(st, NOW, CFG) is None
    st["auto_reset"] = {"redeems": [{"at": NOW - 30, "claim": "in flight"}]}
    assert core.usable_reset_credits(st, NOW, CFG) is None


def test_redeem_target_is_the_soonest_unexpired_credit():
    st = state(credits=[credit("gone", NOW - 1), credit("b", NOW + 9 * D), credit("a", NOW + 2 * D)])
    assert core.redeem_target(st, NOW, CFG) == "a"
    assert core.redeem_target(state(), NOW, CFG) is None


# ── expiry-driven redemption ────────────────────────────────────────────────────────────────

def claim(st, now=NOW, cfg=CFG, supported=True):
    return core.claim_expiry_redeem(st, now, cfg, supports_credit_id=supported)


def test_redeems_a_credit_expiring_within_the_lead_before_the_weekly_reset():
    st = state(week=34.0, credits=[credit("later", NOW + 9 * D), credit("soon", NOW + 10 * H)])
    ok, why, cid = claim(st)
    assert ok and cid == "soon"
    assert "34%" in why and "expir" in why
    assert st["auto_reset"]["expiry"]["inflight"]["credit_id"] == "soon"


def test_waits_while_the_credit_is_outside_the_lead():
    ok, why, cid = claim(state(week=90.0, credits=[credit("a", NOW + 25 * H)]))
    assert not ok and cid is None and "lead" in why


def test_waits_when_the_weekly_window_resets_before_the_credit_expires():
    """The credit is still spendable in the next window, where exhaustion can use all of it."""
    ok, why, _ = claim(state(week=90.0, week_reset=NOW + 5 * H, credits=[credit("a", NOW + 10 * H)]))
    assert not ok and "Weekly resets" in why


def test_skips_when_weekly_usage_is_too_low_to_be_worth_restoring():
    ok, why, _ = claim(state(week=9.0, credits=[credit("a", NOW + 10 * H)]))
    assert not ok and "9%" in why


def test_ignores_expired_credits():
    ok, why, _ = claim(state(week=50.0, credits=[credit("a", NOW - 1)]))
    assert not ok and "no expiring" in why


def test_never_acts_on_a_list_that_predates_the_last_redemption():
    st = state(week=50.0, credits=[credit("a", NOW + 10 * H)], credits_at=NOW - 120)
    st["auto_reset"] = {"redeems": [NOW - 60]}
    ok, why, _ = claim(st)
    assert not ok and "list" in why


def test_a_second_concurrent_claim_is_refused():
    st = state(week=50.0, credits=[credit("a", NOW + 10 * H), credit("b", NOW + 11 * H)])
    assert claim(st)[0]
    ok, why, _ = claim(st)
    assert not ok


def test_failed_attempt_backs_off_then_retries():
    st = state(week=50.0, credits=[credit("a", NOW + 10 * H)])
    ok, why, cid = claim(st)
    core.settle_expiry_redeem(st, NOW, why, "unavailable", cid)
    assert st["auto_reset"]["expiry"]["inflight"] is None
    assert not claim(st, now=NOW + 60)[0]            # list marked for re-fetch
    fresh = core.parse_reset_credits(payload(credit("a", NOW + 10 * H)))
    core.apply_reset_credits(st, fresh, NOW + 60)
    ok, why, _ = claim(st, now=NOW + 120)
    assert not ok and "retrying" in why
    core.apply_reset_credits(st, fresh, NOW + core.EXPIRY_RETRY_S)
    st["providers"]["openai-codex"]["snapshot"] = snap(50.0, fetched_at=NOW + core.EXPIRY_RETRY_S)
    assert claim(st, now=NOW + core.EXPIRY_RETRY_S + 1)[0]


def test_success_is_recorded_and_forces_a_list_refresh():
    st = state(week=50.0, credits=[credit("a", NOW + 10 * H)])
    ok, why, cid = claim(st)
    core.settle_expiry_redeem(st, NOW + 5, why, "reset", cid)
    ex = st["auto_reset"]["expiry"]
    assert ex["inflight"] is None and ex["redeemed"] == [{"at": NOW + 5, "credit_id": "a"}]
    assert st["auto_reset"]["last"]["kind"] == "expiry"
    assert any("expiring" in p and "auto-redeemed" in p for p in st["pending"])
    assert core.reset_credits_due(st, NOW + 6, CFG)
    assert core.usable_reset_credits(st, NOW + 6, CFG) is None


def test_expiry_redemptions_do_not_use_the_weekly_exhaustion_cap():
    st = state(week=50.0, credits=[credit("a", NOW + 10 * H), credit("b", NOW + 20 * D)])
    ok, why, cid = claim(st)
    core.settle_expiry_redeem(st, NOW, why, "reset", cid)
    st["providers"]["openai-codex"]["snapshot"] = snap(100.0, fetched_at=NOW + 2 * D - 60)
    assert core.claim_auto_redeem(st, NOW + 2 * D, CFG)[0]


def test_without_credit_id_support_it_does_not_redeem_and_alerts_once():
    """Untargeted, the backend may spend a later credit and the expiring one still lapses."""
    st = state(week=50.0, credits=[credit("a", NOW + 10 * H)])
    ok, why, _ = claim(st, supported=False)
    assert not ok and "credit_id" in why
    assert "inflight" not in (st.get("auto_reset") or {}).get("expiry", {}) or \
        st["auto_reset"]["expiry"]["inflight"] is None
    alerts = [p for p in st.get("pending", []) if "lapse" in p]
    assert len(alerts) == 1
    claim(st, now=NOW + 600, supported=False)
    assert len([p for p in st["pending"] if "lapse" in p]) == 1


@pytest.mark.parametrize("acfg,needle", [
    (None, "not configured"),
    ({"weekly_threshold": 97.0, "expiry_lead_s": 0}, "disabled"),
    ({"weekly_threshold": 97.0, "expiry_lead_s": "x"}, "invalid"),
    ({"weekly_threshold": 97.0, "expiry_min_used": float("nan")}, "invalid"),
])
def test_config(acfg, needle):
    cfg = dict(core.DEFAULTS, auto_reset=acfg)
    ok, why, _ = claim(state(week=50.0, credits=[credit("a", NOW + 10 * H)]), cfg=cfg)
    assert not ok and needle in why


def test_stale_or_missing_weekly_snapshot_blocks():
    st = state(week=50.0, credits=[credit("a", NOW + 10 * H)])
    st["providers"]["openai-codex"]["snapshot"]["fetched_at"] = NOW - CFG["stale_after_s"] - 1
    assert not claim(st)[0]
    st["providers"]["openai-codex"]["snapshot"] = snap(float("nan"))
    assert not claim(st)[0]


# ── projection allowance (planning) ─────────────────────────────────────────────────────────

def test_banked_allowance_needs_a_credit_that_survives_to_the_weekly_reset():
    """Err toward constraining: a credit that lapses mid-window is not a full future refill."""
    reset = NOW + 3 * D
    lapsing = state(week=60.0, week_reset=reset, credits=[credit("a", reset - H)])
    surviving = state(week=60.0, week_reset=reset, credits=[credit("a", reset - H), credit("b", reset + H)])
    no_list = state(week=60.0, week_reset=reset)
    assert core._auto_redeem(lapsing, NOW, CFG, planning=True) == \
        (False, "banked credits all expire before the Weekly reset")
    assert core._auto_redeem(surviving, NOW, CFG, planning=True)[0]
    assert core._auto_redeem(no_list, NOW, CFG, planning=True)[0]   # unchanged without the list


# ── status ──────────────────────────────────────────────────────────────────────────────────

def test_status_shows_count_and_next_expiry():
    st = core.empty_state()
    st["providers"] = state(credits=[credit("b", NOW + 9 * D), credit("a", NOW + 10 * H)])["providers"]
    text = core.render_status(st, NOW, CFG)
    line = next(l for l in text.splitlines() if "reset credits" in l)
    assert "2 available" in line and "next expires" in line and "10h" in line
    payload_out = core.status_payload(st, NOW, CFG, "host")["providers"]["openai-codex"]["reset_credits"]
    assert [c["expires_at"] for c in payload_out["credits"]] == [pytest.approx(NOW + 10 * H, abs=1e-3),
                                                                pytest.approx(NOW + 9 * D, abs=1e-3)]
    assert "id" not in payload_out["credits"][0]
