"""throttle apply / apply-one (plan Task 4; binding v2 B1, B2, B3, shadow mode, root kanban key).

Every test runs against a temp HERMES_HOME. The real state file's mtime is asserted unchanged.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

_root = Path(__file__).resolve().parents[1]
REAL_STATE = Path.home() / ".hermes" / "cache" / "quota-router" / "state.json"
H = 3600.0
M = 60.0
NOW = 1_800_000_000.0

LEVELS = {
    "fast": {"provider": "anthropic", "model": "opus-x",
             "fallback": [{"provider": "custom:internal", "model": "glm-x"},
                          {"provider": "openai-codex", "model": "codex-x"}]},
    "constrained": {"provider": "custom:internal", "model": "glm-x",
                    "fallback": [{"provider": "openai-codex", "model": "codex-x"}]},
}
KANBAN_LEVELS = {"fast": {**LEVELS["fast"], "max_in_progress": 64},
                 "constrained": {**LEVELS["constrained"], "max_in_progress": 24}}

PROFILE_YAML = """\
# worker profile
model:
  default: opus-x   # primary
  provider: anthropic
display:
  skin: example
fallback_providers:
  - provider: custom:internal
    model: glm-x
  - provider: openai-codex
    model: codex-x
kanban:
  dispatch_in_gateway: false
"""
ROOT_YAML = """\
model:
  default: opus-x
  provider: anthropic
kanban:
  dispatch_in_gateway: true
  max_in_progress: 64   # host cap
  max_in_progress_per_profile: 16
"""


def _load(name, file, monkeypatch):
    spec = importlib.util.spec_from_file_location(name, _root / file)
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, mod)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def env(tmp_path, monkeypatch):
    real_mtime = REAL_STATE.stat().st_mtime if REAL_STATE.exists() else None
    home = tmp_path / ".hermes"
    (home / "profiles" / "worker").mkdir(parents=True)
    (home / "profiles" / "worker" / "config.yaml").write_text(PROFILE_YAML)
    (home / "config.yaml").write_text(ROOT_YAML)
    monkeypatch.setenv("HERMES_HOME", str(home))
    core = _load("quota_router_core", "core.py", monkeypatch)
    core.state_path = lambda hermes_home=None: home / "cache" / "quota-router" / "state.json"
    thr = _load("quota_router_throttle", "throttle.py", monkeypatch)
    plugin = _load("qr_plugin_throttlecase", "__init__.py", monkeypatch)

    class Env:
        pass

    e = Env()
    e.home, e.core, e.thr, e.plugin = home, core, thr, plugin
    e.profile_cfg = home / "profiles" / "worker" / "config.yaml"
    e.root_cfg = home / "config.yaml"
    e.calls = []

    def runner(profile, argv):
        """In-process stand-in for `hermes -p <profile> quota-router throttle apply-one ...`."""
        e.calls.append((profile, list(argv)))
        path = e.root_cfg if profile == "default" else home / "profiles" / profile / "config.yaml"
        args = thr.parse_apply_one_argv(argv)
        res = thr.apply_one(path, args["level"], args["levels"], kanban=args["kanban"],
                            backup_dir=home / "backups" / "quota-throttle", label=profile, now=e.now)
        return 0, thr.RESULT_PREFIX + json.dumps(res) + "\n", ""

    e.runner = runner
    e.now = NOW
    yield e
    if real_mtime is not None:
        assert REAL_STATE.stat().st_mtime == real_mtime, "real state file was touched"


def tcfg(e, **over):
    return e.core.throttle_config({"levels": LEVELS, "profiles": ["worker"], "enabled": True, **over})


def feed(e, used, now):
    e.now = now
    e.plugin.refresh(now=now, snapshots={"anthropic": {"fetched_at": now, "windows": [
        {"label": "Current session", "used_percent": used, "resets_at": now + 3 * H}]}}, drain=False)


def apply(e, t=None, now=None):
    e.now = now if now is not None else e.now
    return e.thr.run_apply(e.core.state_path(), dict(e.core.DEFAULTS), t or tcfg(e), e.runner, e.now)


def profile(e):
    return e.thr.read_raw(e.profile_cfg)


def state(e):
    return e.core.read_state(e.core.state_path())


# ── apply-one (child) ──────────────────────────────────────────────────────────

def test_apply_one_classifies_and_writes_only_when_needed(env):
    bdir = env.home / "backups" / "qt-test"
    r = env.thr.apply_one(env.profile_cfg, "fast", LEVELS, backup_dir=bdir, label="worker", now=NOW)
    assert r["result"] == "noop" and r["actual"] == "fast"
    assert not bdir.exists()
    r = env.thr.apply_one(env.profile_cfg, "constrained", LEVELS, backup_dir=bdir, label="worker", now=NOW)
    assert r["result"] == "applied" and r["actual"] == "fast"
    raw = profile(env)
    assert raw["model"] == {"default": "glm-x", "provider": "custom:internal"}
    assert raw["fallback_providers"] == [{"provider": "openai-codex", "model": "codex-x"}]
    backups = list(bdir.iterdir())
    assert len(backups) == 1 and backups[0].read_text() == PROFILE_YAML
    text = env.profile_cfg.read_text()
    assert "# worker profile" in text and "skin: example" in text
    r = env.thr.apply_one(env.profile_cfg, "constrained", LEVELS, backup_dir=bdir, label="worker", now=NOW)
    assert r["result"] == "noop"


def test_apply_one_compares_parsed_values_not_bytes(env):
    env.profile_cfg.write_text(PROFILE_YAML.replace("custom:internal", "Custom:Internal")
                               .replace("opus-x   # primary", "'opus-x'"))
    r = env.thr.apply_one(env.profile_cfg, "fast", LEVELS, backup_dir=env.home / "b", label="w", now=NOW)
    assert r["result"] == "noop"


def test_apply_one_skips_a_hand_edit(env):
    env.profile_cfg.write_text(PROFILE_YAML.replace("default: opus-x", "default: something-else"))
    before = env.profile_cfg.read_text()
    r = env.thr.apply_one(env.profile_cfg, "constrained", LEVELS, backup_dir=env.home / "b", label="w", now=NOW)
    assert r["result"] == "drift" and r["actual"] is None
    assert env.profile_cfg.read_text() == before


def test_apply_one_refuses_a_model_block_with_its_own_route(env):
    env.profile_cfg.write_text(PROFILE_YAML.replace("  provider: anthropic\n",
                                                    "  provider: anthropic\n  base_url: https://example.invalid\n"))
    r = env.thr.apply_one(env.profile_cfg, "constrained", LEVELS, backup_dir=env.home / "b", label="w", now=NOW)
    assert r["result"] == "unsupported"


def test_apply_one_refuses_a_fallback_entry_with_extra_keys(env):
    yaml = PROFILE_YAML.replace("    model: glm-x\n", "    model: glm-x\n    base_url: https://example.invalid\n")
    env.profile_cfg.write_text(yaml)
    r = env.thr.apply_one(env.profile_cfg, "constrained", LEVELS, backup_dir=env.home / "b", label="w", now=NOW)
    assert r["result"] == "unsupported" and r["actual"] == "fast"
    assert "fallback_providers[0].base_url" in r["message"]
    assert env.profile_cfg.read_text() == yaml


def test_apply_one_refuses_invalid_levels(env):
    leaky = {**LEVELS, "constrained": {**LEVELS["constrained"],
                                       "fallback": [{"provider": "anthropic", "model": "opus-x"}]}}
    r = env.thr.apply_one(env.profile_cfg, "constrained", leaky, backup_dir=env.home / "b", label="w", now=NOW)
    assert r["result"] == "error" and "anthropic" in r["message"]
    assert env.profile_cfg.read_text() == PROFILE_YAML


def test_apply_one_kanban_key_on_root_config(env):
    bdir = env.home / "b"
    r = env.thr.apply_one(env.root_cfg, "constrained", KANBAN_LEVELS, kanban=True, backup_dir=bdir,
                          label="default", now=NOW)
    assert r["result"] == "applied"
    raw = env.thr.read_raw(env.root_cfg)
    assert raw["kanban"]["max_in_progress"] == 24
    assert raw["kanban"]["max_in_progress_per_profile"] == 16
    assert raw["model"] == {"default": "opus-x", "provider": "anthropic"}  # model keys untouched
    assert "# host cap" in env.root_cfg.read_text() or "max_in_progress: 24" in env.root_cfg.read_text()
    env.root_cfg.write_text(ROOT_YAML.replace("max_in_progress: 64", "max_in_progress: 40"))
    r = env.thr.apply_one(env.root_cfg, "fast", KANBAN_LEVELS, kanban=True, backup_dir=bdir, label="default",
                          now=NOW)
    assert r["result"] == "drift"


def test_apply_one_crash_before_replace_leaves_the_file_intact(env, monkeypatch):
    import hermes_cli.config as hc

    def boom(path, data, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(hc, "atomic_config_write", boom)
    r = env.thr.apply_one(env.profile_cfg, "constrained", LEVELS, backup_dir=env.home / "b", label="w", now=NOW)
    assert r["result"] == "error"
    assert env.profile_cfg.read_text() == PROFILE_YAML


# ── apply (orchestrator) ───────────────────────────────────────────────────────

def test_shadow_mode_computes_and_logs_but_never_writes(env):
    feed(env, 90, NOW)
    lines = apply(env, tcfg(env, enabled=False))
    assert env.calls == []
    assert profile(env)["model"]["provider"] == "anthropic"
    assert any(l.startswith("quota-router: throttle (shadow) fast → constrained") for l in lines)
    st = state(env)
    assert st["throttle"]["level"] == "constrained" and st["throttle"]["log"]


def test_enabled_apply_writes_each_profile_once_and_alerts_once(env):
    feed(env, 90, NOW)
    lines = apply(env)
    assert [c[0] for c in env.calls] == ["worker"]
    assert profile(env)["model"]["provider"] == "custom:internal"
    assert len([l for l in lines if "throttle" in l]) == 1
    lines = apply(env, now=NOW + 60)
    assert lines == []
    assert state(env)["throttle"]["applied"]["worker"]["result"] == "noop"


def test_sync_overwrite_during_constrained_is_reapplied_with_one_alert(env):
    feed(env, 90, NOW)
    apply(env)
    env.profile_cfg.write_text(PROFILE_YAML)  # e.g. a profile sync rewrote the fast spec
    lines = apply(env, now=NOW + 300)
    assert profile(env)["model"]["provider"] == "custom:internal"
    assert [l for l in lines if "re-applied" in l] == [
        "quota-router: throttle re-applied constrained to worker after external overwrite"]
    assert apply(env, now=NOW + 600) == []


def test_hand_edit_is_skipped_and_alerted_once_per_profile_and_level(env):
    feed(env, 90, NOW)
    env.profile_cfg.write_text(PROFILE_YAML.replace("default: opus-x", "default: hand-picked"))
    lines = apply(env)
    assert [l for l in lines if "skipped" in l] == [
        "quota-router: throttle skipped worker (manual model change)"]
    assert apply(env, now=NOW + 300) == []
    assert profile(env)["model"]["default"] == "hand-picked"


def test_crash_mid_apply_reconciles_on_the_next_run(env):
    feed(env, 90, NOW)

    def dying(profile, argv):
        raise KeyboardInterrupt  # process killed after the level was recorded, before any write

    with pytest.raises(KeyboardInterrupt):
        env.thr.run_apply(env.core.state_path(), dict(env.core.DEFAULTS), tcfg(env), dying, NOW)
    assert state(env)["throttle"]["level"] == "constrained"
    assert profile(env)["model"]["provider"] == "anthropic"
    lines = apply(env, now=NOW + 300)
    assert profile(env)["model"]["provider"] == "custom:internal"
    assert not any("re-applied" in l for l in lines)  # our own unfinished write, not an overwrite


def test_runner_failure_is_recorded_alerted_once_and_retried(env):
    feed(env, 90, NOW)

    def failing(profile, argv):
        return 1, "", "boom"

    t = tcfg(env)
    lines = env.thr.run_apply(env.core.state_path(), dict(env.core.DEFAULTS), t, failing, NOW)
    assert [l for l in lines if "failed" in l] == ["quota-router: throttle apply to worker failed (exit 1: boom)"]
    assert env.thr.run_apply(env.core.state_path(), dict(env.core.DEFAULTS), t, failing, NOW + 60) == []
    apply(env, now=NOW + 120)
    assert profile(env)["model"]["provider"] == "custom:internal"


@pytest.mark.parametrize("exc", [subprocess.TimeoutExpired(["hermes"], 120),
                                 FileNotFoundError(2, "No such file or directory")])
def test_runner_exception_is_an_error_for_that_profile_and_the_rest_still_apply(env, exc):
    feed(env, 90, NOW)
    tried = []

    def flaky(profile, argv):
        tried.append(profile)
        if profile == "worker":
            raise exc
        return env.runner(profile, argv)

    t = tcfg(env, levels=KANBAN_LEVELS)
    lines = env.thr.run_apply(env.core.state_path(), dict(env.core.DEFAULTS), t, flaky, NOW)
    assert tried == ["worker", "default"]
    assert env.thr.read_raw(env.root_cfg)["kanban"]["max_in_progress"] == 24
    rec = state(env)["throttle"]["applied"]["worker"]
    assert rec["result"] == "error" and type(exc).__name__ in rec["message"]
    assert [l for l in lines if "failed" in l] == [
        f"quota-router: throttle apply to worker failed ({rec['message']})"]
    assert env.thr.run_apply(env.core.state_path(), dict(env.core.DEFAULTS), t, flaky, NOW + 60) == []


def test_invalid_levels_block_writes_with_one_alert(env):
    feed(env, 90, NOW)
    leaky = {**LEVELS, "constrained": {**LEVELS["constrained"],
                                       "fallback": [{"provider": "anthropic", "model": "opus-x"}]}}
    lines = apply(env, tcfg(env, levels=leaky))
    assert env.calls == []
    assert len([l for l in lines if "config invalid" in l]) == 1
    assert apply(env, tcfg(env, levels=leaky), now=NOW + 60) == []


def test_kanban_key_is_managed_only_when_both_levels_set_it(env):
    feed(env, 90, NOW)
    apply(env)
    assert [c[0] for c in env.calls] == ["worker"]
    assert env.thr.read_raw(env.root_cfg)["kanban"]["max_in_progress"] == 64
    env.calls.clear()
    apply(env, tcfg(env, levels=KANBAN_LEVELS), now=NOW + 60)
    assert [c[0] for c in env.calls] == ["worker", "default"]
    assert "--kanban" in env.calls[1][1]
    assert env.thr.read_raw(env.root_cfg)["kanban"]["max_in_progress"] == 24


def test_hold_and_disable_drive_apply(env):
    feed(env, 90, NOW)
    apply(env)
    with env.core.locked_state(env.core.state_path()) as st:
        env.core.throttle_disable(st, "fast", NOW + 60)
    apply(env, now=NOW + 60)
    assert profile(env)["model"]["provider"] == "anthropic"
    with env.core.locked_state(env.core.state_path()) as st:
        env.core.throttle_hold(st, "constrained", NOW + 2 * H, NOW + 120)
    apply(env, now=NOW + 120)
    assert profile(env)["model"]["provider"] == "custom:internal"
    apply(env, now=NOW + 2 * H + 1)   # hold expired: disable (fast) wins again
    assert profile(env)["model"]["provider"] == "anthropic"


def test_concurrent_refresh_and_apply_lose_nothing(env):
    feed(env, 90, NOW)
    errors = []

    def refresher():
        try:
            for i in range(20):
                t = NOW + 1 + i
                env.plugin.refresh(now=t, snapshots={"anthropic": {"fetched_at": t, "windows": [
                    {"label": "Current session", "used_percent": 90, "resets_at": NOW + 3 * H}]}}, drain=False)
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    def applier():
        try:
            for i in range(20):
                env.thr.run_apply(env.core.state_path(), dict(env.core.DEFAULTS), tcfg(env), env.runner, NOW + 1 + i)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=refresher), threading.Thread(target=applier)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert errors == []
    st = state(env)
    assert len(st["history"]["anthropic"]["Current session"]) == 21
    assert len(st["throttle"]["log"]) == 20
    assert profile(env)["model"]["provider"] == "custom:internal"


def test_status_reports_level_profiles_and_leak_count(env):
    import sqlite3
    feed(env, 90, NOW)
    apply(env)
    db = env.home / "profiles" / "worker" / "state.db"
    con = sqlite3.connect(db)
    con.execute("create table sessions (id text, started_at real, billing_provider text, model text)")
    con.executemany("insert into sessions values (?,?,?,?)", [
        ("a", NOW - 10, "anthropic", "opus-x"),       # before constrained: not a leak
        ("b", NOW + 10, "custom", "glm-x"),
        ("c", NOW + 20, "anthropic", "opus-x")])      # leak
    con.commit()
    con.close()
    out = env.thr.throttle_status(state(env), tcfg(env), env.home, NOW + 30)
    assert out["level"] == "constrained" and out["enabled"] is True
    assert out["profiles"]["worker"]["result"] == "applied"
    assert out["profiles"]["worker"]["leaked_sessions"] == 1
    text = env.thr.render_throttle_status(out)
    assert "constrained" in text and "worker" in text and "leak" in text


@pytest.mark.parametrize("allowance", [0, 100])
def test_status_explains_admitted_banked_weekly_allowance(env, allowance):
    status = {"configured": True, "enabled": True, "level": "fast", "since": NOW,
              "source": "policy", "profiles": {}, "last": {"windows": {"Weekly": {
                  "used": 31.0, "projected": 200.0, "projected_short": 150.0,
                  "r_long": 2.0, "r_short": 5.0,
                  **({"projection_allowance": allowance} if allowance else {}),
              }}}}
    text = env.thr.render_throttle_status(status)
    assert ("banked weekly allowance +100%" in text) is bool(allowance)
    assert "31.0%" in text and "200% (short 150%)" in text


def _cli(e, monkeypatch, t, *argv):
    import argparse
    parser = argparse.ArgumentParser()
    e.plugin._cli_setup(parser)
    args = parser.parse_args(list(argv))
    monkeypatch.setattr(e.plugin, "_throttle_cfg", lambda: t)
    monkeypatch.setattr(e.thr, "subprocess_runner", e.runner)
    return e.plugin._cli_handler(args)


def test_cli_hold_disable_and_status(env, monkeypatch, capsys):
    t = tcfg(env)
    feed(env, 10, NOW)
    env.now = NOW
    monkeypatch.setattr(env.plugin.time, "time", lambda: env.now)
    assert _cli(env, monkeypatch, t, "throttle", "hold", "constrained", "--for", "2h") == 0
    assert state(env)["throttle"]["hold"]["until"] == NOW + 2 * H
    assert profile(env)["model"]["provider"] == "custom:internal"   # hold applies at once
    assert _cli(env, monkeypatch, t, "throttle", "hold", "--clear") == 0
    assert "hold" not in state(env)["throttle"]
    assert _cli(env, monkeypatch, t, "throttle", "disable") == 0
    assert state(env)["throttle"]["disabled"]["level"] == "fast"
    assert profile(env)["model"]["provider"] == "anthropic"
    assert _cli(env, monkeypatch, t, "throttle", "disable", "--clear") == 0
    assert "disabled" not in state(env)["throttle"]
    capsys.readouterr()
    assert _cli(env, monkeypatch, t, "throttle", "status", "--json") == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith("QRTHROTTLE_STATUS ") and json.loads(out.split(" ", 1)[1])["level"] == "fast"
    assert _cli(env, monkeypatch, t, "throttle", "hold", "sideways", "--for", "1h") == 2


def test_cli_hold_and_disable_leave_the_pending_queue_for_the_cron(env, monkeypatch, capsys):
    t = tcfg(env)
    feed(env, 10, NOW)
    monkeypatch.setattr(env.plugin.time, "time", lambda: NOW)
    with env.core.locked_state(env.core.state_path()) as st:
        env.core.queue(st, "⚠️ unrelated threshold alert")
    capsys.readouterr()
    assert _cli(env, monkeypatch, t, "throttle", "hold", "constrained", "--for", "1h") == 0
    assert _cli(env, monkeypatch, t, "throttle", "disable") == 0
    assert "unrelated" not in capsys.readouterr().out
    assert "⚠️ unrelated threshold alert" in state(env)["pending"]
    assert profile(env)["model"]["provider"] == "custom:internal"   # applied at once; hold beats disable


def test_cli_apply_one_prints_one_result_line(env, monkeypatch, capsys):
    argv = env.thr.apply_one_argv("constrained", LEVELS, kanban=False)
    monkeypatch.setenv("HERMES_HOME", str(env.home / "profiles" / "worker"))
    import argparse
    parser = argparse.ArgumentParser()
    env.plugin._cli_setup(parser)
    assert env.plugin._cli_handler(parser.parse_args(argv)) == 0
    res = env.thr.parse_result(capsys.readouterr().out)
    assert res["result"] == "applied"
    assert profile(env)["model"]["provider"] == "custom:internal"


def test_cli_parses_hold_duration():
    thr_spec = importlib.util.spec_from_file_location("qr_thr_dur", _root / "throttle.py")
    thr = importlib.util.module_from_spec(thr_spec)
    thr_spec.loader.exec_module(thr)
    assert thr.parse_duration("2h") == 7200
    assert thr.parse_duration("90m") == 5400
    assert thr.parse_duration("45s") == 45
    with pytest.raises(ValueError):
        thr.parse_duration("soon")


# ── B3 round trip against a copy of a real profile config ─────────────────────

REAL_PROFILE = Path(os.environ.get("QR_REAL_PROFILE_CONFIG",
                                   str(Path.home() / ".hermes" / "profiles" / "builder" / "config.yaml")))


@pytest.mark.skipif(not REAL_PROFILE.exists(), reason="no real profile config to copy")
def test_atomic_write_round_trip_on_a_copy_of_a_real_profile_config(env):
    real_mtime = REAL_PROFILE.stat().st_mtime
    copy = env.home / "profiles" / "copy" / "config.yaml"
    copy.parent.mkdir(parents=True)
    shutil.copy2(REAL_PROFILE, copy)
    copy.write_text("# comment kept by the round trip\n" + copy.read_text())
    before = env.thr.read_raw(copy)
    actual = env.thr.actual_spec(before)
    other = {"provider": "custom:round-trip", "model": "rt-model",
             "fallback": [{"provider": "openai-codex", "model": "rt-codex"}]}
    levels = {"fast": {"provider": actual["provider"], "model": actual["model"],
                       "fallback": [{"provider": p, "model": m} for p, m in actual["fallback"]]},
              "constrained": other}
    r = env.thr.apply_one(copy, "constrained", levels, backup_dir=env.home / "b", label="copy", now=NOW)
    assert r["result"] == "applied", r
    after = env.thr.read_raw(copy)
    assert list(after) == list(before)                        # top-level order preserved
    assert list(after["model"]) == list(before["model"])
    changed = {k for k in before if before[k] != after[k]}
    assert changed == {"model", "fallback_providers"}
    assert {k for k in before["model"] if before["model"][k] != after["model"][k]} == {"default", "provider"}
    assert after["model"]["provider"] == "custom:round-trip" and after["model"]["default"] == "rt-model"
    assert after["fallback_providers"] == [{"provider": "openai-codex", "model": "rt-codex"}]
    assert copy.read_text().startswith("# comment kept by the round trip\n")
    assert REAL_PROFILE.stat().st_mtime == real_mtime


# ── live: the real `hermes -p <p>` CLI against a temp HERMES_HOME ──────────────

HERMES_BIN = Path(os.environ.get("QR_HERMES_BIN", str(Path.home() / ".local" / "bin" / "hermes")))


@pytest.mark.skipif(not HERMES_BIN.exists() or os.environ.get("QR_SKIP_LIVE") == "1",
                    reason="no hermes CLI")
def test_live_apply_one_under_hermes_p_writes_the_profile_config(env):
    worker = env.home / "profiles" / "worker"
    (worker / "plugins").mkdir()
    (worker / "plugins" / "quota-router").symlink_to(_root, target_is_directory=True)
    env.profile_cfg.write_text(PROFILE_YAML + "plugins:\n  enabled:\n    - quota-router\n")
    child_env = {k: v for k, v in os.environ.items() if not k.startswith("HERMES_")}
    child_env["HERMES_HOME"] = str(env.home)
    argv = env.thr.apply_one_argv("constrained", LEVELS, kanban=False)
    r = subprocess.run([str(HERMES_BIN), "-p", "worker", "quota-router", *argv],
                       capture_output=True, text=True, timeout=120, env=child_env)
    res = env.thr.parse_result(r.stdout)
    assert r.returncode == 0 and res and res["result"] == "applied", (r.stdout, r.stderr[-2000:])
    raw = env.thr.read_raw(env.profile_cfg)
    assert raw["model"] == {"default": "glm-x", "provider": "custom:internal"}
    assert raw["fallback_providers"] == [{"provider": "openai-codex", "model": "codex-x"}]
    assert raw["display"] == {"skin": "example"}
    assert list((env.home / "backups" / "quota-throttle").iterdir())
