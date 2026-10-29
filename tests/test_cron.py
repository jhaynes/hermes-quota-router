"""Cron wiring + fetch backoff (plan Task 5; v2 "5-min cadence")."""
import importlib.util
import os
import stat
import subprocess
import sys
import types
from pathlib import Path

import pytest

_root = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("qr_core_cron", _root / "core.py")
core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(core)
CFG = dict(core.DEFAULTS)
M = 60.0
NOW = 1_800_000_000.0
REAL_STATE = Path.home() / ".hermes" / "cache" / "quota-router" / "state.json"


def fail(st, provider, t, kind="no_data"):
    core.note_fetch(st, provider, ok=False, kind=kind, message="x", now=t, cfg=CFG)


def test_fetch_is_due_until_three_failures_within_an_hour():
    st = core.empty_state()
    assert core.fetch_due(st, "anthropic", NOW, CFG)
    fail(st, "anthropic", NOW - 50 * M)
    fail(st, "anthropic", NOW - 10 * M)
    assert core.fetch_due(st, "anthropic", NOW, CFG)
    fail(st, "anthropic", NOW - 5 * M)
    assert not core.fetch_due(st, "anthropic", NOW, CFG)          # 5 min since last attempt
    assert core.fetch_due(st, "anthropic", NOW + 10 * M, CFG)     # 15 min since last attempt
    assert core.fetch_due(st, "openai-codex", NOW, CFG)           # per provider


def test_old_failures_and_scoped_failures_do_not_count():
    st = core.empty_state()
    for t in (NOW - 70 * M, NOW - 65 * M, NOW - 5 * M):
        fail(st, "anthropic", t)
    core.note_fetch(st, "anthropic", slot="scoped", ok=False, kind="scoped_error", now=NOW - 4 * M, cfg=CFG)
    assert core.fetch_due(st, "anthropic", NOW, CFG)


def test_backoff_alerts_once_per_incident_and_clears_on_success():
    st = core.empty_state()
    for i in range(3):
        fail(st, "anthropic", NOW - (20 - i) * M)
    core.fetch_due(st, "anthropic", NOW, CFG)
    core.fetch_due(st, "anthropic", NOW + M, CFG)
    lines = [l for l in st["pending"] if "backing off" in l]
    assert lines == ["quota-router: anthropic usage fetch backing off to 15m (3 non-ok fetches in 60m)"]
    core.note_fetch(st, "anthropic", ok=True, now=NOW + 20 * M, cfg=CFG)
    assert core.fetch_due(st, "anthropic", NOW + 21 * M, CFG)
    assert "backoff_alerted" not in st["providers"]["anthropic"]["fetch"]["core"]


def _load_plugin(tmp_home, monkeypatch):
    core_mod = importlib.util.module_from_spec(
        spec := importlib.util.spec_from_file_location("qr_core_croncase", _root / "core.py"))
    monkeypatch.setitem(sys.modules, "qr_core_croncase", core_mod)
    spec.loader.exec_module(core_mod)
    plugin = importlib.util.module_from_spec(
        pspec := importlib.util.spec_from_file_location("qr_plugin_croncase", _root / "__init__.py"))
    monkeypatch.setitem(sys.modules, "qr_plugin_croncase", plugin)
    pspec.loader.exec_module(plugin)
    monkeypatch.setitem(sys.modules, "quota_router_core", core_mod)
    core_mod.state_path = lambda hermes_home=None: tmp_home / "cache" / "quota-router" / "state.json"
    return plugin, core_mod


def test_live_refresh_skips_a_provider_in_backoff(tmp_path, monkeypatch):
    real_mtime = REAL_STATE.stat().st_mtime if REAL_STATE.exists() else None
    plugin, core_mod = _load_plugin(tmp_path / ".hermes", monkeypatch)
    calls = []

    def fetch_account_usage(p):
        calls.append(p)
        return None

    fake = types.ModuleType("agent.account_usage")
    fake.fetch_account_usage = fetch_account_usage
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.account_usage", fake)
    for i in range(3):
        plugin.refresh(now=NOW + i * 5 * M)
    assert calls.count("anthropic") == 3
    calls.clear()
    plugin.refresh(now=NOW + 15 * M)            # 5 min after the third failure: skipped
    assert calls == []
    plugin.refresh(now=NOW + 25 * M)            # 15 min after: due again
    assert set(calls) == {"anthropic", "openai-codex"}
    if real_mtime is not None:
        assert REAL_STATE.stat().st_mtime == real_mtime


def _fake_hermes(tmp_path, refresh_rc=0, apply_rc=0, stamp_rc=0):
    log = tmp_path / "calls.log"
    exe = tmp_path / "hermes"
    exe.write_text(f"""#!/bin/sh
echo "$@" >> {log}
case "$2 $3" in
  refresh*) echo "quota-router: refresh line"; echo "noise"; echo "boom" >&2; exit {refresh_rc};;
  "throttle apply") echo "quota-router: throttle line"; echo "noise"; exit {apply_rc};;
  "throttle stamp-resume") echo "quota-router: stamp line"; echo "resume stamp: off (x)"; exit {stamp_rc};;
esac
""")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return exe, log


STEPS = ["quota-router refresh", "quota-router throttle apply", "quota-router throttle stamp-resume"]


def _run_script(exe, **extra_env):
    env = dict(os.environ, QR_HERMES_BIN=str(exe), **extra_env)
    return subprocess.run([sys.executable, str(_root / "scripts" / "quota-router-refresh.py")],
                          capture_output=True, text=True, timeout=60, env=env)


def test_cron_script_runs_refresh_then_throttle_apply_then_stamp(tmp_path):
    exe, log = _fake_hermes(tmp_path)
    r = _run_script(exe)
    assert r.returncode == 0
    assert log.read_text().splitlines() == STEPS
    assert r.stdout.splitlines() == ["quota-router: refresh line", "quota-router: throttle line",
                                     "quota-router: stamp line"]


def test_cron_script_applies_even_when_refresh_fails(tmp_path):
    exe, log = _fake_hermes(tmp_path, refresh_rc=3)
    r = _run_script(exe)
    assert r.returncode == 1
    assert log.read_text().splitlines() == STEPS
    assert "quota-router refresh failed (exit 3): boom" in r.stdout
    assert "quota-router: throttle line" in r.stdout


def test_cron_script_reports_a_failed_apply(tmp_path):
    exe, _log = _fake_hermes(tmp_path, apply_rc=4)
    r = _run_script(exe)
    assert r.returncode == 1
    assert "quota-router throttle apply failed (exit 4)" in r.stdout
    assert "quota-router: stamp line" in r.stdout


def test_cron_script_reports_a_failed_stamp(tmp_path):
    exe, _log = _fake_hermes(tmp_path, stamp_rc=5)
    r = _run_script(exe)
    assert r.returncode == 1
    assert "quota-router throttle stamp-resume failed (exit 5)" in r.stdout


def test_cron_script_applies_even_when_refresh_hangs(tmp_path):
    exe, log = _fake_hermes(tmp_path)
    exe.write_text(exe.read_text().replace('refresh*) echo', 'refresh*) sleep 5; echo'))
    r = _run_script(exe, QR_STEP_TIMEOUT="1")
    assert r.returncode == 1
    assert log.read_text().splitlines() == STEPS
    assert "quota-router refresh failed (timed out after 1s)" in r.stdout
    assert "quota-router: throttle line" in r.stdout


def test_cron_script_reports_a_missing_binary_for_every_step(tmp_path):
    r = _run_script(tmp_path / "no-such-hermes")
    assert r.returncode == 1
    assert [l.split(" (")[0] for l in r.stdout.splitlines()] == [
        "quota-router refresh failed", "quota-router throttle apply failed",
        "quota-router throttle stamp-resume failed"]
    assert "Traceback" not in r.stderr


# ── issue #16: a malformed QR_STEP_TIMEOUT must not abort the run with a traceback ──
# Unset/empty keeps the 300 s default; a non-numeric or non-finite value (inf/-inf/nan,
# which float() accepts) warns once and falls back to 300 s; every step still runs and
# step-failure isolation is unchanged. Zero/negative stay "valid": steps fail fast, no
# traceback (pre-existing behavior).
@pytest.mark.parametrize("raw,why", [("abc", "not a number"), ("12x", "not a number"),
                                     ("inf", "not finite"), ("-inf", "not finite"),
                                     ("nan", "not finite"), ("1e400", "not finite")])
def test_cron_script_falls_back_to_300_on_a_bad_step_timeout(tmp_path, raw, why):
    exe, log = _fake_hermes(tmp_path)
    r = _run_script(exe, QR_STEP_TIMEOUT=raw)
    assert r.returncode == 0
    assert log.read_text().splitlines() == STEPS
    assert r.stdout.splitlines() == [
        f"QR_STEP_TIMEOUT={raw!r} is {why}; using 300s",
        "quota-router: refresh line", "quota-router: throttle line", "quota-router: stamp line"]
    assert "Traceback" not in r.stderr          # controlled fallback, never an abort


def test_cron_script_keeps_a_valid_step_timeout_and_default(tmp_path):
    exe, log = _fake_hermes(tmp_path)
    r = _run_script(exe, QR_STEP_TIMEOUT="2.5")
    assert r.returncode == 0
    assert log.read_text().splitlines() == STEPS
    assert r.stdout.splitlines() == ["quota-router: refresh line", "quota-router: throttle line",
                                     "quota-router: stamp line"]   # no warning, no default noise
    # unset QR_STEP_TIMEOUT also runs silently at the default
    r = _run_script(exe)
    assert r.returncode == 0 and "QR_STEP_TIMEOUT" not in r.stdout
    # an empty value behaves like unset: silent 300 s default, all steps run (distinct from
    # both the unset branch above and the warning branch of a malformed value)
    sub = tmp_path / "empty-timeout"
    sub.mkdir()
    exe2, log2 = _fake_hermes(sub)
    r = _run_script(exe2, QR_STEP_TIMEOUT="")
    assert r.returncode == 0
    assert log2.read_text().splitlines() == STEPS
    assert "QR_STEP_TIMEOUT" not in r.stdout and "Traceback" not in r.stderr
