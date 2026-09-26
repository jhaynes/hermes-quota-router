"""quota-router: host-level quota awareness for Anthropic + OpenAI Codex.

What it does
- Records REAL quota blocks observed in any session on this host (``api_request_error``) and clears
  them on the next success from that provider (``post_api_request``). Usage percentage alone is not
  a block: Codex has served requests at 100% weekly.
- Keeps a shared, locked state file at ``<root HERMES_HOME>/cache/quota-router/state.json``
  (host-level, shared by every profile and the gateway).
- ``hermes quota-router refresh`` folds live ``/usage`` snapshots in, computes tiers with hysteresis
  and a mode (normal / shift / internal), and prints ONLY new alerts/transitions, so it can run as a
  silent no-agent cron job.
- ``hermes quota-router status`` / ``/quota`` show the current picture.

What it deliberately does NOT do
- Override error classification: Hermes already fails over immediately on rate-limit/billing
  errors when the credential pool cannot rotate (``route_classified_error``). A duplicate override
  could only make it worse.
- Rewrite config or pin kanban cards: pinning removes the fallback chain. Routing before a session
  starts needs an upstream ``resolve_session_route`` hook (proposal tracked in the plan).
"""
from __future__ import annotations

import importlib.util
import json
import logging
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_here = Path(__file__).resolve().parent


def _core():
    name = "quota_router_core"
    mod = sys.modules.get(name)
    if mod is None:
        spec = importlib.util.spec_from_file_location(name, _here / "core.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return mod


def _cfg() -> dict:
    core = _core()
    cfg = dict(core.DEFAULTS)
    try:
        from hermes_cli.config import load_config
        user = ((load_config() or {}).get("plugins", {}).get("entries", {}).get("quota-router", {}) or {})
        for k in cfg:
            if k in user:
                cfg[k] = user[k]
    except Exception:
        pass
    return cfg


def _throttle():
    name = "quota_router_throttle"
    mod = sys.modules.get(name)
    if mod is None:
        _core()
        spec = importlib.util.spec_from_file_location(name, _here / "throttle.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return mod


def _throttle_cfg() -> dict:
    """``plugins.entries.quota-router.throttle`` from this process's config (the root config on the
    cron path)."""
    user = None
    try:
        from hermes_cli.config import load_config
        user = ((load_config() or {}).get("plugins", {}).get("entries", {}).get("quota-router", {}) or {}
                ).get("throttle")
    except Exception:
        pass
    return _core().throttle_config(user)


# ── hooks (hot path: keep cheap, never raise) ──────────────────────────────────

def _on_api_error(provider=None, model=None, status_code=None, reason=None, error=None, **_kw):
    core = _core()
    prov = (provider or "").strip().lower()
    rsn = str(reason or "").lower()
    if not (status_code in core.QUOTA_STATUS or rsn in core.QUOTA_REASONS):
        return
    msg = (error or {}).get("message", "") if isinstance(error, dict) else str(error or "")
    now = time.time()
    try:
        with core.locked_state(core.state_path()) as st:
            line = core.record_error(st, provider=prov, status_code=status_code, reason=rsn, message=msg,
                                     model=model or "", now=now, cfg=_cfg())
            lines = core.recompute(st, now, _cfg())
            all_lines = ([line] if line else []) + lines
            core.queue(st, *all_lines)
        for ln in all_lines:
            logger.warning(ln)
    except Exception as exc:  # observer: fail open
        logger.debug("quota-router: record_error failed: %s", exc)


def _on_api_success(provider=None, **_kw):
    core = _core()
    prov = (provider or "").strip().lower()
    if prov not in core.WATCHED:
        return
    path = core.state_path()
    now = time.time()
    # Unlocked read first: the common case (no block) costs one small file read.
    if not core.active_block(core.read_state(path).get("blocks", {}).get(prov), now):
        return
    try:
        with core.locked_state(path) as st:
            if core.clear_block_on_success(st, prov, now):
                lines = [f"quota-router: {prov} served a request again; block cleared"] + core.recompute(st, now, _cfg())
                core.queue(st, *lines)
                for ln in lines:
                    logger.warning(ln)
    except Exception as exc:
        logger.debug("quota-router: clear failed: %s", exc)


# ── refresh / status ───────────────────────────────────────────────────────────

SCOPED_THROTTLE_S = 300


def _anthropic_headers(token: str) -> dict:
    """Headers for the Anthropic ``/api/oauth/usage`` GET — must match core's
    ``agent/account_usage.py:595-598`` exactly."""
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "claude-code/2.1.0",
    }


def _fetch_anthropic_scoped():
    """Fetch and parse Anthropic model-scoped weekly limits, or ``None`` to skip (no token /
    non-OAuth). Raises on a real fetch failure — callers record ``scoped_error`` and continue.
    Never logs the token or the raw payload.
    """
    import urllib.request
    from agent.anthropic_credentials import resolve_anthropic_token, _is_oauth_token
    token = resolve_anthropic_token()
    if not token or not _is_oauth_token(token):
        return None
    core = _core()
    req = urllib.request.Request("https://api.anthropic.com/api/oauth/usage",
                                 headers=_anthropic_headers(token), method="GET")
    with urllib.request.urlopen(req, timeout=15) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return core.parse_scoped_limits(payload)


def _fetch_anthropic_scoped_outcome():
    """(ok, kind, message, windows|None). Wraps the credential import so an import failure maps
    to a display-only ``scoped_error: {kind: 'import'}`` instead of crashing refresh()."""
    try:
        windows = _fetch_anthropic_scoped()
    except ImportError as exc:
        return False, "import", f"{type(exc).__name__}: {exc}", None
    except Exception as exc:
        return False, "scoped_error", f"{type(exc).__name__}: {str(exc)[:200]}", None
    if windows is None:
        return True, None, "", None  # skipped: no token / not OAuth, not an error
    return True, None, "", windows


def _snap_dict(core, snap, now):
    """Normalize a core snapshot (object or dict or None) to the plain dict shape."""
    return core.normalize_snapshot(snap, now)


def refresh(now: float | None = None, snapshots: dict | None = None, drain: bool = False,
            outcomes: dict | None = None) -> list[str]:
    core = _core()
    now = time.time() if now is None else now
    if snapshots is None:
        from agent.account_usage import fetch_account_usage
        snapshots = {}
        outcomes = {}
        cfg0 = _cfg()
        with core.locked_state(core.state_path()) as st0:  # backoff decision + its one alert
            due = {p: core.fetch_due(st0, p, now, cfg0) for p in core.WATCHED}
        for p in core.WATCHED:
            if not due[p]:
                continue
            try:
                snap = fetch_account_usage(p)
            except Exception as exc:
                logger.warning("quota-router: usage fetch for %s failed: %s", p, exc)
                outcomes[p] = (False, "fetch_error", f"{type(exc).__name__}: {exc}")
                continue
            reason = snap is not None and getattr(snap, "unavailable_reason", None)
            if reason:
                outcomes[p] = (False, "unavailable", str(reason))
                continue
            if snap is None:
                # Core's fetch_account_usage swallows every exception and also returns None when no
                # credential resolves, so None is a failed fetch, never a success.
                outcomes[p] = (False, "no_data", "usage fetch returned nothing (fetch error or no credential)")
                continue
            snapshots[p] = snap
            outcomes[p] = (True, None, "")
    elif outcomes is None:
        outcomes = {p: (True, None, "") for p in snapshots}

    cfg = _cfg()
    core_anthropic_ok = outcomes.get("anthropic", (False, None, ""))[0] and "anthropic" in snapshots
    scoped_windows = None
    scoped_outcome = None
    if core_anthropic_ok:
        prior = core.read_state(core.state_path())
        pstate = prior["providers"].get("anthropic") or {}
        last_scoped_fetch = ((pstate.get("fetch") or {}).get("scoped") or {}).get("scoped_fetched_at")
        throttled = last_scoped_fetch is not None and now - last_scoped_fetch < SCOPED_THROTTLE_S
        if not throttled:
            ok, kind, message, windows = _fetch_anthropic_scoped_outcome()
            scoped_outcome = (ok, kind, message)
            if ok and windows is not None:
                scoped_windows = windows

    # Cache stats: read-only state.db aggregate, collected before taking the state lock.
    cache_outcome = core.collect_cache_outcome(core.state_db_path(), now, cfg)

    with core.locked_state(core.state_path()) as st:
        for p, (ok, kind, message) in outcomes.items():
            core.note_fetch(st, p, ok=ok, kind=kind, message=message, now=now, cfg=cfg)
        if core_anthropic_ok:
            pstate = st["providers"].setdefault("anthropic", {"tier": "ok", "tier_since": now})
            fetch = pstate.setdefault("fetch", {})
            scoped_slot = fetch.setdefault("scoped", {"last_ok_at": None, "error": None, "alerted": False})
            if scoped_outcome is not None:
                ok, kind, message = scoped_outcome
                core.note_fetch(st, "anthropic", slot="scoped", ok=ok, kind=kind, message=message,
                                now=now, cfg=cfg)
                scoped_slot["scoped_fetched_at"] = now
                if scoped_windows is not None:
                    scoped_slot["scoped_windows"] = scoped_windows
                    scoped_slot["scoped_windows_fetched_at"] = now
            # Merge fresh-or-carried scoped windows into the anthropic snapshot, dropping any
            # that are older than stale_after_s.
            carried = scoped_slot.get("scoped_windows") or []
            carried_age = now - float(scoped_slot.get("scoped_windows_fetched_at") or 0)
            if carried and carried_age <= cfg["stale_after_s"]:
                extra = [dict(w, stale=carried_age > 0 and scoped_windows is None) for w in carried]
                snap = snapshots.get("anthropic")

                def _wget(w, k, d=None):
                    return w.get(k, d) if isinstance(w, dict) else getattr(w, k, d)

                if snap is not None:
                    raw_windows = list((snap.get("windows") if isinstance(snap, dict) else snap.windows) or [])
                else:
                    raw_windows = []
                names = {_wget(w, "scope") for w in raw_windows}
                extra = [w for w in extra if w.get("scope") not in names]
                if isinstance(snap, dict):
                    snap = dict(snap)
                    snap["windows"] = raw_windows + extra
                    snapshots["anthropic"] = snap
                elif snap is not None:
                    import types as _types
                    extra_ns = [_types.SimpleNamespace(**w) for w in extra]
                    snapshots["anthropic"] = _types.SimpleNamespace(
                        fetched_at=getattr(snap, "fetched_at", now),
                        unavailable_reason=getattr(snap, "unavailable_reason", None),
                        plan=getattr(snap, "plan", None),
                        details=list(getattr(snap, "details", None) or []),
                        windows=raw_windows + extra_ns,
                    )
                else:
                    snapshots["anthropic"] = {
                        "fetched_at": now, "unavailable_reason": None, "plan": None,
                        "details": [], "windows": extra,
                    }
        core.apply_refresh(st, snapshots, now, cfg)
        core.apply_cache(st, cache_outcome, now, cfg)
        return core.drain_pending(st) if drain else []


def status_text(fetch: bool = False) -> str:
    core = _core()
    if fetch:
        refresh(drain=False)
    return core.render_status(core.read_state(core.state_path()), time.time(), _cfg())


def status_json_line(fetch: bool = False) -> str:
    """Build the ``QRJSON <payload>`` line for ``status --json``. Never drains the queue."""
    core = _core()
    if fetch:
        refresh(drain=False)
    import socket
    now = time.time()
    payload = core.status_payload(core.read_state(core.state_path()), now, _cfg(), socket.gethostname())
    return "QRJSON " + json.dumps(payload)


def _cli_setup(sub):
    sp = sub.add_subparsers(dest="qr_cmd")
    s = sp.add_parser("status", help="Show quota state (add --refresh to fetch live usage first)")
    s.add_argument("--refresh", action="store_true")
    s.add_argument("--json", action="store_true", help="Print one QRJSON <payload> line for the desktop widget")
    sp.add_parser("refresh", help="Fetch live usage; print only new alerts/transitions (cron-friendly)")
    sp.add_parser("path", help="Print the host-level state file path")
    th = sp.add_parser("throttle", help="Host-local burn-rate throttle (fast/constrained)")
    tsp = th.add_subparsers(dest="qr_throttle_cmd")
    tsp.add_parser("apply", help="Decide the level and reconcile throttled profiles (cron path)")
    ts = tsp.add_parser("status", help="Show the throttle level, reasons and per-profile results")
    ts.add_argument("--json", action="store_true")
    h = tsp.add_parser("hold", help="Force a level for a while (overrides everything)")
    h.add_argument("level", nargs="?")
    h.add_argument("--for", dest="duration", default="2h")
    h.add_argument("--clear", action="store_true")
    d = tsp.add_parser("disable", help="Restore a level (default fast) and stop following the policy")
    d.add_argument("--level", default="fast")
    d.add_argument("--clear", action="store_true", help="Resume following the policy")
    a1 = tsp.add_parser("apply-one", help="(internal) reconcile THIS profile's config to a level")
    a1.add_argument("level")
    a1.add_argument("--levels-json", required=True)
    a1.add_argument("--kanban", action="store_true")


def _throttle_handler(args) -> int:
    core, thr = _core(), _throttle()
    cmd = getattr(args, "qr_throttle_cmd", None) or "status"
    now = time.time()
    if cmd == "apply-one":
        print(thr.child_main(args.level, args.levels_json, args.kanban, now))
        return 0
    t = _throttle_cfg()
    path = core.state_path()
    if cmd == "status":
        s = thr.throttle_status(core.read_state(path), t, core.root_home(), now)
        print("QRTHROTTLE_STATUS " + json.dumps(s) if args.json else thr.render_throttle_status(s))
        return 0
    if cmd in ("hold", "disable"):
        try:
            with core.locked_state(path) as st:
                if cmd == "hold" and args.clear:
                    core.throttle_clear_hold(st)
                elif cmd == "hold":
                    core.throttle_hold(st, args.level or "", now + thr.parse_duration(args.duration), now)
                else:
                    core.throttle_disable(st, None if args.clear else args.level, now)
        except ValueError as exc:
            print(f"quota-router: {exc}")
            return 2
    for ln in thr.run_apply(path, _cfg(), t, thr.subprocess_runner, time.time()):
        print(ln)
    return 0


def _cli_handler(args):
    cmd = getattr(args, "qr_cmd", None) or "status"
    if cmd == "refresh":
        for ln in refresh(drain=True):
            print(ln)
        return 0
    if cmd == "path":
        print(_core().state_path())
        return 0
    if cmd == "throttle":
        return _throttle_handler(args)
    if getattr(args, "json", False):
        print(status_json_line(fetch=getattr(args, "refresh", False)))
        return 0
    print(status_text(fetch=getattr(args, "refresh", False)))
    return 0


def _slash(raw_args: str = "") -> str:
    return status_text(fetch="refresh" in (raw_args or ""))


def register(ctx):
    ctx.register_hook("api_request_error", _on_api_error)
    ctx.register_hook("post_api_request", _on_api_success)
    ctx.register_cli_command("quota-router", "Quota-aware routing state for anthropic / openai-codex",
                             _cli_setup, _cli_handler)
    ctx.register_command("quota", _slash, "Show provider quota state (/quota refresh to fetch live)",
                         args_hint="[refresh]")
