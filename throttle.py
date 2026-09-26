"""quota-router throttle apply: reconcile throttled profiles to the level ``core.throttle_step`` decides.

Two halves (v2 B2/B3):
- ``run_apply`` (parent, cron path): decide under the state lock, then for each throttled profile run
  ``hermes -p <profile> quota-router throttle apply-one ...`` (a subprocess, so profile scoping is the
  CLI's own), then record results and alerts under the state lock. Shadow mode (``enabled: false``)
  decides and logs only; it never starts a child.
- ``apply_one`` (child): read the profile's raw config, classify its parsed values against both level
  specs, and make at most ONE ``hermes_cli.config.atomic_config_write`` call. A value set that matches
  neither level is a hand edit and is never overwritten.
"""
from __future__ import annotations

import contextlib
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


def classify(raw: dict, levels: dict, desired: str, kanban: bool) -> Optional[str]:
    """The level whose spec the config's parsed values equal (desired checked first), else None."""
    core = _core()
    order = [desired] + [lv for lv in core.THROTTLE_LEVELS if lv != desired]
    for lv in order:
        if kanban:
            if _kanban_value(raw) == levels[lv].get("max_in_progress"):
                return lv
        elif actual_spec(raw) == spec_values(levels[lv]):
            return lv
    return None


def kanban_managed(levels: dict) -> bool:
    return all(isinstance((levels.get(lv) or {}).get("max_in_progress"), int)
               and not isinstance(levels[lv]["max_in_progress"], bool) for lv in ("fast", "constrained"))


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


def apply_one(path: Path, level: str, levels: dict, *, kanban: bool = False, backup_dir: Path,
              label: str, now: float) -> dict:
    """Reconcile one config file to ``level``. Result: ``noop`` | ``applied`` | ``drift`` |
    ``unsupported`` | ``error``, plus ``actual`` (the level the file matched before any write)."""
    core = _core()
    errs = core.validate_levels(levels)
    if kanban and not kanban_managed(levels):
        errs.append("both levels need an integer max_in_progress for the kanban key")
    if level not in core.THROTTLE_LEVELS:
        errs.append(f"unknown level {level!r}")
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
        spec = levels[level]
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


def apply_one_argv(level: str, levels: dict, *, kanban: bool) -> list[str]:
    """``quota-router`` sub-argv for the child. Level specs travel in argv because the child's
    config is the profile's, which doesn't carry the root throttle block."""
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
    if kanban_managed(t.get("levels") or {}):
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
            errs = core.validate_levels(t.get("levels"), t["protected_provider"]) if t.get("enabled") else []
            if errs:
                if th.get("config_error") != errs[0]:
                    core.queue(st, f"quota-router: throttle config invalid, not applying: {errs[0]}")
                th["config_error"] = errs[0]
            else:
                th.pop("config_error", None)
            prior = {p: dict(v) for p, v in (th.get("applied") or {}).items() if isinstance(v, dict)}
        results: dict = {}
        if t.get("enabled") and not errs:
            level = decision["level"]
            for profile, kanban in _targets(t):
                try:
                    rc, out, err = runner(profile, apply_one_argv(level, t["levels"], kanban=kanban))
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


def throttle_status(state: dict, t: dict, home: Path, now: float) -> dict:
    th = state.get("throttle") or {}
    level = th.get("level") or "fast"
    since = th.get("since")
    applied = th.get("applied") or {}
    profiles = {}
    for profile, kanban in _targets(t):
        rec = dict(applied.get(profile) or {})
        rec.pop("alerted", None)
        if not kanban:
            rec["leaked_sessions"] = (_leaked_sessions(Path(home), profile, t["protected_provider"], float(since))
                                      if level == "constrained" and since else 0)
        profiles[profile] = rec
    hold = th.get("hold") if th.get("hold") and float(th["hold"].get("until") or 0) > now else None
    return {"configured": bool(t.get("configured")), "enabled": bool(t.get("enabled")), "level": level,
            "since": since, "source": th.get("source"), "rule": th.get("rule"), "decided_at": th.get("decided_at"),
            "hold": hold, "disabled": th.get("disabled"), "config_error": th.get("config_error"),
            "profiles": profiles, "last": (th.get("log") or [None])[-1]}


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
    for label, w in ((s.get("last") or {}).get("windows") or {}).items():
        proj = "unknown" if w.get("projected") is None else (
            f"{w['projected']:.0f}% (short {w['projected_short']:.0f}%), "
            f"r_long {w['r_long']:.1f}%/h r_short {w['r_short']:.1f}%/h")
        out.append(f"    {label:<16} {w['used']:5.1f}%  → {proj}")
    for profile, rec in s["profiles"].items():
        bits = [f"{rec.get('result', 'never applied')}"]
        if rec.get("level"):
            bits.append(f"at {rec['level']}")
        if rec.get("message"):
            bits.append(str(rec["message"])[:120])
        if "leaked_sessions" in rec:
            leak = rec["leaked_sessions"]
            bits.append(f"leak {'?' if leak is None else leak} anthropic sessions while constrained")
        out.append(f"  {profile}: " + "; ".join(bits))
    return "\n".join(out)


_DUR = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd])\s*$")


def parse_duration(text: str) -> float:
    m = _DUR.match(text or "")
    if not m:
        raise ValueError(f"bad duration {text!r} (use e.g. 90m, 2h, 1d)")
    return float(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
