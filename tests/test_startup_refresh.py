"""Tests for the on_session_start startup refresh.

Written against the review findings (r1): per-provider throttle, single-flight
dedupe, drain=False preservation, and fail-open behavior. Never touches the real
~/.hermes/cache/quota-router/state.json — uses the tmp-HERMES_HOME harness
pattern from test_init_queue.py.
"""
import importlib.util
import re
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
    # Each test gets a fresh module => fresh _STARTUP_LOCK.
    return plugin_mod, core_mod


def _seed_ok_fetch(core_mod, provider, at):
    cfg = dict(core_mod.DEFAULTS)
    with core_mod.locked_state(core_mod.state_path()) as st:
        core_mod.note_fetch(st, provider, ok=True, kind=None, message="", now=at, cfg=cfg)


def test_hook_registered_on_session_start(tmp_path, monkeypatch):
    plugin_mod, _ = _load_plugin(tmp_path / ".hermes", monkeypatch)
    registered = {}

    class Ctx:
        def register_hook(self, name, cb):
            registered[name] = cb

        def register_cli_command(self, *a, **k):
            pass

        def register_command(self, *a, **k):
            pass

    plugin_mod.register(Ctx())
    assert registered.get("on_session_start") is plugin_mod._on_session_start


def test_startup_due_set_is_per_provider(tmp_path, monkeypatch):
    """The r1 high finding: a permanently broken provider must not force refreshes of
    the healthy one. Due-ness is judged per provider, so one stale/broken provider
    makes exactly that provider due — never the whole WATCHED set."""
    plugin_mod, core_mod = _load_plugin(tmp_path / ".hermes", monkeypatch)
    now = 1_800_000_000.0
    watched = list(core_mod.WATCHED)
    assert len(watched) == 2

    # No state at all: everything due.
    assert plugin_mod._startup_refresh_due(core_mod, now) == set(watched)

    # anthropic fresh, codex never fetched: only codex due.
    _seed_ok_fetch(core_mod, watched[0], now)
    assert plugin_mod._startup_refresh_due(core_mod, now + 60) == {watched[1]}

    # Past the throttle window: that provider is due again.
    assert watched[0] in plugin_mod._startup_refresh_due(core_mod, now + plugin_mod.STARTUP_REFRESH_THROTTLE_S + 1)

    # Both fresh: nothing due.
    _seed_ok_fetch(core_mod, watched[1], now)
    assert plugin_mod._startup_refresh_due(core_mod, now + 60) == set()


def test_startup_due_survives_corrupt_state_fields(tmp_path, monkeypatch):
    """Malformed last_ok_at / non-dict providers must not raise and must fail toward
    fetching, not toward silently skipping (r1 nit)."""
    plugin_mod, core_mod = _load_plugin(tmp_path / ".hermes", monkeypatch)
    state_file = core_mod.state_path()
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text('{"providers": {"anthropic": {"fetch": {"core": '
                          '{"last_ok_at": "not-a-number"}}}, "mode": "normal"}')
    due = plugin_mod._startup_refresh_due(core_mod, time.time())
    assert "anthropic" in due
    # Also: a garbage file must not raise.
    state_file.write_text("not json at all")
    assert plugin_mod._startup_refresh_due(core_mod, time.time()) == set(core_mod.WATCHED)


def test_hook_fetches_only_due_providers_and_never_drains(tmp_path, monkeypatch):
    """The hook's refresh call must pass only= due set (the healthy provider is not
    re-fetched because a sibling credential is broken) and drain=False (only the cron
    drains the alert queue — README 'Alert queue')."""
    tmp_home = tmp_path / ".hermes"
    plugin_mod, core_mod = _load_plugin(tmp_home, monkeypatch)
    now = time.time()
    watched = list(core_mod.WATCHED)
    _seed_ok_fetch(core_mod, watched[0], now)  # healthy
    # watched[1] broken/never fetched

    seen = {}

    def fake_refresh(now=None, snapshots=None, drain=False, outcomes=None, only=None):
        seen["drain"] = drain
        seen["only"] = only
        return []

    monkeypatch.setattr(plugin_mod, "refresh", fake_refresh)
    plugin_mod._on_session_start(session_id="s1")
    # The hook itself must not block: wait for the daemon thread.
    deadline = time.time() + 5
    while "only" not in seen and time.time() < deadline:
        time.sleep(0.02)
    assert seen["only"] == {watched[1]}, seen
    # drain=False is a frozen contract: the cron owns the alert queue.
    assert seen["drain"] is False
    assert watched[0] not in seen["only"]


def test_hook_single_flight_under_concurrent_starts(tmp_path, monkeypatch):
    """N simultaneous session starts => at most ONE refresh (r1 medium: thundering
    herd). The first due start wins the lock; the rest skip without spawning."""
    plugin_mod, core_mod = _load_plugin(tmp_path / ".hermes", monkeypatch)

    calls = []
    release = threading.Event()

    def slow_refresh(now=None, snapshots=None, drain=False, outcomes=None, only=None):
        calls.append(only)
        release.wait(timeout=5)  # hold the "network call" open

    monkeypatch.setattr(plugin_mod, "refresh", slow_refresh)
    started = threading.Barrier(4)

    def fire(i):
        started.wait(timeout=5)
        plugin_mod._on_session_start(session_id=f"s{i}")

    threads = [threading.Thread(target=fire, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    # Allow the worker thread to have actually entered refresh.
    deadline = time.time() + 5
    while not calls and time.time() < deadline:
        time.sleep(0.02)
    release.set()
    deadline = time.time() + 5
    while len(calls) < 1 and time.time() < deadline:
        time.sleep(0.02)
    assert len(calls) == 1, f"expected single-flight, got {len(calls)} refresh calls"


def test_hook_is_async_not_inline(tmp_path, monkeypatch):
    """The refresh must run on a different thread than the caller (session start
    never blocks on the network)."""
    plugin_mod, _ = _load_plugin(tmp_path / ".hermes", monkeypatch)
    caller_thread = threading.current_thread()

    seen = {}

    def spy_refresh(now=None, snapshots=None, drain=False, outcomes=None, only=None):
        seen["thread"] = threading.current_thread()
        seen["daemon"] = threading.current_thread().daemon
        return []

    monkeypatch.setattr(plugin_mod, "refresh", spy_refresh)
    plugin_mod._on_session_start(session_id="s1")
    deadline = time.time() + 5
    while "thread" not in seen and time.time() < deadline:
        time.sleep(0.02)
    assert "thread" in seen, "refresh never ran"
    assert seen["thread"] is not caller_thread
    assert seen["daemon"] is True


def test_hook_skips_entirely_when_all_fresh(tmp_path, monkeypatch):
    plugin_mod, core_mod = _load_plugin(tmp_path / ".hermes", monkeypatch)
    now = time.time()
    for p in core_mod.WATCHED:
        _seed_ok_fetch(core_mod, p, now)

    called = threading.Event()

    def fake_refresh(now=None, snapshots=None, drain=False, outcomes=None, only=None):
        called.set()
        return []

    monkeypatch.setattr(plugin_mod, "refresh", fake_refresh)
    plugin_mod._on_session_start(session_id="s2")
    assert not called.wait(timeout=1.0), "startup refresh ran despite all providers fresh"


def test_hook_never_raises_and_survives_core_failure(tmp_path, monkeypatch):
    plugin_mod, _ = _load_plugin(tmp_path / ".hermes", monkeypatch)
    monkeypatch.setattr(plugin_mod, "_core", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    # Must not raise into the session-start caller.
    plugin_mod._on_session_start(session_id="s3")


def test_worker_releases_lock_on_refresh_failure(tmp_path, monkeypatch):
    """A crashing refresh() must not leave _STARTUP_LOCK held forever (that would
    permanently disable startup refreshes)."""
    plugin_mod, core_mod = _load_plugin(tmp_path / ".hermes", monkeypatch)

    def boom(now=None, snapshots=None, drain=False, outcomes=None, only=None):
        raise RuntimeError("network exploded")

    monkeypatch.setattr(plugin_mod, "refresh", boom)
    # The hook acquires; the worker releases. Mirror that contract.
    assert plugin_mod._STARTUP_LOCK.acquire(blocking=False)
    plugin_mod._startup_refresh_worker()  # must not raise
    # The lock is released: a subsequent single-flight acquire succeeds.
    assert plugin_mod._STARTUP_LOCK.acquire(blocking=False)
    plugin_mod._STARTUP_LOCK.release()


def test_worker_rereads_due_under_lock(tmp_path, monkeypatch):
    """The worker re-checks due-ness after winning the lock: a refresh completed by
    another path (cron) between hook and worker must not be duplicated."""
    plugin_mod, core_mod = _load_plugin(tmp_path / ".hermes", monkeypatch)
    now = time.time()

    calls = []

    def fake_refresh(now=None, snapshots=None, drain=False, outcomes=None, only=None):
        calls.append(only)
        return []

    monkeypatch.setattr(plugin_mod, "refresh", fake_refresh)

    def seed_then_check(core_arg, at):
        # Runs inside the worker (its _core is our core_mod): by the time it checks,
        # all providers are fresh => due set empty => refresh must not be called.
        for p in core_arg.WATCHED:
            _seed_ok_fetch(core_arg, p, at)
        return plugin_mod._startup_refresh_due(core_arg, at)

    monkeypatch.setattr(plugin_mod, "_startup_refresh_due", seed_then_check)
    assert plugin_mod._STARTUP_LOCK.acquire(blocking=False)
    plugin_mod._startup_refresh_worker()
    assert calls == [], "worker refreshed despite everything fresh at re-check"


def test_hook_never_raises_on_unreadable_state_path(tmp_path, monkeypatch):
    """With an unwritable/unreadable state path the hook must still not raise."""
    plugin_mod, _ = _load_plugin(
        tmp_path / ".nonexistent-home", monkeypatch)
    plugin_mod._on_session_start(session_id="s4")  # must not raise
