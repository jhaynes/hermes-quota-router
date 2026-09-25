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


def test_anthropic_headers_match_core_literal():
    import types
    tmp_home_unused = None
    import importlib.util
    spec = importlib.util.spec_from_file_location("qr_plugin_headers", _root / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["qr_plugin_headers"] = mod
    spec.loader.exec_module(mod)
    headers = mod._anthropic_headers("tok123")
    assert headers == {
        "Authorization": "Bearer tok123",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "claude-code/2.1.0",
    }


def test_fetch_anthropic_scoped_skips_without_oauth_token(tmp_path, monkeypatch):
    import types
    tmp_home = tmp_path / ".hermes"
    plugin_mod, core_mod = _load_plugin(tmp_home, monkeypatch)
    creds = types.ModuleType("agent.anthropic_credentials")
    creds.resolve_anthropic_token = lambda: None
    creds._is_oauth_token = lambda t: False
    monkeypatch.setitem(sys.modules, "agent", sys.modules.get("agent") or types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.anthropic_credentials", creds)
    assert plugin_mod._fetch_anthropic_scoped() is None


def test_fetch_anthropic_scoped_import_failure_reports_kind_import(tmp_path, monkeypatch):
    tmp_home = tmp_path / ".hermes"
    plugin_mod, core_mod = _load_plugin(tmp_home, monkeypatch)
    monkeypatch.setitem(sys.modules, "agent.anthropic_credentials", None)  # forces ImportError
    ok, kind, msg, windows = plugin_mod._fetch_anthropic_scoped_outcome()
    assert ok is False and kind == "import"


def test_refresh_wires_scoped_windows_into_anthropic_snapshot(tmp_path, monkeypatch):
    import types
    tmp_home = tmp_path / ".hermes"
    plugin_mod, core_mod = _load_plugin(tmp_home, monkeypatch)
    now = time.time()
    good = types.SimpleNamespace(fetched_at=now, unavailable_reason=None, plan=None, details=[],
                                 windows=[types.SimpleNamespace(label="Current week", used_percent=10.0,
                                                                resets_at=None)])
    fake_usage = types.ModuleType("agent.account_usage")
    fake_usage.fetch_account_usage = lambda p: good if p == "anthropic" else good
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.account_usage", fake_usage)

    monkeypatch.setattr(plugin_mod, "_fetch_anthropic_scoped",
                        lambda: [{"label": "Fable week (model)", "used_percent": 42.0,
                                 "resets_at": now + 3600, "scope": "Fable"}])
    plugin_mod.refresh(now=now)
    state_file = tmp_home / "cache" / "quota-router" / "state.json"
    st = core_mod.read_state(state_file)
    windows = st["providers"]["anthropic"]["snapshot"]["windows"]
    assert any(w.get("scope") == "Fable" and w["used"] == 42.0 for w in windows)


def test_refresh_scoped_exception_sets_scoped_error_but_keeps_core_windows(tmp_path, monkeypatch):
    import types
    tmp_home = tmp_path / ".hermes"
    plugin_mod, core_mod = _load_plugin(tmp_home, monkeypatch)
    now = time.time()
    good = types.SimpleNamespace(fetched_at=now, unavailable_reason=None, plan=None, details=[],
                                 windows=[types.SimpleNamespace(label="Current week", used_percent=10.0,
                                                                resets_at=None)])
    fake_usage = types.ModuleType("agent.account_usage")
    fake_usage.fetch_account_usage = lambda p: good
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.account_usage", fake_usage)

    def boom():
        raise RuntimeError("scoped boom")
    monkeypatch.setattr(plugin_mod, "_fetch_anthropic_scoped", boom)
    plugin_mod.refresh(now=now)
    state_file = tmp_home / "cache" / "quota-router" / "state.json"
    st = core_mod.read_state(state_file)
    assert st["providers"]["anthropic"]["fetch"]["scoped"]["error"]["kind"] == "scoped_error"
    assert any(w["label"] == "Current week" for w in st["providers"]["anthropic"]["snapshot"]["windows"])


def test_scoped_fetch_throttled_within_300s(tmp_path, monkeypatch):
    import types
    tmp_home = tmp_path / ".hermes"
    plugin_mod, core_mod = _load_plugin(tmp_home, monkeypatch)
    now = time.time()
    good = types.SimpleNamespace(fetched_at=now, unavailable_reason=None, plan=None, details=[],
                                 windows=[types.SimpleNamespace(label="Current week", used_percent=10.0,
                                                                resets_at=None)])
    fake_usage = types.ModuleType("agent.account_usage")
    fake_usage.fetch_account_usage = lambda p: good
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.account_usage", fake_usage)

    calls = {"n": 0}
    def fake_scoped():
        calls["n"] += 1
        return [{"label": "Fable week (model)", "used_percent": 5.0, "resets_at": None, "scope": "Fable"}]
    monkeypatch.setattr(plugin_mod, "_fetch_anthropic_scoped", fake_scoped)

    plugin_mod.refresh(now=now)
    plugin_mod.refresh(now=now + 10)  # within 300s: should not call scoped fetch again
    assert calls["n"] == 1
    plugin_mod.refresh(now=now + 400)  # past throttle: fetch again
    assert calls["n"] == 2


def test_status_payload_json_line_round_trips_and_no_secrets(tmp_path, monkeypatch):
    import json
    tmp_home = tmp_path / ".hermes"
    plugin_mod, core_mod = _load_plugin(tmp_home, monkeypatch)
    now = time.time()
    with core_mod.locked_state(core_mod.state_path()) as st:
        core_mod.note_fetch(st, "anthropic", ok=False, kind="fetch_error",
                            message="Bearer x sk-ant-x oat01-x", now=now, cfg=dict(core_mod.DEFAULTS))
    payload = core_mod.status_payload(core_mod.read_state(core_mod.state_path()), now, dict(core_mod.DEFAULTS), "h")
    line = "QRJSON " + json.dumps(payload)
    parsed = json.loads(line[len("QRJSON "):])
    assert parsed["v"] == 1 and "anthropic" in parsed["providers"]
    assert "Bearer" not in line and "sk-ant-" not in line and "oat01" not in line
