"""Thin tests for __init__.refresh()'s drain semantics against a tmp HERMES_HOME.

Never touches the real ~/.hermes/cache/quota-router/state.json.
"""
import importlib.util
import sys
import time
from pathlib import Path

_root = Path(__file__).resolve().parents[1]


def _load_plugin(tmp_home, monkeypatch):
    """Load a fresh copy of __init__.py + core.py, isolated from any cached module and
    pointed at a tmp HERMES_HOME so state never touches the real file."""
    core_spec = importlib.util.spec_from_file_location("qr_core_initcase", _root / "core.py")
    core_mod = importlib.util.module_from_spec(core_spec)
    monkeypatch.setitem(sys.modules, "qr_core_initcase", core_mod)
    core_spec.loader.exec_module(core_mod)

    plugin_spec = importlib.util.spec_from_file_location("qr_plugin_initcase", _root / "__init__.py")
    plugin_mod = importlib.util.module_from_spec(plugin_spec)
    monkeypatch.setitem(sys.modules, "qr_plugin_initcase", plugin_mod)
    plugin_spec.loader.exec_module(plugin_mod)

    # Force the plugin's lazily-imported core module to our tmp-bound instance, and pin
    # state_path()/root_home() to the tmp home regardless of the real environment.
    monkeypatch.setitem(sys.modules, "quota_router_core", core_mod)
    core_mod.state_path = lambda hermes_home=None: tmp_home / "cache" / "quota-router" / "state.json"
    return plugin_mod, core_mod


def test_refresh_drain_true_returns_alert_and_drain_false_leaves_queue(tmp_path, monkeypatch):
    tmp_home = tmp_path / ".hermes"
    plugin_mod, core_mod = _load_plugin(tmp_home, monkeypatch)
    now = 1_800_000_000.0
    snap = {"fetched_at": now, "windows": [
        {"label": "Current week", "used_percent": 80.0, "resets_at": now + 3 * 24 * 3600}]}

    drained = plugin_mod.refresh(now=now, snapshots={"anthropic": snap}, drain=True)
    assert any("≥75%" in l for l in drained)

    # A real state file was written under tmp_home, never under the real HERMES_HOME.
    state_file = tmp_home / "cache" / "quota-router" / "state.json"
    assert state_file.exists()

    # Status-style refresh (drain=False) must not remove anything further queued and must not
    # re-return already-drained lines.
    before = core_mod.read_state(state_file)["pending"]
    empty_drain = plugin_mod.refresh(now=now + 1, snapshots={"anthropic": snap}, drain=False)
    assert empty_drain == []
    after = core_mod.read_state(state_file)["pending"]
    # No new ≥75% alert re-fires in the same reset cycle, and nothing already queued is lost.
    assert after == before


def test_refresh_records_injected_failing_outcome_and_later_ok_clears_it(tmp_path, monkeypatch):
    tmp_home = tmp_path / ".hermes"
    plugin_mod, core_mod = _load_plugin(tmp_home, monkeypatch)
    now = 1_800_000_000.0

    plugin_mod.refresh(now=now, snapshots={}, outcomes={
        "anthropic": (False, "fetch_error", "boom")}, drain=False)
    state_file = tmp_home / "cache" / "quota-router" / "state.json"
    st = core_mod.read_state(state_file)
    assert st["providers"]["anthropic"]["fetch"]["core"]["error"]["kind"] == "fetch_error"

    plugin_mod.refresh(now=now + 60, snapshots={}, outcomes={"anthropic": (True, None, "")}, drain=False)
    st = core_mod.read_state(state_file)
    assert st["providers"]["anthropic"]["fetch"]["core"]["error"] is None
    assert st["providers"]["anthropic"]["fetch"]["core"]["last_ok_at"] == now + 60


def test_live_fetch_path_classifies_none_unavailable_and_exception(tmp_path, monkeypatch):
    """Real refresh() fetch branch (snapshots=None) with a stubbed core fetcher: None and
    unavailable snapshots are failures, a raising fetcher is fetch_error, a snapshot is ok."""
    import types
    tmp_home = tmp_path / ".hermes"
    plugin_mod, core_mod = _load_plugin(tmp_home, monkeypatch)
    now = time.time()
    good = types.SimpleNamespace(fetched_at=now, unavailable_reason=None, plan=None, details=[],
                                 windows=[types.SimpleNamespace(label="Current week", used_percent=10.0, resets_at=None)])
    results = {"anthropic": None, "openai-codex": good}
    def fake_fetch(p):
        r = results[p]
        if isinstance(r, Exception):
            raise r
        return r
    fake = types.ModuleType("agent.account_usage"); fake.fetch_account_usage = fake_fetch
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.account_usage", fake)
    state_file = tmp_home / "cache" / "quota-router" / "state.json"
    fetch = lambda p: core_mod.read_state(state_file)["providers"][p]["fetch"]["core"]

    plugin_mod.refresh()
    assert fetch("anthropic")["error"]["kind"] == "no_data"
    assert fetch("openai-codex")["error"] is None and fetch("openai-codex")["last_ok_at"]

    results["anthropic"] = types.SimpleNamespace(unavailable_reason="not oauth", windows=[])
    results["openai-codex"] = RuntimeError("boom")
    plugin_mod.refresh()
    assert fetch("anthropic")["error"]["kind"] == "unavailable"
    assert fetch("openai-codex")["error"]["kind"] == "fetch_error"
    assert "boom" in fetch("openai-codex")["error"]["message"]
