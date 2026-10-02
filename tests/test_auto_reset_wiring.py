"""refresh() wiring for auto-redeem: decision runs after the state lock, redeem is attempted
only when the decision fires, the attempt is recorded, and drain semantics are preserved.

Follows test_startup_refresh.py's harness: tmp HERMES_HOME, isolated module instances, and an
autouse tripwire failing on any real outbound TCP connect (the redeem path is monkeypatched).
"""
import importlib.util
import sys
import types
from pathlib import Path

import pytest

_root = Path(__file__).resolve().parents[1]


def _load_plugin(tmp_home, monkeypatch):
    core_spec = importlib.util.spec_from_file_location("qr_core_auto_wiring", _root / "core.py")
    core_mod = importlib.util.module_from_spec(core_spec)
    monkeypatch.setitem(sys.modules, "qr_core_auto_wiring", core_mod)
    core_spec.loader.exec_module(core_mod)

    plugin_spec = importlib.util.spec_from_file_location("qr_plugin_auto_wiring", _root / "__init__.py")
    plugin_mod = importlib.util.module_from_spec(plugin_spec)
    monkeypatch.setitem(sys.modules, "qr_plugin_auto_wiring", plugin_mod)
    plugin_spec.loader.exec_module(plugin_mod)

    monkeypatch.setitem(sys.modules, "quota_router_core", core_mod)
    core_mod.state_path = lambda hermes_home=None: tmp_home / "cache" / "quota-router" / "state.json"
    core_mod.state_db_path = lambda hermes_home=None: tmp_home / "state.db"
    return plugin_mod, core_mod


def _seed_codex_snapshot(core_mod, week_used, details=("You have 2 resets banked - use /usage reset to activate",)):
    cfg = dict(core_mod.DEFAULTS, auto_reset={"weekly_threshold": 97.0})
    snap = {
        "fetched_at": 1_800_000_000.0 - 60.0,
        "windows": [
            {"label": "Session", "used": 0.0, "resets_at": 1_800_000_000.0 + 3 * 3600, "scope": None},
            {"label": "Weekly", "used": float(week_used), "resets_at": 1_800_000_000.0 + 3 * 24 * 3600, "scope": None},
        ],
        "unavailable": None, "plan": None, "details": list(details),
    }
    with core_mod.locked_state(core_mod.state_path()) as st:
        st.setdefault("providers", {}).setdefault(
            "openai-codex", {"tier": "exhausted", "tier_since": 1_800_000_000.0})["snapshot"] = snap
    return cfg


class _Result:
    def __init__(self, status="reset", message="ok"):
        self.status, self.message = status, message


def _enable(core_mod, enabled=True):
    """Build a plugin._cfg replacement: feature on (weekly 97) or off."""
    def _cfg():
        cfg = dict(core_mod.DEFAULTS)
        if enabled:
            cfg["auto_reset"] = {"weekly_threshold": 97.0}
        return cfg
    return _cfg


def test_refresh_attempts_redeem_when_weekly_exhausted(tmp_path, monkeypatch):
    plugin_mod, core_mod = _load_plugin(tmp_path / ".hermes", monkeypatch)
    _seed_codex_snapshot(core_mod, 100.0)
    monkeypatch.setattr(plugin_mod, "_cfg", _enable(core_mod, True))
    calls = []
    fake = types.ModuleType("agent.account_usage")
    fake.redeem_codex_reset_credit = lambda **kw: calls.append(kw) or _Result()
    fake.fetch_account_usage = lambda p: None
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.account_usage", fake)

    plugin_mod.refresh(now=1_800_000_000.0, snapshots={})

    assert len(calls) == 1 and calls[0].get("force") is None  # never force
    st = plugin_mod._core().read_state(plugin_mod._core().state_path())
    assert st["auto_reset"]["redeems"] and st["auto_reset"]["last"]["outcome"] == "reset"
    assert any("auto-redeemed" in a for a in st["pending"])


def test_refresh_skips_redeem_on_dead_5h_window_alone(tmp_path, monkeypatch):
    plugin_mod, core_mod = _load_plugin(tmp_path / ".hermes", monkeypatch)
    _seed_codex_snapshot(core_mod, 40.0,
                         details=["You have 2 resets banked - use /usage reset to activate"])
    # a dead session window at 100% alongside a 40% week must still NOT redeem
    with core_mod.locked_state(core_mod.state_path()) as st:
        st["providers"]["openai-codex"]["snapshot"]["windows"][0]["used"] = 100.0
    monkeypatch.setattr(plugin_mod, "_cfg", _enable(core_mod, True))
    calls = []
    fake = types.ModuleType("agent.account_usage")
    fake.redeem_codex_reset_credit = lambda **kw: calls.append(kw) or _Result()
    fake.fetch_account_usage = lambda p: None
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.account_usage", fake)

    plugin_mod.refresh(now=1_800_000_000.0, snapshots={})

    assert calls == []
    st = plugin_mod._core().read_state(plugin_mod._core().state_path())
    assert "auto_reset" not in st


def test_refresh_skips_redeem_when_feature_off(tmp_path, monkeypatch):
    plugin_mod, core_mod = _load_plugin(tmp_path / ".hermes", monkeypatch)
    _seed_codex_snapshot(core_mod, 100.0)
    # wipe the auto_reset config: feature off by default
    monkeypatch.setattr(plugin_mod, "_cfg", _enable(core_mod, False))
    calls = []
    fake = types.ModuleType("agent.account_usage")
    fake.redeem_codex_reset_credit = lambda **kw: calls.append(kw) or _Result()
    fake.fetch_account_usage = lambda p: None
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.account_usage", fake)

    plugin_mod.refresh(now=1_800_000_000.0, snapshots={})

    assert calls == []
    st = core_mod.read_state(core_mod.state_path())
    assert "auto_reset" not in st
