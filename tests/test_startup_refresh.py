"""Tests for the on_session_start startup refresh.

Written against the review findings (r1..r4): per-provider throttle,
single-flight dedupe, drain=False preservation, and fail-open behavior. An
autouse tripwire records any real outbound TCP connect and fails the test at
teardown (credential/DNS resolution are NOT intercepted); the tests never
touch the real ~/.hermes/cache/quota-router/state.json — they use the
tmp-HERMES_HOME harness pattern from test_init_queue.py.
"""
import importlib.util
import json
import re
import socket
import sys
import threading
import time
import types
from pathlib import Path

import pytest

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
    fetching, not toward silently skipping (r1-#5). The payload must carry
    version: 1 or read_state discards it wholesale."""
    plugin_mod, core_mod = _load_plugin(tmp_path / ".hermes", monkeypatch)
    now = 1_800_000_000.0
    state_file = core_mod.state_path()
    state_file.parent.mkdir(parents=True, exist_ok=True)
    # Non-numeric last_ok_at for anthropic, well-formed fresh ok for codex:
    # anthropic must be due, codex must NOT be — this is the r1-#5 per-field fallback.
    state_file.write_text(json.dumps({
        "version": 1,
        "providers": {
            "anthropic": {"fetch": {"core": {"last_ok_at": "not-a-number"}}},
            "openai-codex": {"fetch": {"core": {"last_ok_at": now}}},
        },
    }))
    due = plugin_mod._startup_refresh_due(core_mod, now)
    assert due == {"anthropic"}, due
    # providers that is not a dict: everything due, no raise.
    state_file.write_text(json.dumps({"version": 1, "providers": ["not", "a", "dict"]}))
    assert plugin_mod._startup_refresh_due(core_mod, now) == set(core_mod.WATCHED)
    # A garbage (non-JSON) file: read_state swallows the ValueError and yields
    # empty_state, so every provider is due — same fail-open direction, no raise.
    state_file.write_text("not json at all")
    assert plugin_mod._startup_refresh_due(core_mod, now) == set(core_mod.WATCHED)
    # The predicate's own outer except (defence-in-depth): make read_state itself
    # raise and confirm every provider is due rather than a crash.
    monkeypatch.setattr(core_mod, "read_state",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk gone")))
    assert plugin_mod._startup_refresh_due(core_mod, now) == set(core_mod.WATCHED)


def _join_startup_threads(module, timeout=5.0):
    """Wait out any daemon startup-refresh worker this test spawned so it cannot
    outlive monkeypatch teardown and touch the real host state file."""
    deadline = time.time() + timeout
    for t in threading.enumerate():
        if t.name == "quota-router-startup-refresh" and t is not threading.current_thread():
            t.join(timeout=max(0.0, deadline - time.time()))


@pytest.fixture(autouse=True)
def _offline_tripwire(monkeypatch):
    """Record any outbound TCP connect attempt made by a test in this file and
    fail the test at teardown.

    A future hook test that forgets to fake refresh() would otherwise silently
    hit the live usage endpoints (the r3 high). socket.socket.connect is the
    common chokepoint for httpx/urllib. Product code swallows exceptions on
    background threads, so the hit is RECORDED here and asserted at teardown —
    not raised at the connect site. Note limits: DNS resolution (getaddrinfo)
    and credential resolution are not intercepted; only the TCP connect is
    blocked. Tests that legitimately need a socket may opt out by inserting a
    marker: monkeypatch.setitem(sys.modules, '_qr_allow_sockets', True).
    """
    real_socket = socket.socket
    hits = []

    class _NoNetSocket(real_socket):
        def connect(self, address):
            if sys.modules.get("_qr_allow_sockets"):
                return super().connect(address)
            hits.append(address)
            raise AssertionError(
                "test tried a real network connection — fake refresh()/fetchers "
                "instead (see tests/test_startup_refresh.py docstring)")

    monkeypatch.setattr(socket, "socket", _NoNetSocket)
    yield
    if hits:
        pytest.fail(f"test attempted {len(hits)} real outbound network connection(s) "
                    f"(first: {hits[0]!r}) — fake refresh()/fetchers instead")


@pytest.fixture(autouse=True)
def _no_stray_startup_threads(monkeypatch):
    """Fail if any quota-router-startup-refresh worker is alive at test end.

    Requesting monkeypatch makes this fixture set up AFTER monkeypatch and tear
    down BEFORE it — a leaked worker is caught while the fakes are still active,
    not after sys.modules has been restored to the real (host-bound) modules. A
    leak is failed even if it exits during the grace window.
    """
    before = {t for t in threading.enumerate() if t.name == "quota-router-startup-refresh"}
    saw_leak = []

    yield

    now_leaked = [t for t in threading.enumerate()
                  if t.name == "quota-router-startup-refresh"
                  and t is not threading.current_thread() and t not in before]
    if now_leaked:
        saw_leak.extend(now_leaked)
        deadline = time.time() + 2.0
        while time.time() < deadline:
            alive = [t for t in saw_leak if t.is_alive()]
            if not alive:
                break
            for t in alive:
                t.join(timeout=0.2)
    if saw_leak:
        pytest.fail(f"startup-refresh worker(s) outlived the test "
                    f"(would race teardown with real modules restored): {saw_leak}")


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
    _join_startup_threads(plugin_mod)


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
    # Join the just-released worker before teardown, or the leak guard can catch
    # it mid-exit and fail the test on a false alarm.
    _join_startup_threads(plugin_mod)


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
    _join_startup_threads(plugin_mod)


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

    real_due = plugin_mod._startup_refresh_due

    def seed_then_check(core_arg, at):
        # Simulates the cron refreshing everything between the hook's check and the
        # worker's re-check: mark all providers fresh, then run the REAL predicate.
        for p in core_arg.WATCHED:
            _seed_ok_fetch(core_arg, p, at)
        return real_due(core_arg, at)

    monkeypatch.setattr(plugin_mod, "_startup_refresh_due", seed_then_check)
    assert plugin_mod._STARTUP_LOCK.acquire(blocking=False)
    plugin_mod._startup_refresh_worker()
    assert calls == [], "worker refreshed despite everything fresh at re-check"


def test_refresh_only_filter_skips_fresh_providers(tmp_path, monkeypatch):
    """The refresh()-side half of the r1-#1 fix: only= restricts which providers are
    fetched. With only={'openai-codex'}, the anthropic fetcher must never run even
    though anthropic is due. Deleting the filter reintroduces the r1 high at the
    fetch layer — this test must fail if it is."""
    plugin_mod, core_mod = _load_plugin(tmp_path / ".hermes", monkeypatch)

    fetched = []

    class FakeSnap:
        fetched_at = 1_800_000_000.0
        unavailable_reason = None
        plan = None
        details = []
        windows = []

    # Inject a fake agent.account_usage module (same pattern as test_init_queue.py)
    # so no real network fetch and no dependency on PYTHONPATH for this test.
    fake_agent = types.ModuleType("agent")
    fake_au = types.ModuleType("agent.account_usage")
    fake_au.fetch_account_usage = lambda p, **k: fetched.append(p) or FakeSnap()
    fake_agent.account_usage = fake_au
    monkeypatch.setitem(sys.modules, "agent", fake_agent)
    monkeypatch.setitem(sys.modules, "agent.account_usage", fake_au)
    # The scoped anthropic fetch resolves the operator's real OAuth token and GETs
    # api.anthropic.com — stub it so this test is fully offline.
    monkeypatch.setattr(plugin_mod, "_fetch_anthropic_raw", lambda: None)
    monkeypatch.setattr(plugin_mod, "_fetch_anthropic_scoped_outcome",
                        lambda: (True, None, "", None, None))

    now = time.time()
    plugin_mod.refresh(drain=False, only={"openai-codex"})
    assert fetched == ["openai-codex"], fetched

    # Sanity: no only= (the cron path) fetches everything due.
    fetched.clear()
    plugin_mod.refresh(drain=False)
    assert set(fetched) == set(core_mod.WATCHED), fetched


def test_thread_spawn_failure_releases_the_lock(tmp_path, monkeypatch):
    """If threading.Thread fails to start, the lock acquired by the hook must be
    released — otherwise startup refresh is permanently disabled in the process."""
    plugin_mod, core_mod = _load_plugin(tmp_path / ".hermes", monkeypatch)

    seen = {}

    def fake_refresh(now=None, snapshots=None, drain=False, outcomes=None, only=None):
        seen["only"] = only
        return []

    monkeypatch.setattr(plugin_mod, "refresh", fake_refresh)
    real_thread = plugin_mod.threading.Thread

    def boom_thread(*a, **k):
        raise RuntimeError("cannot spawn thread")

    monkeypatch.setattr(plugin_mod.threading, "Thread", boom_thread)
    plugin_mod._on_session_start(session_id="s5")  # must not raise
    # The lock is free again: the next start can acquire it and refresh.
    assert not plugin_mod._STARTUP_LOCK.locked()

    monkeypatch.setattr(plugin_mod.threading, "Thread", real_thread)
    plugin_mod._on_session_start(session_id="s6")
    deadline = time.time() + 5
    while "only" not in seen and time.time() < deadline:
        time.sleep(0.02)
    assert "only" in seen, "startup refresh never ran after spawn-failure recovery"
    _join_startup_threads(plugin_mod)


def test_hook_never_raises_on_unreadable_state_path(tmp_path, monkeypatch):
    """With an unreadable state path the hook must still not raise. refresh is faked
    (like every other hook test) so the spawned daemon worker can never reach the
    real host state file even if it races monkeypatch teardown; the test joins its
    own worker via _join_startup_threads, and the autouse leak guard fails on any
    worker that still outlives the test."""
    plugin_mod, _ = _load_plugin(tmp_path / ".nonexistent-home", monkeypatch)
    monkeypatch.setattr(plugin_mod, "refresh",
                        lambda now=None, snapshots=None, drain=False, outcomes=None, only=None: [])
    plugin_mod._on_session_start(session_id="s4")  # must not raise
    _join_startup_threads(plugin_mod)
