"""Auto-redeem decision policy (pure core.claim_auto_redeem / settle_auto_redeem).

The invariant under test: a banked reset restores the FULL 5h + weekly allowance, so a
dead 5h window alone must NEVER spend a credit — only weekly exhaustion justifies one.
"""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location("qr_core_auto", Path(__file__).resolve().parents[1] / "core.py")
core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(core)

NOW = 1_800_000_000.0
CFG = dict(core.DEFAULTS, auto_reset={"weekly_threshold": 97.0})
FUTURE_RESET = NOW + 3 * 24 * 3600


def snap(week: float, session: float = 0.0, details=("You have 2 resets banked - use /usage reset to activate",),
         week_reset: float = FUTURE_RESET):
    return {
        "fetched_at": NOW - 60.0,
        "windows": [
            {"label": "Session", "used": session, "resets_at": NOW + 3 * 3600, "scope": None},
            {"label": "Weekly", "used": week, "resets_at": week_reset, "scope": None},
        ],
        "unavailable": None, "plan": None, "details": list(details),
    }


def state(s=None):
    return {"providers": {"openai-codex": {"tier": "exhausted", "tier_since": NOW, "snapshot": s}}}


def decide(st=None, now=NOW, cfg=CFG, snapshot=None, claim=False):
    """Run the pure decision; claim=True also reserves the cap slot (like refresh does)."""
    return core.claim_auto_redeem(state(snapshot) if st is None else st, now, cfg)


def test_off_by_default():
    ok, why = core.claim_auto_redeem(state(snap(99.0)), NOW, dict(core.DEFAULTS))
    assert ok is False and why == "auto_reset not configured"


def test_fires_on_weekly_exhaustion_with_banked_credits():
    ok, why = decide(snapshot=snap(100.0))
    assert ok is True and "100%" in why and "2 banked" in why


def test_fires_at_threshold():
    ok, _ = decide(snapshot=snap(97.0))
    assert ok is True


def test_no_redeem_on_dead_5h_window_alone():
    """The core user concern: session at 100%, weekly at 40% — a credit must NOT be spent."""
    ok, why = decide(snapshot=snap(40.0, session=100.0))
    assert ok is False and "40%" in why


def test_no_redeem_without_banked_credits():
    ok, why = decide(snapshot=snap(100.0, details=["You have 0 resets banked - use /usage reset to activate"]))
    assert ok is False and why == "no banked credits"


def test_no_redeem_without_banked_details_line():
    ok, why = decide(snapshot=snap(100.0, details=[]))
    assert ok is False and why == "no banked credits"


def test_no_redeem_below_threshold():
    ok, why = decide(snapshot=snap(90.0, session=90.0))
    assert ok is False and "90%" in why


def test_no_redeem_on_stale_snapshot():
    st = state(snap(100.0))
    ok, why = core.claim_auto_redeem(st, NOW + core.DEFAULTS["stale_after_s"] + 60.0, CFG)
    assert ok is False and why == "snapshot stale"


def test_no_redeem_on_unavailable_snapshot():
    st = state(snap(100.0))
    st["providers"]["openai-codex"]["snapshot"]["unavailable"] = "no snapshot"
    ok, why = decide(st)
    assert ok is False and why == "no fresh snapshot"


def test_no_redeem_when_no_snapshot():
    ok, why = decide(state(None))
    assert ok is False and why == "no fresh snapshot"


def test_no_redeem_without_weekly_window():
    st = state(snap(100.0))
    st["providers"]["openai-codex"]["snapshot"]["windows"] = [
        {"label": "Session", "used": 100.0, "resets_at": NOW + 3 * 3600, "scope": None}]
    ok, why = decide(st)
    assert ok is False and why == "no Weekly window in snapshot"


def test_scoped_window_labelled_weekly_never_counts():
    """A model-scoped window LABELLED 'Weekly' (scope set) must not satisfy the gate.

    Decisive shape: unscoped Weekly at 40% (below threshold), scoped 'Weekly' at 100%.
    A mutant that drops the scope filter would flip this to redeem=True.
    """
    st = state(snap(40.0))
    st["providers"]["openai-codex"]["snapshot"]["windows"].append(
        {"label": "Weekly", "used": 100.0, "resets_at": FUTURE_RESET, "scope": "fable"})
    ok, why = decide(st)
    assert ok is False and "40%" in why


def test_no_redeem_when_weekly_already_reset():
    """Weekly 100% but its reset passed: the exhaustion is history (freshly refilled week)."""
    ok, why = decide(snapshot=snap(100.0, week_reset=NOW - 60.0))
    assert ok is False and why == "Weekly window already reset (stale exhaustion)"


def test_invalid_threshold_config_never_raises():
    ok, why = core.claim_auto_redeem(
        state(snap(100.0)), NOW, dict(core.DEFAULTS, auto_reset={"weekly_threshold": None}))
    assert ok is False and why == "invalid auto_reset.weekly_threshold config"
    ok, why = core.claim_auto_redeem(
        state(snap(100.0)), NOW, dict(core.DEFAULTS, auto_reset={"weekly_threshold": "abc"}))
    assert ok is False and why == "invalid auto_reset.weekly_threshold config"


def test_old_redeems_do_not_count_toward_cap():
    st = state(snap(100.0))
    st["auto_reset"] = {"redeems": [NOW - 8 * 24 * 3600.0]}
    ok, _ = core.claim_auto_redeem(st, NOW, CFG)
    assert ok is True


def test_claim_reserves_cap_slot():
    st = state(snap(100.0))
    ok, _ = core.claim_auto_redeem(st, NOW, CFG)
    assert ok is True
    assert len(st["auto_reset"]["redeems"]) == 1
    assert isinstance(st["auto_reset"]["redeems"][0], dict) and "claim" in st["auto_reset"]["redeems"][0]


def test_second_claim_blocked_by_in_flight_reservation():
    st = state(snap(100.0))
    ok, _ = core.claim_auto_redeem(st, NOW, CFG)
    assert ok is True
    ok2, why2 = core.claim_auto_redeem(st, NOW + 60.0, CFG)
    assert ok2 is False and "cap reached" in why2


def test_settle_success_keeps_reservation_as_timestamp():
    st = state(snap(100.0))
    ok, reason = core.claim_auto_redeem(st, NOW, CFG)
    assert ok is True
    core.settle_auto_redeem(st, NOW + 5.0, reason, "reset")
    assert st["auto_reset"]["redeems"] == [NOW + 5.0]
    assert st["auto_reset"]["last"]["outcome"] == "reset"
    assert any("auto-redeemed" in a for a in st["pending"])


def test_settle_refusal_rolls_back_reservation_so_cap_not_burned():
    """F3: a not_exhausted refusal at 97-99.9% must not block the real redeem at 100%."""
    st = state(snap(98.0))
    ok, reason = core.claim_auto_redeem(st, NOW, CFG)
    assert ok is True
    core.settle_auto_redeem(st, NOW + 5.0, reason, "not_exhausted")
    assert st["auto_reset"]["redeems"] == []           # slot rolled back
    assert st["auto_reset"]["last"]["outcome"] == "not_exhausted"
    assert any("not done" in a for a in st["pending"])
    # one hour later, weekly truly exhausted with a FRESH snapshot: the claim must now fire
    st2 = state(snap(100.0))
    st2["providers"]["openai-codex"]["snapshot"]["fetched_at"] = NOW + 3600.0 - 60.0
    st2["auto_reset"] = st["auto_reset"]
    ok2, _ = core.claim_auto_redeem(st2, NOW + 3600.0, CFG)
    assert ok2 is True


def test_settle_failure_rolls_back_reservation():
    st = state(snap(100.0))
    ok, reason = core.claim_auto_redeem(st, NOW, CFG)
    core.settle_auto_redeem(st, NOW + 5.0, reason, "error")
    assert st["auto_reset"]["redeems"] == []


def test_settle_keeps_other_in_flight_claims():
    cfg2 = dict(CFG, auto_reset_max_redeems_per_week=2)
    st2 = state(snap(100.0))
    ok1, reason1 = core.claim_auto_redeem(st2, NOW, cfg2)
    ok2, reason2 = core.claim_auto_redeem(st2, NOW + 1.0, cfg2)
    assert ok1 and ok2
    # settling claim1 as a REFUSAL rolls back only claim1's slot; claim2 stays reserved
    core.settle_auto_redeem(st2, NOW + 5.0, reason1, "not_exhausted")
    redeems = st2["auto_reset"]["redeems"]
    assert len(redeems) == 1 and isinstance(redeems[0], dict) and redeems[0]["claim"] == reason2
