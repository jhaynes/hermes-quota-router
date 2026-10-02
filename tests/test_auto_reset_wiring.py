"""refresh() wiring for auto-redeem: atomic claim under the lock, redeem outside it, settle
afterwards; drain semantics preserved on every path; concurrent refreshes cannot double-redeem.

Follows test_startup_refresh.py's harness: tmp HERMES_HOME, isolated module instances. The
redeem path is monkeypatched (no network); the reviewer's probes cover the real-httpx path.
"""
import importlib.util
import sys
import threading
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
    snap = {
        "fetched_at": 1_800_000_000.0 - 60.0,
        "windows": [
            {"label": "Session", "used": 0.0, "resets_at": 1_800_000_000.0 + 3 * 3600, "scope": None},
            {"label": "Weekly", "used": float(week_used),
             "resets_at": 1_800_000_000.0 + 3 * 24 * 3600, "scope": None},
        ],
        "unavailable": None, "plan": None, "details": list(details),
    }
    with core_mod.locked_state(core_mod.state_path()) as st:
        st.setdefault("providers", {}).setdefault(
            "openai-codex", {"tier": "exhausted", "tier_since": 1_800_000_000.0})["snapshot"] = snap


def _stub_redeem(monkeypatch, calls, status="reset", latency=0.0):
    fake = types.ModuleType("agent.account_usage")

    def _redeem(**kw):
        calls.append(kw)
        if latency:
            import time as _t
            _t.sleep(latency)
        return types.SimpleNamespace(status=status, message="ok")

    fake.redeem_codex_reset_credit = _redeem
    fake.fetch_account_usage = lambda p: None
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.account_usage", fake)


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
    _stub_redeem(monkeypatch, calls)

    plugin_mod.refresh(now=1_800_000_000.0, snapshots={})

    assert len(calls) == 1 and calls[0].get("force") is None  # never force
    st = core_mod.read_state(core_mod.state_path())
    assert st["auto_reset"]["redeems"] == [1_800_000_000.0]  # settled to a success stamp
    assert st["auto_reset"]["last"]["outcome"] == "reset"
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
    _stub_redeem(monkeypatch, calls)

    plugin_mod.refresh(now=1_800_000_000.0, snapshots={})

    assert calls == []
    st = core_mod.read_state(core_mod.state_path())
    assert "auto_reset" not in st


def test_refresh_skips_redeem_when_feature_off(tmp_path, monkeypatch):
    plugin_mod, core_mod = _load_plugin(tmp_path / ".hermes", monkeypatch)
    _seed_codex_snapshot(core_mod, 100.0)
    monkeypatch.setattr(plugin_mod, "_cfg", _enable(core_mod, False))
    calls = []
    _stub_redeem(monkeypatch, calls)

    plugin_mod.refresh(now=1_800_000_000.0, snapshots={})

    assert calls == []
    st = core_mod.read_state(core_mod.state_path())
    assert "auto_reset" not in st


def test_refresh_drain_true_persists_on_every_path(tmp_path, monkeypatch):
    """F1 regression: drain must empty the PERSISTED queue, on the redeem path and off it."""
    plugin_mod, core_mod = _load_plugin(tmp_path / ".hermes", monkeypatch)
    _seed_codex_snapshot(core_mod, 40.0)  # feature on, decision NO -> non-redeem path
    monkeypatch.setattr(plugin_mod, "_cfg", _enable(core_mod, True))
    with core_mod.locked_state(core_mod.state_path()) as st:
        core_mod.queue(st, "quota-router: seeded pending alert")

    first = plugin_mod.refresh(now=1_800_000_000.0, snapshots={}, drain=True)
    assert any("seeded pending alert" in l for l in first)
    st = core_mod.read_state(core_mod.state_path())
    assert st["pending"] == []                      # drained ON DISK
    second = plugin_mod.refresh(now=1_800_000_000.0, snapshots={}, drain=True)
    assert second == []                             # NOT re-delivered

    # redeem path: same guarantee after a successful auto-redeem
    _seed_codex_snapshot(core_mod, 100.0)
    with core_mod.locked_state(core_mod.state_path()) as st:
        core_mod.queue(st, "quota-router: seeded pending alert 2")
    calls = []
    _stub_redeem(monkeypatch, calls)
    drained = plugin_mod.refresh(now=1_800_000_000.0, snapshots={}, drain=True)
    assert any("auto-redeemed" in l for l in drained)
    assert any("seeded pending alert 2" in l for l in drained)
    assert core_mod.read_state(core_mod.state_path())["pending"] == []


def test_concurrent_refreshes_redeem_at_most_once(tmp_path, monkeypatch):
    """F2 regression: overlapping refresh() calls must claim the cap slot atomically."""
    plugin_mod, core_mod = _load_plugin(tmp_path / ".hermes", monkeypatch)
    _seed_codex_snapshot(core_mod, 100.0)
    monkeypatch.setattr(plugin_mod, "_cfg", _enable(core_mod, True))
    calls = []
    _stub_redeem(monkeypatch, calls, latency=0.3)  # widen the in-flight window

    threads = [threading.Thread(target=plugin_mod.refresh,
                                kwargs={"now": 1_800_000_000.0, "snapshots": {}})
               for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)

    assert len(calls) == 1                          # one redeem despite 3 concurrent refreshes
    st = core_mod.read_state(core_mod.state_path())
    assert st["auto_reset"]["redeems"] == [1_800_000_000.0]
