"""Auto-redeem decision policy (pure core.auto_reset_decision / record_auto_redeem).

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


def snap(week: float, session: float = 0.0, details=("You have 2 resets banked - use /usage reset to activate",)):
    return {
        "fetched_at": NOW - 60.0,
        "windows": [
            {"label": "Session", "used": session, "resets_at": NOW + 3 * 3600, "scope": None},
            {"label": "Weekly", "used": week, "resets_at": NOW + 3 * 24 * 3600, "scope": None},
        ],
        "unavailable": None, "plan": None, "details": list(details),
    }


def state(s=None):
    return {"providers": {"openai-codex": {"tier": "exhausted", "tier_since": NOW, "snapshot": s}}}


def decide(st=None, now=NOW, cfg=CFG, snapshot=None):
    return core.auto_reset_decision(state(snapshot) if st is None else st, now, cfg)


def test_off_by_default():
    ok, why = core.auto_reset_decision(state(snap(99.0)), NOW, dict(core.DEFAULTS))
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
    ok, why = decide(st, now=NOW + core.DEFAULTS["stale_after_s"] + 60.0)
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


def test_scoped_windows_never_count():
    """Model-scoped windows (scope set) are display-only; the Weekly check must ignore them."""
    st = state(snap(40.0))
    st["providers"]["openai-codex"]["snapshot"]["windows"].append(
        {"label": "Fable week (model)", "used": 100.0, "resets_at": NOW + 3 * 24 * 3600, "scope": "fable"})
    ok, why = decide(st)
    assert ok is False and "40%" in why


def test_weekly_cap_limits_repeated_redeems():
    st = state(snap(100.0))
    st["auto_reset"] = {"redeems": [NOW - 3600.0]}
    ok, why = decide(st)
    assert ok is False and "cap reached" in why


def test_old_redeems_do_not_count_toward_cap():
    st = state(snap(100.0))
    st["auto_reset"] = {"redeems": [NOW - 8 * 24 * 3600.0]}
    ok, _ = decide(st)
    assert ok is True


def test_record_appends_history_and_queues_one_alert():
    st = {"providers": {}, "pending": []}
    core.record_auto_redeem(st, NOW, "Weekly at 100%", "reset")
    assert st["auto_reset"]["redeems"] == [NOW]
    assert st["auto_reset"]["last"]["outcome"] == "reset"
    assert len(st["pending"]) == 1 and "auto-redeemed" in st["pending"][0]


def test_record_prunes_old_history():
    st = {"auto_reset": {"redeems": [NOW - 8 * 24 * 3600.0]}, "pending": []}
    core.record_auto_redeem(st, NOW, "Weekly at 100%", "reset")
    assert st["auto_reset"]["redeems"] == [NOW]
