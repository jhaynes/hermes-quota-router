"""Tests for the on_session_start startup refresh (red-first: written before the hook landed).

Never touches the real ~/.hermes/cache/quota-router/state.json — uses the tmp-HERMES_HOME
harness from test_init_queue.py.
"""
import importlib.util
import sys
import threading
import time
from pathlib import Path

_root = Path(__file__).resolve().parents[1]


def _load_plugin(tmp_home, monkeypatch):
    core_spec = importlib.util.spec_from_file_location("qr_core_startup", _root / "core.py")
    core_mod = importlib.util.module_from_spec(core_spec)
    monkeypatch.setitem(sys.modules, "qr_core_startup", core_mod)
    core_spec.loader.exec_module(core_mod)

    plugin_spec = importlib.util.spec_from_file_location("qr_plugin_startup", _root / "__init__.py")
    plugin_mod = importlib.util.module_from_spec(plugin_spec)
    monkeypatch.setitem(sys.modules, "qr_plugin_startup", plugin_mod)
    plugin_spec.loader.exec_module(plugin_mod)

    monkeypatch.setitem(sys.modules, "quota_router_core", core_mod)
    core_mod.state_path = lambda hermes_home=None: tmp_home / "cache" / "quota-router" / "state.json"
    core_mod.state_db_path = lambda hermes_home=None: tmp_home / "state.db"
    return plugin_mod, core_mod


def _mark_fetched(core_mod, tmp_home, provider, ok_at, now):
    """Seed a fetch outcome directly through the public note_fetch path."""
    cfg = dict(core_mod.DEFAULTS)
    with core_mod.locked_state(core_mod.state_path()) as st:
        core_mod.note_fetch(st, provider, ok=True, kind=None, message="", now=ok_at, cfg=cfg)


def test_hook_registered_on_session_start(tmp_path, monkeypatch):
    tmp_home = tmp_path / ".hermes"
    plugin_mod, core_mod = _load_plugin(tmp_home, monkeypatch)

    registered = {}

    class Ctx:
        def register_hook(self, name, cb):
            registered[name] = cb

        def register_cli_command(self, *a, **k):
            pass

        def register_command(self, *a, **k):
            pass

    plugin_mod.register(Ctx())
    assert "on_session_start" in registered
    assert callable(registered["on_session_start"])


def test_startup_due_when_no_state_and_false_after_fresh_ok_fetch(tmp_path, monkeypatch):
    tmp_home = tmp_path / ".hermes"
    plugin_mod, core_mod = _load_plugin(tmp_home, monkeypatch)
    now = 1_800_000_000.0

    # No state file at all: due.
    assert plugin_mod._startup_refresh_due(core_mod, now) is True

    # Fresh successful fetch for every watched provider: not due.
    with core_mod.locked_state(core_mod.state_path()) as st:
        cfg = dict(core_mod.DEFAULTS)
        for p in core_mod.WATCHED:
            core_mod.note_fetch(st, p, ok=True, kind=None, message="", now=now, cfg=cfg)
    assert plugin_mod._startup_refresh_due(core_mod, now + 60) is False

    # Past the throttle window: due again.
    assert plugin_mod._startup_refresh_due(core_mod, now + plugin_mod.STARTUP_REFRESH_THROTTLE_S + 1) is True


def test_startup_due_true_when_one_provider_stale(tmp_path, monkeypatch):
    tmp_home = tmp_path / ".hermes"
    plugin_mod, core_mod = _load_plugin(tmp_home, monkeypatch)
    now = 1_800_000_000.0
    cfg = dict(core_mod.DEFAULTS)
    with core_mod.locked_state(core_mod.state_path()) as st:
        for i, p in enumerate(core_mod.WATCHED):
            core_mod.note_fetch(st, p, ok=True, kind=None, message="", now=now - i * 900, cfg=cfg)
    # openai-codex (stale) makes it due even though anthropic is fresh.
    assert plugin_mod._startup_refresh_due(core_mod, now) is True


def test_hook_runs_refresh_in_background(tmp_path, monkeypatch):
    tmp_home = tmp_path / ".hermes"
    plugin_mod, core_mod = _load_plugin(tmp_home, monkeypatch)
    now = 1_800_000_000.0

    called = threading.Event()

    def fake_refresh(now=None, snapshots=None, drain=False, outcomes=None):
        called.set()
        return []

    monkeypatch.setattr(plugin_mod, "refresh", fake_refresh)
    plugin_mod._on_session_start(session_id="s1")
    assert called.wait(timeout=5), "startup refresh did not run on session start"


def test_hook_skips_refresh_when_all_fresh(tmp_path, monkeypatch):
    tmp_home = tmp_path / ".hermes"
    plugin_mod, core_mod = _load_plugin(tmp_home, monkeypatch)
    now = time.time()
    cfg = dict(core_mod.DEFAULTS)
    with core_mod.locked_state(core_mod.state_path()) as st:
        for p in core_mod.WATCHED:
            core_mod.note_fetch(st, p, ok=True, kind=None, message="", now=now, cfg=cfg)

    called = threading.Event()

    def fake_refresh(now=None, snapshots=None, drain=False, outcomes=None):
        called.set()
        return []

    monkeypatch.setattr(plugin_mod, "refresh", fake_refresh)
    plugin_mod._on_session_start(session_id="s2")
    assert not called.wait(timeout=1.0), "startup refresh ran despite all providers fresh"


def test_hook_never_raises_and_survives_core_failure(tmp_path, monkeypatch):
    tmp_home = tmp_path / ".hermes"
    plugin_mod, core_mod = _load_plugin(tmp_home, monkeypatch)
    monkeypatch.setattr(plugin_mod, "_core", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    # Must not raise.
    plugin_mod._on_session_start(session_id="s3")
