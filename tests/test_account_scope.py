"""Compatibility with Hermes's explicit account/model quota scopes.

These fixtures model the real account_usage wire shape, not provider responses.
They use only in-memory state and never touch credentials or live quotas.
"""
import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("qr_account_scope", Path(__file__).resolve().parents[1] / "core.py")
assert _spec is not None and _spec.loader is not None
core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(core)
NOW = 1_800_000_000.0
RESET = NOW + 3 * 86400
CFG = dict(core.DEFAULTS)


def window(used=100.0, scope="account", label="Weekly", reset=RESET):
    return {"label": label, "used": used, "resets_at": reset, "scope": scope}


def cached_state(*windows):
    state = core.empty_state()
    state["providers"]["openai-codex"] = {
        "tier": "ok", "tier_since": NOW - 86400,
        "snapshot": {"fetched_at": NOW, "windows": list(windows), "details": []},
    }
    return state


@pytest.mark.parametrize("scope", ["account", None, ""])
def test_cached_account_window_drives_tier(scope):
    """Explicit account scopes were dropped; old persisted unscoped data must still work."""
    state = cached_state(window(scope=scope), window(scope="model", label="Model limit"))
    core.recompute(state, NOW, CFG)
    assert state["providers"]["openai-codex"]["tier"] == "exhausted"


@pytest.mark.parametrize("scope", ["model", "Fable", "Opus", "unknown"])
def test_model_or_unknown_scope_never_drives_account_tier(scope):
    """Do not solve disappearing account windows by admitting model caps to routing."""
    state = cached_state(window(used=20.0), window(scope=scope, label="Model limit"))
    core.recompute(state, NOW, CFG)
    assert state["providers"]["openai-codex"]["tier"] == "ok"


def test_explicit_account_block_expiry_uses_account_reset():
    """Hard blocks must expire at the account reset, not the fallback TTL or a model reset."""
    windows = [window(used=80.0), window(scope="model", reset=NOW + 3600)]
    assert core.block_until(windows, NOW, CFG) == RESET


def test_history_and_forecast_follow_explicit_account_windows():
    """The discarded account scope prevented new history points and every dependent forecast."""
    state = cached_state()
    for i in range(5):
        t = NOW - (4 - i) * 1200
        snap = {"fetched_at": t, "windows": [window(used=20.0 + i), window(scope="model", label="Model cap")]}
        core.append_history(state, "openai-codex", snap, NOW)
    assert len(state["history"]["openai-codex"].get("Weekly", [])) == 5
    assert "Model cap" not in state["history"]["openai-codex"]
    assert core.forecast_state(state, NOW, CFG)["openai-codex"]["Weekly"]["status"] == "ok"


def test_json_status_classifies_account_windows_for_desktop():
    """Desktop uses affects_tier/forecast, so fixing the TUI alone would leave its chip broken."""
    state = cached_state(window(), window(scope="model", label="Model cap"))
    state["history"] = {"openai-codex": {"Weekly": [[NOW, 100.0, RESET]]}}
    windows = core.status_payload(state, NOW, CFG, "fixture")["providers"]["openai-codex"]["windows"]
    assert windows[0]["scope"] == "account"  # Preserve the host's wire identity.
    assert windows[0]["affects_tier"] is True
    assert windows[0]["forecast"] is not None
    assert windows[1]["affects_tier"] is False and windows[1]["forecast"] is None


def test_text_status_keeps_account_windows_out_of_model_limits():
    """Explicit account windows were incorrectly rendered under the model-limits heading."""
    state = cached_state(window(used=27.0))
    rendered = core.render_status(state, NOW, CFG)
    assert "Weekly" in rendered and "27.0%" in rendered and "resets" in rendered
    assert "model limits:" not in rendered


def test_auto_redeem_claim_accepts_account_weekly_with_existing_guards():
    """Restore only scope admission; an eligible claim remains capped and model caps cannot redeem."""
    state = cached_state(window())
    state["providers"]["openai-codex"]["snapshot"]["details"] = ["You have 2 resets banked - use /usage reset to activate"]
    cfg = dict(CFG, auto_reset={"weekly_threshold": 97.0})
    claimed, reason = core.claim_auto_redeem(state, NOW, cfg)
    assert claimed is True and "2 banked" in reason
    assert core.claim_auto_redeem(state, NOW, cfg)[0] is False
    state = cached_state(window(scope="model"))
    state["providers"]["openai-codex"]["snapshot"]["details"] = ["You have 2 resets banked - use /usage reset to activate"]
    assert core.claim_auto_redeem(state, NOW, cfg)[0] is False


def test_explicit_account_usage_drives_throttle_ladder():
    """The configured ladder must leave Codex at actual account exhaustion without needing forecasts."""
    state = cached_state(window())
    t = core.throttle_config({"ladder": [
        {"name": "codex", "gate": ["openai-codex"], "provider": "openai-codex", "model": "fixture"},
        {"name": "internal", "provider": "ciq-internal", "model": "fixture"},
    ]})
    decision = core.throttle_step(state, NOW, CFG, t)
    assert decision["level"] == "internal"
    state = cached_state(window(used=20.0), window(scope="model"))
    assert core.throttle_step(state, NOW, CFG, t)["level"] == "codex"


def test_account_scope_preserves_near_reset_and_staleness_guards():
    """Scope compatibility must not relax the existing fail-open and imminent-reset rules."""
    state = cached_state(window(reset=NOW + 60))
    core.recompute(state, NOW, CFG)
    assert state["providers"]["openai-codex"]["tier"] == "ok"
    state = cached_state(window())
    core.recompute(state, NOW + CFG["stale_after_s"] + 1, CFG)
    assert state["providers"]["openai-codex"]["tier"] == "ok"
