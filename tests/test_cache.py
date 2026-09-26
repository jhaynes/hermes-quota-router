"""Cache-efficiency collection, alerts and surfaces. Run: python -m pytest -q tests/

Every test runs against a fixture SQLite DB that has the real ``sessions`` columns; nothing here
opens the real ``state.db`` or the real ``state.json``.
"""
import importlib.util
import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

_root = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("qr_core_cache", _root / "core.py")
core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(core)
CFG = dict(core.DEFAULTS)
H = 3600.0
NOW = 1_800_000_000.0
M = 1_000_000

# Column list copied from Hermes' state.db ``sessions`` table (hermes_state schema).
SESSIONS_DDL = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, source TEXT NOT NULL, user_id TEXT, session_key TEXT, chat_id TEXT,
    chat_type TEXT, thread_id TEXT, display_name TEXT, origin_json TEXT,
    expiry_finalized INTEGER DEFAULT 0, model TEXT, model_config TEXT, system_prompt TEXT,
    system_prompt_hash TEXT, parent_session_id TEXT, started_at REAL NOT NULL, ended_at REAL,
    end_reason TEXT, message_count INTEGER DEFAULT 0, tool_call_count INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0, cache_write_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0, cwd TEXT, git_branch TEXT, git_repo_root TEXT,
    git_metadata_generation INTEGER NOT NULL DEFAULT 0, billing_provider TEXT,
    billing_base_url TEXT, billing_mode TEXT, estimated_cost_usd REAL, actual_cost_usd REAL,
    cost_status TEXT, cost_source TEXT, pricing_version TEXT, title TEXT, title_source TEXT,
    last_activity_at REAL, last_activity_description TEXT, last_activity_provenance TEXT,
    api_call_count INTEGER DEFAULT 0, handoff_state TEXT, handoff_platform TEXT,
    handoff_error TEXT, compression_failure_cooldown_until REAL, compression_failure_error TEXT,
    compression_fallback_streak INTEGER NOT NULL DEFAULT 0,
    compression_ineffective_count INTEGER NOT NULL DEFAULT 0, profile_name TEXT,
    rewind_count INTEGER NOT NULL DEFAULT 0, archived INTEGER NOT NULL DEFAULT 0,
    pinned INTEGER NOT NULL DEFAULT 0, hidden INTEGER NOT NULL DEFAULT 0, last_read_at REAL,
    "compression_recovery_deadline" REAL, "transport_profile" TEXT, "tool_names" TEXT
);
"""


def sess(sid, provider, source, inp, read, write, *, out=0, last=NOW - H, started=None,
         model="m1", title=None):
    return {"id": sid, "billing_provider": provider, "source": source, "input_tokens": inp,
            "cache_read_tokens": read, "cache_write_tokens": write, "output_tokens": out,
            "last_activity_at": last, "started_at": started if started is not None else (last or NOW) - H,
            "model": model, "title": title}


def make_db(path: Path, rows) -> Path:
    con = sqlite3.connect(path)
    con.executescript(SESSIONS_DDL)
    for r in rows:
        cols = ", ".join(r)
        con.execute(f"INSERT INTO sessions ({cols}) VALUES ({', '.join('?' * len(r))})", list(r.values()))
    con.commit()
    con.close()
    return path


def collect(path, now=NOW, cfg=CFG):
    ok, kind, message, data = core.collect_cache_outcome(path, now, cfg)
    assert ok, (kind, message)
    return data


# ── provider mapping / metric ──────────────────────────────────────────────────

def test_provider_mapping():
    assert core.cache_provider("anthropic") == "anthropic"
    assert core.cache_provider(" Anthropic ") == "anthropic"
    assert core.cache_provider("openai-codex") == "openai-codex"
    for other in ("openai", "copilot", "custom:x", "nous", "", None):
        assert core.cache_provider(other) == "other"


def test_hit_pct_is_cache_read_over_all_input():
    assert core.hit_pct(0, 0, 0) is None
    assert core.hit_pct(100, 900, 0) == 90.0
    assert core.hit_pct(50, 50, 100) == 25.0


# ── aggregate query ────────────────────────────────────────────────────────────

def test_aggregates_per_provider_and_source_per_window(tmp_path):
    db = make_db(tmp_path / "state.db", [
        sess("a1", "anthropic", "desktop", 100, 900, 0, last=NOW - 1 * H),
        sess("a2", "anthropic", "kanban", 50, 50, 100, last=NOW - 2 * H),
        sess("a3", "anthropic", "desktop", 1000, 0, 0, last=NOW - 72 * H),
        # long session: started 10 days ago but active in the last day -> counts in 24h
        sess("a4", "anthropic", "cron", 10, 90, 0, last=NOW - 3 * H, started=NOW - 240 * H),
        sess("c1", "openai-codex", "cli", 700, 300, 0),
        sess("o1", "copilot", "kanban", 10, 10, 0),
        # no last_activity_at: falls back to started_at
        sess("o2", "custom:x", "tool", 5, 5, 0, last=None, started=NOW - 2 * H),
        # outside both windows
        sess("old", "anthropic", "desktop", 9 * M, 0, 0, last=NOW - 8 * 24 * H),
    ])
    data = collect(db)
    assert set(data["providers"]) == set(core.CACHE_PROVIDERS)
    a24 = data["providers"]["anthropic"]["24h"]
    assert (a24["sessions"], a24["input"], a24["cache_read"], a24["cache_write"]) == (3, 160, 1040, 100)
    assert a24["hit"] == core.hit_pct(160, 1040, 100)
    assert a24["sources"]["desktop"]["hit"] == 90.0
    assert a24["sources"]["kanban"]["hit"] == 25.0
    assert a24["sources"]["cron"]["sessions"] == 1
    a7 = data["providers"]["anthropic"]["7d"]
    assert a7["sessions"] == 4 and a7["input"] == 1160
    assert a7["sources"]["desktop"]["sessions"] == 2
    other = data["providers"]["other"]["24h"]
    assert other["sessions"] == 2 and set(other["sources"]) == {"kanban", "tool"}
    assert data["providers"]["openai-codex"]["24h"]["hit"] == 30.0


def test_empty_db_reports_every_provider_with_no_hit(tmp_path):
    data = collect(make_db(tmp_path / "state.db", []))
    for p in core.CACHE_PROVIDERS:
        for w in ("24h", "7d"):
            agg = data["providers"][p][w]
            assert agg["sessions"] == 0 and agg["hit"] is None and agg["sources"] == {}
    assert data["top"] == []


def test_top_burners_ranked_by_uncached_input_plus_writes(tmp_path):
    rows = [sess(f"s{i}", "anthropic", "desktop", i * M, 10 * M, 0, title=f"t{i}") for i in range(1, 8)]
    rows.append(sess("w", "openai-codex", "cli", 1 * M, 1 * M, 19 * M, model="gpt", title=None))
    rows.append(sess("old", "anthropic", "desktop", 99 * M, 0, 0, last=NOW - 30 * H))
    data = collect(make_db(tmp_path / "state.db", rows))
    top = data["top"]
    assert [t["id"] for t in top] == ["w", "s7", "s6", "s5", "s4"]
    assert len(top) == core.CACHE_TOP_N
    w = top[0]
    assert w == {"id": "w", "title": None, "source": "cli", "model": "gpt", "provider": "openai-codex",
                 "input": 1 * M, "cache_read": 1 * M, "cache_write": 19 * M, "output": 0,
                 "burn": 20 * M, "hit": core.hit_pct(1 * M, 1 * M, 19 * M)}


def test_db_is_opened_read_only_and_left_untouched(tmp_path, monkeypatch):
    db = make_db(tmp_path / "state.db", [sess("a", "anthropic", "desktop", 1, 1, 1)])
    before = (db.stat().st_mtime_ns, db.read_bytes())
    seen = []
    real_connect = sqlite3.connect

    def spy(target, *a, **kw):
        seen.append((target, kw))
        return real_connect(target, *a, **kw)

    monkeypatch.setattr(core.sqlite3, "connect", spy)
    collect(db)
    assert seen and all(kw.get("uri") is True and str(t).endswith("?mode=ro") for t, kw in seen)
    assert all(kw.get("timeout", 5) <= 1 for _, kw in seen), "busy timeout must be short"
    assert (db.stat().st_mtime_ns, db.read_bytes()) == before


# ── missing / locked DB ────────────────────────────────────────────────────────

def test_missing_db_is_a_failed_outcome_and_is_not_created(tmp_path):
    db = tmp_path / "state.db"
    ok, kind, message, data = core.collect_cache_outcome(db, NOW, CFG)
    assert (ok, kind, data) == (False, "missing", None)
    assert not db.exists()


def test_locked_db_is_a_failed_outcome(tmp_path):
    db = make_db(tmp_path / "state.db", [sess("a", "anthropic", "desktop", 1, 1, 1)])
    locker = sqlite3.connect(db, isolation_level=None)
    locker.execute("BEGIN EXCLUSIVE")
    try:
        t0 = time.monotonic()
        ok, kind, message, data = core.collect_cache_outcome(db, NOW, CFG, timeout_s=0.05)
        assert time.monotonic() - t0 < 2
    finally:
        locker.execute("ROLLBACK")
        locker.close()
    assert (ok, kind, data) == (False, "locked", None)


def test_non_database_file_is_a_db_error(tmp_path):
    db = tmp_path / "state.db"
    db.write_bytes(b"not a sqlite database" * 100)
    ok, kind, _, data = core.collect_cache_outcome(db, NOW, CFG)
    assert (ok, kind, data) == (False, "db_error", None)


def test_failed_collection_records_cache_slot_and_never_touches_tier_or_mode(tmp_path):
    st = core.empty_state()
    core.apply_refresh(st, {"anthropic": {"fetched_at": NOW, "windows": [
        {"label": "week", "used_percent": 99.0, "resets_at": NOW + 48 * H}]}}, NOW, CFG)
    tiers = {p: st["providers"][p]["tier"] for p in core.WATCHED}
    mode, pending = st["mode"], list(st["pending"])

    core.apply_cache(st, core.collect_cache_outcome(tmp_path / "nope.db", NOW, CFG), NOW, CFG)
    err = st["cache"]["fetch"]["error"]
    assert err["kind"] == "missing" and err["since"] == NOW
    core.apply_cache(st, (False, "locked", "database is locked", None), NOW + 900, CFG)
    err = st["cache"]["fetch"]["error"]
    assert err["kind"] == "locked" and err["since"] == NOW and err["at"] == NOW + 900
    assert {p: st["providers"][p]["tier"] for p in core.WATCHED} == tiers
    assert st["mode"] == mode and st["pending"] == pending

    data = collect(make_db(tmp_path / "state.db", []), now=NOW + 1800)
    core.apply_cache(st, (True, None, "", data), NOW + 1800, CFG)
    assert st["cache"]["fetch"] == {"last_ok_at": NOW + 1800, "error": None}
    assert st["cache"]["collected_at"] == NOW + 1800


def test_state_keeps_aggregates_only(tmp_path):
    rows = [sess(f"s{i}", "anthropic", "desktop", M, 0, 20 * M) for i in range(20)]
    st = core.empty_state()
    core.apply_cache(st, core.collect_cache_outcome(make_db(tmp_path / "state.db", rows), NOW, CFG), NOW, CFG)
    assert set(st["cache"]) <= {"collected_at", "providers", "top", "fetch", "alerts_sent"}
    assert len(st["cache"]["top"]) == core.CACHE_TOP_N
    # repeated refreshes replace, never append
    core.apply_cache(st, core.collect_cache_outcome(tmp_path / "state.db", NOW + 900, CFG), NOW + 900, CFG)
    assert len(st["cache"]["top"]) == core.CACHE_TOP_N


# ── alert gating ───────────────────────────────────────────────────────────────

def _alerts(st, db, now, cfg=CFG):
    before = list(st["pending"])
    lines = core.apply_cache(st, core.collect_cache_outcome(db, now, cfg), now, cfg)
    assert [l for l in st["pending"] if l not in before] == lines
    return lines


def test_low_hit_alert_fires_above_min_volume_once_per_24h(tmp_path):
    # 30M total input at 50% hit
    db = make_db(tmp_path / "state.db", [
        sess("a", "anthropic", "desktop", 15 * M, 15 * M, 0, last=NOW - 60)])
    st = core.empty_state()
    lines = _alerts(st, db, NOW)
    assert len(lines) == 1 and lines[0].startswith("⚠️") and "anthropic" in lines[0] and "50.0%" in lines[0]
    assert _alerts(st, db, NOW + 1 * H) == []
    assert _alerts(st, db, NOW + 23.9 * H) == []
    # 24h later, a still-active low-hit provider alerts again (after the cron drained the first)
    core.drain_pending(st)
    con = sqlite3.connect(db)
    con.execute("UPDATE sessions SET last_activity_at = ?", (NOW + 24 * H - 60,))
    con.commit()
    con.close()
    assert len(_alerts(st, db, NOW + 24 * H)) == 1


def test_low_hit_alert_quiet_below_min_volume_or_at_threshold(tmp_path):
    st = core.empty_state()
    low_volume = make_db(tmp_path / "a.db", [sess("a", "anthropic", "desktop", 5 * M, 5 * M, 0)])
    assert _alerts(st, low_volume, NOW) == []
    at_threshold = make_db(tmp_path / "b.db", [sess("b", "anthropic", "desktop", 20 * M, 80 * M, 0)])
    assert _alerts(st, at_threshold, NOW) == []


def test_alert_thresholds_are_configurable(tmp_path):
    db = make_db(tmp_path / "state.db", [sess("a", "openai-codex", "cli", 1 * M, 9 * M, 0)])
    cfg = dict(CFG, cache_alert_below=95.0, cache_alert_min_tokens=5 * M)
    lines = _alerts(core.empty_state(), db, NOW, cfg)
    assert len(lines) == 1 and "openai-codex" in lines[0] and "(<95%)" in lines[0]


def test_session_cache_write_alert(tmp_path):
    db = make_db(tmp_path / "state.db", [
        sess("burner", "openai-codex", "cli", 1 * M, 5 * M, 12 * M, model="gpt", title="big job"),
        sess("fine", "anthropic", "desktop", 0, 100 * M, 12 * M),        # hit 89%: quiet
        sess("small", "anthropic", "desktop", 5 * M, 0, 5 * M),          # writes < 10M: quiet
    ])
    st = core.empty_state()
    lines = [l for l in _alerts(st, db, NOW) if "session" in l]
    assert len(lines) == 1 and "burner" in lines[0] and "12.0M" in lines[0]
    assert [l for l in _alerts(st, db, NOW + H) if "session" in l] == []
    cfg = dict(CFG, cache_write_alert_tokens=4 * M, cache_write_alert_below=60.0)
    ids = {l.split("session ")[1].split()[0] for l in _alerts(core.empty_state(), db, NOW, cfg) if "session" in l}
    assert ids == {"burner", "small"}


def test_alerts_only_evaluated_on_successful_collection():
    st = core.empty_state()
    assert core.apply_cache(st, (False, "missing", "no state.db", None), NOW, CFG) == []
    assert st["pending"] == []


# ── surfaces ───────────────────────────────────────────────────────────────────

def test_status_payload_cache_shape(tmp_path):
    db = make_db(tmp_path / "state.db", [
        sess("a", "anthropic", "desktop", 100, 900, 0, title="hello"),
        sess("c", "openai-codex", "cli", 700, 300, 0)])
    st = core.empty_state()
    empty = core.status_payload(st, NOW, CFG, "h")["cache"]
    assert empty["collected_at"] is None and empty["stale"] is True
    assert empty["providers"] == {} and empty["top"] == []

    core.apply_cache(st, core.collect_cache_outcome(db, NOW, CFG), NOW, CFG)
    payload = json.loads(json.dumps(core.status_payload(st, NOW + 60, CFG, "h")))
    assert payload["v"] == 1
    cache = payload["cache"]
    assert set(cache) == {"collected_at", "stale", "alert_below", "providers", "top", "fetch"}
    assert cache["collected_at"] == NOW and cache["stale"] is False
    assert cache["alert_below"] == CFG["cache_alert_below"]
    assert cache["fetch"] == {"last_ok_at": NOW, "error": None}
    assert set(cache["providers"]) == set(core.CACHE_PROVIDERS)
    for p in core.CACHE_PROVIDERS:
        for w in ("24h", "7d"):
            assert set(cache["providers"][p][w]) == {"sessions", "input", "cache_read", "cache_write",
                                                     "output", "hit", "sources"}
    assert cache["providers"]["anthropic"]["24h"]["hit"] == 90.0
    assert set(cache["top"][0]) == {"id", "title", "source", "model", "provider", "input",
                                    "cache_read", "cache_write", "output", "burn", "hit"}
    later = core.status_payload(st, NOW + CFG["stale_after_s"] + 1, CFG, "h")["cache"]
    assert later["stale"] is True


def test_render_status_shows_cache_block_and_failures(tmp_path):
    db = make_db(tmp_path / "state.db", [
        sess("sess-a", "anthropic", "desktop", 100, 900, 0, title="hello"),
        sess("sess-c", "openai-codex", "cli", 700, 300, 0)])
    st = core.empty_state()
    core.apply_cache(st, core.collect_cache_outcome(db, NOW, CFG), NOW, CFG)
    text = core.render_status(st, NOW + 120, CFG)
    assert "cache:" in text
    lines = text.splitlines()
    a = next(l for l in lines if l.strip().startswith("anthropic") and "24h" in l)
    assert "90.0%" in a and "7d" in a
    assert any(l.strip().startswith("desktop") and "90.0%" in l for l in lines)
    assert any("top burners" in l for l in lines)
    assert any("sess-c" in l and "30.0%" in l for l in lines)

    core.apply_cache(st, (False, "locked", "database is locked", None), NOW + 900, CFG)
    text = core.render_status(st, NOW + 900, CFG)
    assert "cache stats fetch failed" in text and "locked" in text
    assert "90.0%" in text  # last good aggregates stay visible

    assert "cache: no data yet" in core.render_status(core.empty_state(), NOW, CFG)


def test_fmt_tokens():
    assert core.fmt_tokens(999) == "999"
    assert core.fmt_tokens(12_300) == "12.3K"
    assert core.fmt_tokens(19_000_000) == "19.0M"
    assert core.fmt_tokens(1_250_000_000) == "1.25B"


# ── plugin wiring: refresh() collects, hooks never do ──────────────────────────

def _load_plugin(tmp_home, monkeypatch):
    core_spec = importlib.util.spec_from_file_location("qr_core_cachewire", _root / "core.py")
    core_mod = importlib.util.module_from_spec(core_spec)
    monkeypatch.setitem(sys.modules, "qr_core_cachewire", core_mod)
    core_spec.loader.exec_module(core_mod)
    plugin_spec = importlib.util.spec_from_file_location("qr_plugin_cachewire", _root / "__init__.py")
    plugin_mod = importlib.util.module_from_spec(plugin_spec)
    monkeypatch.setitem(sys.modules, "qr_plugin_cachewire", plugin_mod)
    plugin_spec.loader.exec_module(plugin_mod)
    monkeypatch.setitem(sys.modules, "quota_router_core", core_mod)
    real_state = core_mod.state_path()
    core_mod.state_path = lambda hermes_home=None: tmp_home / "cache" / "quota-router" / "state.json"
    core_mod.state_db_path = lambda hermes_home=None: tmp_home / "state.db"
    return plugin_mod, core_mod, real_state


def _mtime(p: Path):
    return p.stat().st_mtime_ns if p.exists() else None


def test_refresh_collects_cache_into_tmp_state_only(tmp_path, monkeypatch):
    tmp_home = tmp_path / ".hermes"
    tmp_home.mkdir()
    plugin_mod, core_mod, real_state = _load_plugin(tmp_home, monkeypatch)
    real_before = _mtime(real_state)
    now = time.time()
    make_db(tmp_home / "state.db", [sess("a", "anthropic", "desktop", 15 * M, 15 * M, 0, last=now - 60)])
    lines = plugin_mod.refresh(now=now, snapshots={}, outcomes={}, drain=True)
    assert any("cache" in l and "anthropic" in l for l in lines)
    st = core_mod.read_state(core_mod.state_path())
    assert st["cache"]["providers"]["anthropic"]["24h"]["hit"] == 50.0
    assert '"cache"' in core_mod.state_path().read_text()
    status = json.loads(plugin_mod.status_json_line()[len("QRJSON "):])
    assert status["cache"]["providers"]["anthropic"]["24h"]["hit"] == 50.0
    assert "cache:" in plugin_mod._slash("")
    assert _mtime(real_state) == real_before, "real state.json must be untouched"


def test_refresh_missing_db_records_outcome(tmp_path, monkeypatch):
    tmp_home = tmp_path / ".hermes"
    plugin_mod, core_mod, real_state = _load_plugin(tmp_home, monkeypatch)
    real_before = _mtime(real_state)
    plugin_mod.refresh(now=NOW, snapshots={}, outcomes={}, drain=False)
    st = core_mod.read_state(core_mod.state_path())
    assert st["cache"]["fetch"]["error"]["kind"] == "missing"
    assert not (tmp_home / "state.db").exists()
    assert _mtime(real_state) == real_before


def test_hooks_never_collect_cache(tmp_path, monkeypatch):
    tmp_home = tmp_path / ".hermes"
    plugin_mod, core_mod, real_state = _load_plugin(tmp_home, monkeypatch)

    def boom(*a, **kw):
        raise AssertionError("cache collection on the hook path")

    monkeypatch.setattr(core_mod, "collect_cache_outcome", boom)
    plugin_mod._on_api_error(provider="anthropic", status_code=429, reason="rate_limit",
                             error={"message": "usage limit reached"})
    plugin_mod._on_api_success(provider="anthropic")
    st = core_mod.read_state(core_mod.state_path())
    assert "anthropic" in st["blocks"] or st["errors"]
    assert not st.get("cache")
