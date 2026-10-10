"""``hermes quota-router export [--since <unix-ts>] [--json]`` (issue #22, t_0e2dbdd4).

Contract: one ``QRJSON`` line on stdout with ``v``/``host``/``samples``; one sample per
retained, well-formed history point whose label is an ACCOUNT window of the provider's
CURRENT snapshot (D1: fail closed on labels missing from the snapshot) and whose
``ts > since`` (strict). No snapshot-age staleness filter (D2): the write-time ``stale``
flag already kept those points out of history. Export is read-only: no refresh, no
queue drain, no state write.
"""
import argparse
import importlib.util
import json
import math
import sys
from pathlib import Path

import pytest

_root = Path(__file__).resolve().parents[1]
NOW = 1_800_000_000.0
HOST = "testhost"


def _load(name, file, monkeypatch):
    spec = importlib.util.spec_from_file_location(name, _root / file)
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, mod)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    core = _load("quota_router_core", "core.py", monkeypatch)
    core.state_path = lambda hermes_home=None: home / "cache" / "quota-router" / "state.json"
    plugin = _load("qr_plugin_export", "__init__.py", monkeypatch)
    return core, plugin


def window(label, used, resets_at=None, scope="account", **kw):
    return {"label": label, "used": used, "resets_at": resets_at, "scope": scope, **kw}


# The current anthropic snapshot: two account windows, a model-family window, and a
# model-name-scoped window. "Ghost label" exists only in history (absent from snapshot).
SNAP_WINDOWS = [window("Current week", 40.0, resets_at=NOW + 3 * 3600),
                window("Current session", 10.0, scope=None),
                window("Opus week", 50.0, scope="model"),
                window("Fable week (model)", 60.0, scope="Fable")]


def write_state(e, st):
    st.setdefault("version", 1)
    p = e.state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st, indent=1, sort_keys=True, default=str))


def base_state():
    return {"version": 1, "providers": {"anthropic": {"snapshot": {"fetched_at": NOW,
                                                                   "windows": SNAP_WINDOWS}}},
            "history": {"anthropic": {"Current week": [[1, 40.0, None]],
                                      "Current session": [[1, 10.0, None]],
                                      "Opus week": [[1, 50.0, None]],
                                      "Fable week (model)": [[1, 60.0, None]],
                                      "Ghost label": [[1, 5.0, None]]},
                        # history but NO snapshot -> exports nothing (D1 fail closed)
                        "openai-codex": {"Weekly": [[1, 20.0, None]]}}}


def run_cli(e, *argv):
    parser = argparse.ArgumentParser()
    e[1]._cli_setup(parser)
    return e[1]._cli_handler(parser.parse_args(list(argv)))


def out_payload(capsys):
    out = capsys.readouterr()
    lines = out.out.splitlines()
    assert len(lines) == 1 and lines[0].startswith("QRJSON ")
    return json.loads(lines[0][len("QRJSON "):]), out


# ── acceptance 1: --since boundary is strict (ts == since excluded) ──────────────
def test_since_boundary_is_strict(env, capsys):
    core, plugin = env
    st = base_state()
    st["history"]["anthropic"]["Current week"] = [[NOW - 1, 30.0, None], [NOW, 40.0, None],
                                                  [NOW + 1, 41.0, None]]
    write_state(core, st)
    assert run_cli(env, "export", "--since", str(NOW)) == 0
    payload, _ = out_payload(capsys)
    assert [(s["provider"], s["window"], s["ts"]) for s in payload["samples"]] == \
        [("anthropic", "Current week", NOW + 1)]


# ── acceptance 2: --since omitted exports every retained account-window point ────
def test_since_omitted_exports_all_account_points(env, capsys):
    core, plugin = env
    st = base_state()
    st["history"] = {"anthropic": {"Current week": [[1, 10.0, None], [2, 20.0, None]],
                                   "Current session": [[3, 11.0, None]]}}
    write_state(core, st)
    assert run_cli(env, "export") == 0
    payload, _ = out_payload(capsys)
    got = sorted((s["provider"], s["window"], s["ts"]) for s in payload["samples"])
    assert got == [("anthropic", "Current session", 3),
                   ("anthropic", "Current week", 1), ("anthropic", "Current week", 2)]


# ── acceptance 3: invalid --since → one stderr line, non-zero exit, no QRJSON ─────
@pytest.mark.parametrize("bad", ["abc", "", "nan", "inf"])
def test_invalid_since_errors_without_payload(env, capsys, bad):
    assert run_cli(env, "export", "--since", bad) != 0
    captured = capsys.readouterr()
    err = captured.err.splitlines()
    assert len(err) == 1 and err[0].startswith("quota-router:")
    assert "QRJSON" not in captured.out


# ── acceptance 4: model-family windows excluded, account windows kept ─────────────
def test_model_family_windows_excluded(env, capsys):
    core, plugin = env
    st = base_state()
    st["history"] = {"anthropic": {"Current week": [[1, 40.0, None]],
                                   "Current session": [[1, 10.0, None]],
                                   "Opus week": [[1, 50.0, None]],
                                   "Fable week (model)": [[1, 60.0, None]]}}
    write_state(core, st)
    assert run_cli(env, "export") == 0
    payload, _ = out_payload(capsys)
    wins = {s["window"] for s in payload["samples"] if s["provider"] == "anthropic"}
    assert "Opus week" not in wins and "Fable week (model)" not in wins
    assert {"Current week", "Current session"} <= wins   # scope "account" AND scope None


def test_payload_core_function_applies_d1(env):
    core, _ = env
    payload = core.export_payload(base_state(), -math.inf, HOST)
    wins = {(s["provider"], s["window"]) for s in payload["samples"]}
    assert ("anthropic", "Current week") in wins and ("anthropic", "Current session") in wins
    assert ("anthropic", "Opus week") not in wins
    assert ("anthropic", "Fable week (model)") not in wins
    assert ("anthropic", "Ghost label") not in wins
    assert ("openai-codex", "Weekly") not in wins   # no snapshot -> nothing (fail closed)


# ── acceptance 5: labels missing from the snapshot / provider without snapshot ────
def test_label_missing_from_snapshot_and_snapshotless_provider_excluded(env, capsys):
    assert run_cli(env, "export") == 0
    payload, _ = out_payload(capsys)
    provs = {s["provider"] for s in payload["samples"]}
    assert "openai-codex" not in provs
    assert "Ghost label" not in {s["window"] for s in payload["samples"]}


# ── acceptance 6: stale ───────────────────────────────────────────────────────────
def test_stale_window_folded_through_append_history_is_absent(env):
    core, _ = env
    st = base_state()
    st["history"] = {}                                  # isolate: only this fold's points
    snap = {"fetched_at": NOW, "windows": [window("Current week", 80.0, stale=True)]}
    core.append_history(st, "anthropic", snap, NOW)
    payload = core.export_payload(st, -math.inf, HOST)
    assert payload["samples"] == []
    write_state(core, st)
    payload = core.export_payload(core.read_state(core.state_path()), -math.inf, HOST)
    assert payload["samples"] == []


def test_old_snapshot_points_still_exported_no_age_filter(env):
    core, _ = env
    st = base_state()
    stale_after = core.DEFAULTS["stale_after_s"]
    st["providers"]["anthropic"]["snapshot"]["fetched_at"] = NOW - 10 * stale_after
    write_state(core, st)
    payload = core.export_payload(core.read_state(core.state_path()), -math.inf, HOST)
    assert any(s["provider"] == "anthropic" for s in payload["samples"])


# ── acceptance 7: empty history ───────────────────────────────────────────────────
@pytest.mark.parametrize("hist", [None, {}, {"anthropic": {}}])
def test_empty_history_yields_empty_samples(env, capsys, hist):
    core, plugin = env
    st = base_state()
    if hist is None:
        st.pop("history")
    else:
        st["history"] = hist
    write_state(core, st)
    assert run_cli(env, "export") == 0
    payload, _ = out_payload(capsys)
    assert payload["samples"] == [] and payload["v"] == 1 and payload["host"]


# ── acceptance 8: schema ──────────────────────────────────────────────────────────
def test_schema_one_line_keys(env, capsys):
    assert run_cli(env, "export", "--json") == 0     # --json accepted; same output
    payload, out = out_payload(capsys)
    assert payload["v"] == 1 and payload["host"]
    for s in payload["samples"]:
        assert set(s) == {"ts", "provider", "window", "used_pct", "resets_at"}


def test_resets_at_number_or_null(env):
    core, _ = env
    st = base_state()
    st["history"]["anthropic"]["Current week"] = [[1, 40.0, 123.5], [2, 41.0, None]]
    payload = core.export_payload(st, -math.inf, HOST)
    assert {s["resets_at"] for s in payload["samples"]} == {123.5, None}


# ── acceptance 9: read-only ───────────────────────────────────────────────────────
def test_export_is_read_only(env, capsys, monkeypatch):
    core, plugin = env
    write_state(core, base_state())
    path = core.state_path()
    with core.locked_state(path) as st:            # seed a pending line the export must keep
        core.queue(st, "⚠️ unrelated threshold alert")
    before_bytes = path.read_bytes()
    before_mtime = path.stat().st_mtime_ns
    before_pending = core.read_state(path)["pending"]

    def _boom(*a, **k):
        raise AssertionError("export must not fetch or refresh")

    monkeypatch.setattr(plugin, "refresh", _boom)
    # The import inside refresh() is the fetch path; block it too, so a regression that starts
    # fetching cannot pass by importing fresh code.
    import builtins
    real_import = builtins.__import__

    def _no_fetch_import(name, *a, **k):
        if name == "agent.account_usage":
            raise AssertionError("export must not fetch or refresh")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _no_fetch_import)

    assert run_cli(env, "export") == 0
    out_payload(capsys)
    assert path.read_bytes() == before_bytes
    assert path.stat().st_mtime_ns == before_mtime
    assert core.read_state(path)["pending"] == before_pending


# ── acceptance 10: malformed containers/points are skipped, never raised ──────────
def test_malformed_skipped_not_raised(env):
    core, _ = env
    st = base_state()
    st["history"]["anthropic"] = {
        "Current week": [["x", 1, None], [1, 2], [1, 2, 3, 4], [True, 1, None],
                         [NOW, 42.0, None]],          # only the last one is valid
        "Current session": "not-a-list",
        "Opus week": [[NOW, 50.0, None]],             # snapshot has it but scope model → skipped
    }
    st["history"]["broken-provider"] = "not-a-dict"
    payload = core.export_payload(st, -math.inf, HOST)
    assert [(s["window"], s["ts"], s["used_pct"]) for s in payload["samples"]] == \
        [("Current week", NOW, 42.0)]


# ── regression: a non-hashable snapshot window label must not crash export ────────
# A corrupted/hand-edited state.json can carry a list or dict as a window's label.
# The contract says malformed containers are skipped, never raised (no TypeError).
@pytest.mark.parametrize("bad_label", [["bad", "label"], {"k": 1}, 42, None])
def test_unhashable_or_non_str_window_label_skipped(env, bad_label):
    core, _ = env
    st = base_state()
    st["providers"]["anthropic"]["snapshot"]["windows"].append(
        {"label": bad_label, "used": 1.0, "scope": "account", "resets_at": None})
    # the corrupt window contributes no account label; the well-formed ones still export
    payload = core.export_payload(st, -math.inf, HOST)
    assert {s["window"] for s in payload["samples"]} == \
        {"Current week", "Current session"}


def test_snapshot_window_without_label_skipped(env):
    core, _ = env
    st = base_state()
    st["providers"]["anthropic"]["snapshot"]["windows"].insert(
        0, {"used": 1.0, "scope": "account"})          # no "label" key at all
    st["providers"]["anthropic"]["snapshot"]["windows"].append(
        {"used": 2.0, "scope": "account"})             # non-dict entry entirely
    st["providers"]["anthropic"]["snapshot"]["windows"].append("garbage")
    payload = core.export_payload(st, -math.inf, HOST)
    assert {s["window"] for s in payload["samples"]} == \
        {"Current week", "Current session"}


# ── public entry point: export_line(since=...) is the single QRJSON builder ───────
def test_export_line_public_entry_point(env, capsys):
    core, plugin = env
    st = base_state()
    write_state(core, st)
    line_default = plugin.export_line()
    line_since = plugin.export_line(since=NOW - 1)
    assert line_default.startswith("QRJSON {") and line_since.startswith("QRJSON {")
    assert json.loads(line_default[7:])["samples"] == [
        {"ts": 1, "provider": "anthropic", "window": "Current session", "used_pct": 10.0,
         "resets_at": None},
        {"ts": 1, "provider": "anthropic", "window": "Current week", "used_pct": 40.0,
         "resets_at": None},
    ]


# ── acceptance 11 is the full suite; here: payload purity (no state mutation) ─────
def test_export_payload_does_not_mutate_state(env):
    core, _ = env
    st = base_state()
    snapshot = json.dumps(st, sort_keys=True, default=str)
    core.export_payload(st, -math.inf, HOST)
    assert json.dumps(st, sort_keys=True, default=str) == snapshot
