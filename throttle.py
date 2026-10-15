"""quota-router throttle apply: reconcile throttled profiles to the level ``core.throttle_step`` decides.

Two halves (v2 B2/B3):
- ``run_apply`` (parent, cron path): decide under the state lock, then for each throttled profile run
  ``hermes -p <profile> quota-router throttle apply-one ...`` (a subprocess, so profile scoping is the
  CLI's own), then record results and alerts under the state lock. Shadow mode (``enabled: false``)
  decides and logs only; it never starts a child.
- ``apply_one`` (child): read the profile's raw config, classify its parsed values against every level
  spec, and make at most ONE ``hermes_cli.config.atomic_config_write`` call. A value set that matches
  no level is a hand edit and is never overwritten.

Level specs arrive as the legacy ``levels`` mapping (``{fast, constrained}``) or as the ladder list
(``[{name, gate, provider, model, fallback, max_in_progress}]``, highest preference first).
"""
from __future__ import annotations

import contextlib
import copy
import fcntl
import importlib.util
import json
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

_here = Path(__file__).resolve().parent
RESULT_PREFIX = "QRTHROTTLE "
ROOT_PROFILE = "default"
# Keys in a ``model:`` block that belong to one route; switching provider under them would send the
# new provider's traffic to the old endpoint, so such a profile is skipped rather than rewritten.
ROUTE_KEYS = ("base_url", "api_mode", "api_key", "context_length")


def _core():
    mod = sys.modules.get("quota_router_core")
    if mod is None:
        spec = importlib.util.spec_from_file_location("quota_router_core", _here / "core.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["quota_router_core"] = mod
        spec.loader.exec_module(mod)
    return mod


# ── child: one profile, one atomic write ──────────────────────────────────────

def read_raw(path: Path) -> dict:
    from hermes_cli import config as hc
    return hc.read_user_config_raw(Path(path))


def _norm_provider(p: Any) -> str:
    return str(p or "").strip().lower()


def spec_values(spec: dict) -> dict:
    return {"provider": _norm_provider(spec.get("provider")), "model": str(spec.get("model") or "").strip(),
            "fallback": [(_norm_provider(f.get("provider")), str(f.get("model") or "").strip())
                         for f in spec.get("fallback") or []]}


def actual_spec(raw: dict) -> dict:
    """Parsed ``model.provider``, ``model.default`` and ``fallback_providers`` of a raw config."""
    model = raw.get("model")
    model = model if isinstance(model, dict) else {}
    fb = raw.get("fallback_providers")
    fb = fb if isinstance(fb, list) else []
    return {"provider": _norm_provider(model.get("provider")), "model": str(model.get("default") or "").strip(),
            "fallback": [(_norm_provider(f.get("provider")), str(f.get("model") or "").strip())
                         for f in fb if isinstance(f, dict)]}


def _kanban_value(raw: dict) -> Any:
    kb = raw.get("kanban")
    return kb.get("max_in_progress") if isinstance(kb, dict) else None


def level_specs(levels: Any) -> dict:
    """``{name: spec}`` in level order: the legacy ``levels`` mapping's two levels, or a ladder's
    levels (non-mapping entries skipped; ``validate_ladder`` reports them)."""
    if isinstance(levels, list):
        return {str(e.get("name")): e for e in levels if isinstance(e, dict)}
    levels = levels if isinstance(levels, dict) else {}
    return {lv: levels.get(lv) or {} for lv in _core().THROTTLE_LEVELS}


def validate(levels: Any) -> list[str]:
    core = _core()
    return core.validate_ladder(levels) if isinstance(levels, list) else core.validate_levels(levels)


def classify(raw: dict, levels: Any, desired: str, kanban: bool) -> Optional[str]:
    """The level whose spec the config's parsed values equal (desired checked first), else None."""
    specs = level_specs(levels)
    order = [desired] + [lv for lv in specs if lv != desired]
    for lv in order:
        if kanban:
            if _kanban_value(raw) == specs[lv].get("max_in_progress"):
                return lv
        elif actual_spec(raw) == spec_values(specs[lv]):
            return lv
    return None


def kanban_managed(levels: Any) -> bool:
    """Every level sets an integer ``max_in_progress`` (all levels or none)."""
    specs = level_specs(levels)
    return bool(specs) and all(isinstance(spec.get("max_in_progress"), int)
                               and not isinstance(spec["max_in_progress"], bool) for spec in specs.values())


def _backup(path: Path, backup_dir: Path, label: str, now: float) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromtimestamp(now).strftime("%Y%m%d-%H%M%S")
    dest = backup_dir / f"{label}-{stamp}.yaml"
    n = 1
    while dest.exists():
        dest = backup_dir / f"{label}-{stamp}-{n}.yaml"
        n += 1
    shutil.copy2(path, dest)
    return dest


def apply_one(path: Path, level: str, levels: Any, *, kanban: bool = False, backup_dir: Path,
              label: str, now: float) -> dict:
    """Reconcile one config file to ``level``. Result: ``noop`` | ``applied`` | ``drift`` |
    ``unsupported`` | ``error``, plus ``actual`` (the level the file matched before any write).
    A file matching any level other than ``level`` is rewritten; one matching none is a hand edit."""
    errs = validate(levels)
    if kanban and not kanban_managed(levels):
        errs.append("every level needs an integer max_in_progress for the kanban key" if isinstance(levels, list)
                    else "both levels need an integer max_in_progress for the kanban key")
    if level not in level_specs(levels):
        errs.append(f"unknown level {level!r}")
    if not errs and isinstance(levels, list):
        # Validate outside Hermes too (the back-test, a plain script): the child refuses rather
        # than touching a file, so the fail-closed checks do not depend on hermes_cli importing.
        ladder_errs = _core().validate_ladder(levels)
        if ladder_errs:
            errs.append(ladder_errs[0])
    if errs:
        return {"result": "error", "actual": None, "message": errs[0]}
    try:
        from hermes_cli import config as hc
        path = Path(path)
        raw = hc.read_user_config_raw(path)
        actual = classify(raw, levels, level, kanban)
        if actual == level:
            return {"result": "noop", "actual": actual}
        if actual is None:
            return {"result": "drift", "actual": None}
        model = raw.get("model") if isinstance(raw.get("model"), dict) else {}
        own = [f"model.{k}" for k in ROUTE_KEYS if model.get(k) not in (None, "")]
        # A fallback entry is rewritten as {provider, model}; any other key on it would be dropped.
        own += [f"fallback_providers[{i}].{k}" for i, f in enumerate(raw.get("fallback_providers") or [])
                if isinstance(f, dict) for k in f if k not in ("provider", "model")]
        if own and not kanban:
            return {"result": "unsupported", "actual": actual, "message": f"{own[0]} is set"}
        spec = level_specs(levels)[level]
        if kanban:
            raw["kanban"]["max_in_progress"] = spec["max_in_progress"]
        else:
            raw["model"]["provider"] = spec["provider"]
            raw["model"]["default"] = spec["model"]
            raw["fallback_providers"] = [{"provider": f["provider"], "model": f["model"]}
                                         for f in spec.get("fallback") or []]
        _backup(path, Path(backup_dir), label, now)
        hc.atomic_config_write(path, raw)
        after = classify(hc.read_user_config_raw(path), levels, level, kanban)
        if after != level:
            return {"result": "error", "actual": actual, "message": f"verify failed: file reads as {after}"}
        return {"result": "applied", "actual": actual}
    except Exception as exc:
        return {"result": "error", "actual": None, "message": f"{type(exc).__name__}: {str(exc)[:200]}"}


def apply_one_argv(level: str, levels: Any, *, kanban: bool) -> list[str]:
    """``quota-router`` sub-argv for the child. Level specs travel in argv because the child's
    config is the profile's, which doesn't carry the root throttle block. ``--levels-json`` is the
    legacy mapping or the ladder list (a 0.3.0 child refuses a list: "must be a mapping")."""
    argv = ["throttle", "apply-one", level, "--levels-json", json.dumps(levels, sort_keys=True)]
    return argv + (["--kanban"] if kanban else [])


def parse_apply_one_argv(argv: list[str]) -> dict:
    assert argv[:2] == ["throttle", "apply-one"], argv
    return {"level": argv[2], "levels": json.loads(argv[argv.index("--levels-json") + 1]),
            "kanban": "--kanban" in argv}


def parse_result(stdout: str) -> Optional[dict]:
    for line in reversed((stdout or "").splitlines()):
        if line.startswith(RESULT_PREFIX):
            try:
                res = json.loads(line[len(RESULT_PREFIX):])
            except ValueError:
                return None
            return res if isinstance(res, dict) else None
    return None


def child_main(level: str, levels_json: str, kanban: bool, now: float) -> str:
    """Body of ``throttle apply-one`` inside ``hermes -p <profile>``: the CLI already scoped
    HERMES_HOME to the profile, so the config path and label come from it."""
    from hermes_cli import config as hc
    core = _core()
    home = Path(hc.get_config_path()).parent
    label = home.name if home.parent.name == "profiles" else ROOT_PROFILE
    try:
        levels = json.loads(levels_json)
    except ValueError as exc:
        return RESULT_PREFIX + json.dumps({"result": "error", "actual": None, "message": f"levels-json: {exc}"})
    res = apply_one(hc.get_config_path(), level, levels, kanban=kanban,
                    backup_dir=core.root_home() / "backups" / "quota-throttle", label=label, now=now)
    return RESULT_PREFIX + json.dumps(res)


# ── parent: decide, fan out, record ────────────────────────────────────────────

Runner = Callable[[str, list], tuple]


def hermes_bin() -> str:
    found = shutil.which("hermes")
    return found or str(Path.home() / ".local" / "bin" / "hermes")


def subprocess_runner(profile: str, argv: list) -> tuple:
    r = subprocess.run([hermes_bin(), "-p", profile, "quota-router", *argv],
                       capture_output=True, text=True, timeout=120)
    return r.returncode, r.stdout, r.stderr


@contextlib.contextmanager
def _apply_lock(state_path: Path):
    """Serialises whole apply runs (cron vs a manual ``throttle hold``) without holding the state
    lock across child processes, so hot-path hooks never wait on a config write."""
    lock = Path(state_path).with_name("throttle-apply.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a+") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _targets(t: dict) -> list[tuple[str, bool]]:
    out = [(str(p), False) for p in t.get("profiles") or []]
    if kanban_managed(_core().throttle_apply_levels(t) or {}):
        out.append((ROOT_PROFILE, True))
    return out


def _alert_for(profile: str, level: str, res: dict, prior_level: Optional[str], kanban: bool) -> Optional[str]:
    kind = res.get("result")
    what = "kanban.max_in_progress" if kanban else "model"
    if kind == "applied":
        if prior_level == level:
            return f"quota-router: throttle re-applied {level} to {profile} after external overwrite"
        return None
    if kind == "drift":
        return f"quota-router: throttle skipped {profile} (manual {what} change)"
    if kind == "unsupported":
        return f"quota-router: throttle skipped {profile} ({res.get('message')}; set by hand)"
    if kind == "error":
        return f"quota-router: throttle apply to {profile} failed ({res.get('message')})"
    return None


def run_apply(state_path: Path, cfg: dict, t: dict, runner: Runner, now: float, *, drain: bool = True) -> list[str]:
    """One cron run: decide (always, even in shadow mode), reconcile every target when enabled, and
    return the drained pending lines (``drain=False`` leaves them for the next cron run)."""
    core = _core()
    if not t.get("configured"):
        return []
    with _apply_lock(state_path):
        with core.locked_state(state_path) as st:
            decision = core.throttle_step(st, now, cfg, t)
            th = st["throttle"]
            errs = core.validate_throttle(t, cfg) if t.get("enabled") else []
            if errs:
                if th.get("config_error") != errs[0]:
                    core.queue(st, f"quota-router: throttle config invalid, not applying: {errs[0]}")
                th["config_error"] = errs[0]
            else:
                th.pop("config_error", None)
                # answered review r1 Q2: warn, don't reject — legacy `levels:` still validates and
                # decides exactly as 0.3.0, so an alias there is a warning line, not an error.
                warns = core.legacy_alias_warnings(t) if t.get("enabled") else []
                if warns and th.get("alias_warned") != warns:
                    core.queue(st, *(f"quota-router: throttle warning: {w}" for w in warns))
                if warns:
                    th["alias_warned"] = warns
                else:
                    th.pop("alias_warned", None)
            prior = {p: dict(v) for p, v in (th.get("applied") or {}).items() if isinstance(v, dict)}
        results: dict = {}
        if t.get("enabled") and not errs:
            level = decision["level"]
            for profile, kanban in _targets(t):
                try:
                    rc, out, err = runner(profile, apply_one_argv(level, core.throttle_apply_levels(t),
                                                                  kanban=kanban))
                    res = parse_result(out) if rc == 0 else None
                except Exception as exc:   # timeout, missing binary: this profile failed, go on
                    rc, out, err = None, "", ""
                    res = {"result": "error", "actual": None, "message": f"{type(exc).__name__}: {str(exc)[:160]}"}
                if res is None:
                    tail = (err or out or "").strip().splitlines()[-1:] or ["no result"]
                    res = {"result": "error", "actual": None, "message": f"exit {rc}: {tail[0][:160]}"}
                results[profile] = (level, kanban, res)
        with core.locked_state(state_path) as st:
            applied = st.setdefault("throttle", {}).setdefault("applied", {})
            for profile, (level, kanban, res) in results.items():
                rec = prior.get(profile) or {}
                line = _alert_for(profile, level, res, rec.get("level"), kanban)
                ok = res["result"] in ("noop", "applied")
                key = f"{res['result']}|{level}"
                new = {"level": level if ok else rec.get("level"), "result": res["result"],
                       "actual": res.get("actual"), "message": res.get("message"), "at": now,
                       "alerted": None if ok else rec.get("alerted")}
                if line and (ok or rec.get("alerted") != key):
                    core.queue(st, line)
                    if not ok:
                        new["alerted"] = key
                applied[profile] = new
            return core.drain_pending(st) if drain else []


# ── status ─────────────────────────────────────────────────────────────────────

def _leaked_sessions(home: Path, profile: str, provider: str, since: float) -> Optional[int]:
    """Sessions started on ``provider`` by ``profile`` since ``since`` (read-only ``state.db``)."""
    db = (home if profile == ROOT_PROFILE else home / "profiles" / profile) / "state.db"
    if not db.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        try:
            row = con.execute("select count(*) from sessions where started_at >= ? and lower(billing_provider) = ?",
                              (since, provider)).fetchone()
        finally:
            con.close()
        return int(row[0])
    except sqlite3.Error:
        return None


# ── resume stamping (opt-in ``throttle.stamp_resume``) ─────────────────────────
#
# Hermes' TUI/desktop gateway restores a stored session's model/provider on resume, and that restore
# becomes a session pin which the per-turn config sync never moves. So a chat that was resumed keeps
# the route it had, and a throttle flip never reaches it. Rows marked ``follow_profile_config`` (the
# Bot Chat contract) resume onto the profile's current config instead, and keep later explicit picks
# only until config moves again. Stamping that marker onto the throttled profiles' TUI/desktop rows
# makes their next resume follow the router. Guards: rows already following config, rows with recorded
# explicit-pick provenance, room plumbing/hidden rows, unparseable ``model_config`` and rows whose
# stored route is not one the ladder configures (an explicit ``/model`` pick elsewhere) are left alone,
# and nothing is written unless Hermes' own resume reader confirms the stamped row follows config.

STAMP_KEY = "follow_profile_config"
STAMP_SOURCES = ("tui", "desktop")
STAMP_BUSY_TIMEOUT_S = 5.0
STAMP_COUNTS = ("stamp", "already", "explicit_pick", "off_ladder", "plumbing", "unparseable", "no_model",
                "contract", "raced")


class StampRefused(Exception):
    """Hermes core no longer honours the stamp as this plugin expects: stamp nothing."""


def profile_state_db(home: Path, profile: str) -> Path:
    return (Path(home) if profile == ROOT_PROFILE else Path(home) / "profiles" / profile) / "state.db"


def ladder_routes(t: dict) -> list[tuple[str, str, str]]:
    """``(raw provider, canonical provider, model)`` for every level's primary and fallback entries
    (a ``main`` fallback is the level's own primary, already listed)."""
    core = _core()
    out = []
    for rung in core.throttle_ladder(t):
        for provider, model in core.level_chain(rung["spec"]):
            raw, model = _norm_provider(provider), str(model or "").strip()
            if raw and raw != "main" and model:
                out.append((raw, core.canonical_provider(raw), model))
    return list(dict.fromkeys(out))


def route_matches(provider: Any, model: Any, routes: list[tuple[str, str, str]]) -> bool:
    """Whether a stored ``(provider, model)`` is one of the ladder's routes. Providers compare as Hermes
    routes them; a bare ``custom`` (how the agent names any custom entry) matches any non-tracked route
    serving that model, and a row with no stored provider matches on the model alone."""
    model = str(model or "").strip()
    if not model:
        return False
    raw = _norm_provider(provider)
    canon = _core().canonical_provider(raw) if raw else ""
    tracked = _core().WATCHED
    for r_raw, r_canon, r_model in routes:
        if r_model != model:
            continue
        if not raw or canon == r_canon or raw == r_raw:
            return True
        if (raw == "custom" or canon == "custom") and r_canon not in tracked:
            return True
    return False


def _row_model_config(row: dict) -> Optional[dict]:
    raw = row.get("model_config")
    if raw in (None, ""):
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def classify_stamp_row(row: dict, routes: list[tuple[str, str, str]]) -> str:
    """``stamp`` for a row to mark, otherwise the reason it is left alone (``STAMP_COUNTS``)."""
    mc = _row_model_config(row)
    if mc is None:
        return "unparseable"
    if mc.get(STAMP_KEY) or str(row.get("title") or "").strip() == "Bot Chat":
        return "already"   # Hermes already resumes these onto the profile config
    if mc.get("room_plumbing") or row.get("hidden"):
        return "plumbing"
    if isinstance(mc.get("composer_override_profile"), dict):
        return "explicit_pick"   # an explicit pick with provenance; Hermes drops it once config moves
    model = str(row.get("model") or mc.get("model") or "").strip()
    if not model:
        return "no_model"
    provider = mc.get("provider") or row.get("billing_provider")
    return "stamp" if route_matches(provider, model, routes) else "off_ladder"


def stamped_row(row: dict) -> dict:
    out = dict(row)
    out["model_config"] = json.dumps({**(_row_model_config(row) or {}), STAMP_KEY: True})
    return out


def hermes_stamp_contract() -> Callable[[dict], bool]:
    """Hermes' own resume reader: a stamped row must count as profile-following and restore no stored
    route. Importing the gateway module is the only way to ask it; raises ``StampRefused`` when the
    reader is missing (an incompatible Hermes) so nothing is stamped blind. The import repoints
    ``sys.stdout`` (its JSON-RPC guard) and the excepthooks; this CLI process gets them back."""
    import threading
    saved = sys.stdout, sys.excepthook, threading.excepthook
    try:
        # Current Hermes defines these in ``tui_gateway.agent_factory`` but only ``server`` binds them to
        # live globals (``method_ctx.bind_module``); older builds define them in ``server`` itself.
        from tui_gateway import server as gw
        follows, overrides = gw._row_follows_profile, gw._stored_session_runtime_overrides
    except Exception as exc:
        raise StampRefused(f"Hermes resume reader unavailable ({type(exc).__name__}: {str(exc)[:120]})") from None
    finally:
        sys.stdout, sys.excepthook, threading.excepthook = saved

    def check(row: dict) -> bool:
        return bool(follows(row)) and overrides(row) == {}

    probe = {"id": "quota-router-probe", "source": "tui", "title": "", "model": "probe-model",
             "billing_provider": "anthropic", "model_config": json.dumps({STAMP_KEY: True})}
    try:
        ok = check(probe)
    except Exception as exc:
        raise StampRefused(f"Hermes resume reader failed ({type(exc).__name__}: {str(exc)[:120]})") from None
    if not ok:
        raise StampRefused(f"Hermes no longer resumes {STAMP_KEY} rows onto the profile config")
    return check


def _session_rows(con: sqlite3.Connection) -> list[dict]:
    cols = {r[1] for r in con.execute("pragma table_info(sessions)")}
    want = [c for c in ("id", "source", "title", "model", "billing_provider", "model_config", "hidden") if c in cols]
    if not {"id", "source", "model", "model_config"} <= set(want):
        raise sqlite3.OperationalError("sessions table lacks id/source/model/model_config")
    marks = ",".join("?" * len(STAMP_SOURCES))
    cur = con.execute(f"select {', '.join(want)} from sessions where source in ({marks})", STAMP_SOURCES)
    return [dict(zip(want, r)) for r in cur.fetchall()]


def stamp_db(db: Path, routes: list[tuple[str, str, str]], contract: Callable[[], Callable[[dict], bool]],
             *, dry_run: bool) -> dict:
    """Classify (and unless ``dry_run`` stamp) one profile's TUI/desktop rows. Candidates are re-read and
    written in one ``BEGIN IMMEDIATE`` transaction, so no Hermes write lands between the guard and the
    stamp; each UPDATE also compares the old ``model_config``. ``contract`` is called only when there
    is something to stamp. Result: counts per ``STAMP_COUNTS`` plus ``error``."""
    counts: dict[str, int] = dict.fromkeys(STAMP_COUNTS, 0)
    if not db.exists():
        return {**counts, "error": None, "missing": True}
    con = sqlite3.connect(str(db), timeout=STAMP_BUSY_TIMEOUT_S, isolation_level=None)
    try:
        rows = _session_rows(con)   # unlocked pre-scan: load Hermes' reader outside the write lock
        kinds = [classify_stamp_row(r, routes) for r in rows]
        if "stamp" not in kinds:
            for kind in kinds:
                counts[kind] += 1
            return {**counts, "error": None}
        check = contract()
        if not dry_run:
            con.execute("BEGIN IMMEDIATE")
        try:
            for r in _session_rows(con) if not dry_run else rows:
                kind = classify_stamp_row(r, routes)
                if kind == "stamp":
                    new = stamped_row(r)
                    try:
                        ok = check(new)
                    except Exception:
                        ok = False
                    if not ok:
                        kind = "contract"
                    elif not dry_run:
                        cur = con.execute("update sessions set model_config = ? where id = ? and model_config is ?",
                                          (new["model_config"], r["id"], r["model_config"]))
                        if cur.rowcount != 1:
                            kind = "raced"   # the row changed under us; next run retries
                counts[kind] += 1
            if not dry_run:
                con.execute("COMMIT")
        except BaseException:
            if not dry_run and con.in_transaction:
                con.execute("ROLLBACK")
            raise
        return {**counts, "error": None}
    finally:
        con.close()


def run_stamp(home: Path, t: dict, *, dry_run: bool = False,
              contract: Callable[[], Callable[[dict], bool]] = hermes_stamp_contract) -> dict:
    """Stamp every throttled profile's ``state.db``. ``{"refused": msg|None, "profiles": {name: result}}``;
    a refused contract stops the whole run before any write."""
    routes = ladder_routes(t)
    loaded: list = []

    def once():
        if not loaded:
            loaded.append(contract())
        return loaded[0]

    out: dict = {"refused": None, "dry_run": dry_run, "profiles": {}}
    for profile in [str(p) for p in t.get("profiles") or []]:
        try:
            out["profiles"][profile] = stamp_db(profile_state_db(home, profile), routes, once, dry_run=dry_run)
        except StampRefused as exc:
            out["refused"] = str(exc)
            break
        except sqlite3.Error as exc:
            out["profiles"][profile] = {**dict.fromkeys(STAMP_COUNTS, 0),
                                        "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
    return out


def stamp_alert_items(res: dict) -> list[tuple[str, bool]]:
    """Deliverable lines as ``(line, is_failure)``: stamps made (``False``), a refused contract and
    per-profile failures (``True``; the CLI delivers each distinct failure once)."""
    if res.get("refused"):
        return [(f"quota-router: resume stamp refused, nothing stamped: {res['refused']}", True)]
    items = []
    for profile, r in res["profiles"].items():
        if r.get("error"):
            items.append((f"quota-router: resume stamp for {profile} failed ({r['error']})", True))
        elif r.get("stamp") and not res.get("dry_run"):
            items.append((f"quota-router: resume stamp marked {r['stamp']} {profile} session(s) to follow config",
                          False))
    return items


def stamp_alerts(res: dict) -> list[str]:
    """Deliverable lines: stamps made, a refused contract, per-profile failures."""
    return [ln for ln, _ in stamp_alert_items(res)]


def render_stamp(res: dict) -> str:
    head = "resume stamp (dry run): would mark" if res.get("dry_run") else "resume stamp: marked"
    out = []
    if res.get("refused"):
        out.append(f"resume stamp refused, nothing stamped: {res['refused']}")
    for profile, r in res["profiles"].items():
        if r.get("missing"):
            out.append(f"  {profile}: no state.db")
            continue
        if r.get("error"):
            out.append(f"  {profile}: failed ({r['error']})")
            continue
        rest = ", ".join(f"{k.replace('_', ' ')} {r[k]}" for k in STAMP_COUNTS[1:] if r.get(k))
        out.append(f"  {profile}: {head} {r.get('stamp', 0)}" + (f"; left alone: {rest}" if rest else ""))
    return "\n".join(out) or "resume stamp: no throttled profiles"


def throttle_status(state: dict, t: dict, home: Path, now: float) -> dict:
    """Selected level, reasons, per-provider state and per-profile results. The leak count, per gate
    provider, is the sessions a throttled profile started on that provider since it became
    ``constrained`` (no selectable level routes to a constrained gate provider, so each one leaked)."""
    core = _core()
    th = dict(state.get("throttle") or {})
    rungs = core.throttle_ladder(t)
    gates = core.gate_providers(rungs)
    level = th.get("level") or rungs[0]["name"]
    since = th.get("since")
    applied = th.get("applied") or {}
    # on a copy: status never migrates or otherwise writes the state it reports
    pstates = core.throttle_provider_states(copy.deepcopy({k: v for k, v in th.items() if k != "log"}), t, gates)
    last = (th.get("log") or [None])[-1]
    providers = {}
    for p in gates:
        ps = pstates.get(p) or {}
        windows = ((last or {}).get("providers") or {}).get(p, {}).get("windows")
        if windows is None and len(gates) == 1:
            windows = (last or {}).get("windows")
        providers[p] = {"state": ps.get("state", "ok"), "since": ps.get("since"), "source": ps.get("source"),
                        "rule": ps.get("rule"), "label": ps.get("label"), "windows": windows or {}}
    profiles = {}
    for profile, kanban in _targets(t):
        rec = dict(applied.get(profile) or {})
        rec.pop("alerted", None)
        if not kanban:
            leaks = {p: (_leaked_sessions(Path(home), profile, p, float(ps["since"]))
                         if ps["state"] == "constrained" and ps["since"] else 0) for p, ps in providers.items()}
            counts = [n for n in leaks.values() if n is not None]
            rec["leaked_sessions"] = sum(counts) if counts or not leaks else None
            rec["leaked_by_provider"] = leaks
        profiles[profile] = rec
    hold = th.get("hold") if th.get("hold") and float(th["hold"].get("until") or 0) > now else None
    return {"configured": bool(t.get("configured")), "enabled": bool(t.get("enabled")), "level": level,
            "since": since, "source": th.get("source"), "rule": th.get("rule"), "decided_at": th.get("decided_at"),
            "hold": hold, "disabled": th.get("disabled"), "config_error": th.get("config_error"),
            "profiles": profiles, "last": last,
            "ladder": [{"name": r["name"], "gate": list(r["gate"])} for r in rungs], "providers": providers}


def render_throttle_status(s: dict) -> str:
    def ts(v):
        return datetime.fromtimestamp(v).strftime("%a %H:%M") if v else "?"

    if not s["configured"]:
        return "throttle: not configured (plugins.entries.quota-router.throttle)"
    out = [f"throttle: {s['level']}{'' if s['enabled'] else ' (shadow: enabled=false, nothing is written)'}"
           f"  since {ts(s['since'])}  source={s['source']}" + (f" rule={s['rule']}" if s.get("rule") else "")]
    if s.get("hold"):
        out.append(f"  hold {s['hold']['level']} until {ts(s['hold']['until'])}")
    if s.get("disabled"):
        out.append(f"  disabled → {s['disabled']['level']} (since {ts(s['disabled']['since'])})")
    if s.get("config_error"):
        out.append(f"  CONFIG INVALID: {s['config_error']}")

    def windows(ws: dict, indent: str) -> None:
        for label, w in (ws or {}).items():
            proj = "unknown" if w.get("projected") is None else (
                f"{w['projected']:.0f}% (short {w['projected_short']:.0f}%), "
                f"r_long {w['r_long']:.1f}%/h r_short {w['r_short']:.1f}%/h")
            if w.get("projection_allowance"):
                proj += f"; banked weekly allowance +{w['projection_allowance']:.0f}%"
            out.append(f"{indent}{label:<16} {w['used']:5.1f}%  → {proj}")

    gates = [p for r in s.get("ladder") or [] for p in r["gate"]]
    gates = list(dict.fromkeys(gates))
    if len(gates) > 1:
        out.append("  ladder: " + " → ".join(f"{r['name']} [{', '.join(r['gate']) or 'floor'}]"
                                               for r in s["ladder"]))
        for p in gates:
            ps = (s.get("providers") or {}).get(p) or {}
            out.append(f"  {p}: {ps.get('state', 'ok')}  since {ts(ps.get('since'))}  source={ps.get('source')}"
                       + (f" rule={ps['rule']}" if ps.get("rule") else ""))
            windows(ps.get("windows"), "      ")
    else:
        windows((s.get("last") or {}).get("windows"), "    ")
    for profile, rec in s["profiles"].items():
        bits = [f"{rec.get('result', 'never applied')}"]
        if rec.get("level"):
            bits.append(f"at {rec['level']}")
        if rec.get("message"):
            bits.append(str(rec["message"])[:120])
        by = rec.get("leaked_by_provider") or {}
        if len(by) > 1:
            bits.append("leak " + ", ".join(f"{'?' if n is None else n} {p}" for p, n in by.items())
                        + " sessions while constrained")
        elif "leaked_sessions" in rec:
            leak = rec["leaked_sessions"]
            provider = next(iter(by), "anthropic")
            bits.append(f"leak {'?' if leak is None else leak} {provider} sessions while constrained")
        out.append(f"  {profile}: " + "; ".join(bits))
    return "\n".join(out)


_DUR = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd])\s*$")


def parse_duration(text: str) -> float:
    m = _DUR.match(text or "")
    if not m:
        raise ValueError(f"bad duration {text!r} (use e.g. 90m, 2h, 1d)")
    return float(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
