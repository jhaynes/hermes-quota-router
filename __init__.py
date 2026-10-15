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
- ``hermes quota-router throttle apply`` (the cron path) picks a throttle level from per-provider
  burn-rate forecasts. It is opt-in and in shadow mode by default. With ``throttle.enabled: true`` it
  rewrites each listed profile's ``model.provider``, ``model.default`` and ``fallback_providers`` to
  that level, plus the root ``kanban.max_in_progress`` when every level sets it. It never overwrites
  a hand edit.

What it deliberately does NOT do
- Override error classification: Hermes already fails over immediately on rate-limit/billing
  errors when the credential pool cannot rotate (``route_classified_error``). A duplicate override
  could only make it worse.
- Pin kanban cards: pinning removes the fallback chain. No config is written outside the opt-in
  throttle. Routing before a session starts needs an upstream ``resolve_session_route`` hook
  (proposal tracked in the plan).
"""
from __future__ import annotations

import importlib.util
import json
import logging
import math
import sys
import threading
import time
from pathlib import Path
from typing import Optional

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


def _fetch_anthropic_raw():
    """Raw GET to Anthropic's ``/api/oauth/usage``, or ``None`` to skip (no token / non-OAuth).
    Raises on a real fetch failure — callers record ``scoped_error`` and continue. Never logs the
    token or the raw payload. Shared by scoped model limits AND the core-window percent-fix
    override below: one fetch, two derived views.
    """
    import urllib.request
    from agent.anthropic_credentials import resolve_anthropic_token, _is_oauth_token
    token = resolve_anthropic_token()
    if not token or not _is_oauth_token(token):
        return None
    req = urllib.request.Request("https://api.anthropic.com/api/oauth/usage",
                                 headers=_anthropic_headers(token), method="GET")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fetch_anthropic_scoped_outcome():
    """(ok, kind, message, windows|None, overrides|None). Wraps the credential import so an
    import failure maps to a display-only ``scoped_error: {kind: 'import'}`` instead of
    crashing refresh(). ``overrides`` is core.py's ``parse_core_overrides`` fix for the
    ``Current session``/``Current week`` percent-unit bug (see core.py for why); ``None`` means
    skipped, same as ``windows``.
    """
    core = _core()
    try:
        payload = _fetch_anthropic_raw()
    except ImportError as exc:
        return False, "import", f"{type(exc).__name__}: {exc}", None, None
    except Exception as exc:
        return False, "scoped_error", f"{type(exc).__name__}: {str(exc)[:200]}", None, None
    if payload is None:
        return True, None, "", None, None  # skipped: no token / not OAuth, not an error
    return True, None, "", core.parse_scoped_limits(payload), core.parse_core_overrides(payload)


def _snap_dict(core, snap, now):
    """Normalize a core snapshot (object or dict or None) to the plain dict shape."""
    return core.normalize_snapshot(snap, now)


# ── Codex reset-credit list + targeted consume ────────────────────────────────────────────────
# Hermes exposes only a count of banked credits and an untargeted redeem. The list endpoint gives
# each credit's expires_at, and the consume endpoint accepts a credit_id (the Codex CLI's
# consume_rate_limit_reset_credit_by_id), so both calls are made here on Hermes's credential,
# URL and outcome helpers.
_CODEX_HELPERS = ("_resolve_codex_usage_credentials", "_codex_backend_urls", "_codex_headers",
                  "_codex_reset_outcome")


def _codex_api():
    """``agent.account_usage`` when it has every helper the calls below need, else None."""
    try:
        from agent import account_usage as au
    except Exception:
        return None
    return au if all(callable(getattr(au, n, None)) for n in _CODEX_HELPERS) else None


def _codex_call(au, fn):
    """Run ``fn(urls, headers)`` with Codex credentials, retrying once with refreshed ones on 401."""
    import httpx
    token, base, account = au._resolve_codex_usage_credentials(None, None)
    for attempt in range(2):
        try:
            return fn(au._codex_backend_urls(base), au._codex_headers(token, account))
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 401 or attempt:
                raise
            token, base, account = au._resolve_codex_usage_credentials(None, None, force_refresh=True)


def _fetch_codex_reset_credits() -> dict:
    """GET the banked reset-credit list (raises on failure)."""
    au = _codex_api()
    if au is None:
        raise ImportError("agent.account_usage lacks the Codex helpers")
    import httpx

    def get(urls, headers):
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(urls[1], headers=headers)
            resp.raise_for_status()
            return resp.json()
    return _codex_call(au, get)


def _redeem_codex_credit(credit_id: str):
    """POST consume for one specific credit, with force semantics (no exhaustion guard): the
    caller has decided the credit would otherwise lapse. Returns ``(status, message)``; never raises."""
    import uuid
    au = _codex_api()
    if au is None:
        return "unavailable", "agent.account_usage lacks the Codex helpers"
    request_id = str(uuid.uuid4())   # reused across the 401 retry: the backend dedupes on it

    def consume(urls, headers):
        import httpx
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(urls[2], headers={**headers, "Content-Type": "application/json"},
                               json={"redeem_request_id": request_id, "credit_id": credit_id})
            resp.raise_for_status()
            return resp.json() or {}
    try:
        body = _codex_call(au, consume)
        result = au._codex_reset_outcome(body, 1)
        return result.status, result.message
    except Exception as exc:
        return "unavailable", f"{type(exc).__name__}: {str(exc)[:200]}"


def refresh(now: float | None = None, snapshots: dict | None = None, drain: bool = False,
            outcomes: dict | None = None, only: set | None = None) -> list[str]:
    core = _core()
    now = time.time() if now is None else now
    fetching_live = snapshots is None
    if snapshots is None:
        from agent.account_usage import fetch_account_usage
        snapshots = {}
        outcomes = {}
        cfg0 = _cfg()
        with core.locked_state(core.state_path()) as st0:  # backoff decision + its one alert
            due = {p: core.fetch_due(st0, p, now, cfg0) for p in core.WATCHED}
        for p in core.WATCHED:
            if only is not None and p not in only:
                continue
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
    core_overrides = None
    if core_anthropic_ok:
        prior = core.read_state(core.state_path())
        pstate = prior["providers"].get("anthropic") or {}
        last_scoped_fetch = ((pstate.get("fetch") or {}).get("scoped") or {}).get("scoped_fetched_at")
        throttled = last_scoped_fetch is not None and now - last_scoped_fetch < SCOPED_THROTTLE_S
        if not throttled:
            ok, kind, message, windows, overrides = _fetch_anthropic_scoped_outcome()
            scoped_outcome = (ok, kind, message)
            if ok and windows is not None:
                scoped_windows = windows
            if ok and overrides is not None:
                core_overrides = overrides

    # Cache stats: read-only state.db aggregate, collected before taking the state lock.
    cache_outcome = core.collect_cache_outcome(core.state_db_path(), now, cfg)

    # Banked reset-credit list (expiry per credit), only on a live fetch that reached Codex.
    credits_outcome = None
    if fetching_live and outcomes.get("openai-codex", (False,))[0]:
        due_credits = core.reset_credits_due(core.read_state(core.state_path()), now, cfg)
        if due_credits:
            try:
                credits_outcome = (True, "", core.parse_reset_credits(_fetch_codex_reset_credits()))
            except Exception as exc:
                credits_outcome = (False, f"{type(exc).__name__}: {str(exc)[:200]}", None)

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
                if core_overrides is not None:
                    scoped_slot["core_overrides"] = core_overrides
                    scoped_slot["core_overrides_fetched_at"] = now
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
            # Apply fresh-or-carried core-window percent overrides (see core.parse_core_overrides):
            # core's own fraction-vs-percent heuristic misreads Anthropic's current ``five_hour``/
            # ``seven_day`` payload, so rewrite ``used_percent`` on the matching labels using this
            # plugin's own raw fetch of the same endpoint. Carry-forward follows the same
            # stale_after_s rule as scoped windows above.
            override_age = now - float(scoped_slot.get("core_overrides_fetched_at") or 0)
            overrides_to_apply = scoped_slot.get("core_overrides") or {}
            if overrides_to_apply and override_age <= cfg["stale_after_s"]:
                snap = snapshots.get("anthropic")

                def _wget2(w, k, d=None):
                    return w.get(k, d) if isinstance(w, dict) else getattr(w, k, d)

                def _woverride(w, pct):
                    """New window with ``used_percent`` replaced; windows may be plain dicts or
                    the frozen ``AccountUsageWindow`` dataclass, which rejects ``setattr``."""
                    if isinstance(w, dict):
                        return {**w, "used_percent": pct}
                    import dataclasses
                    if dataclasses.is_dataclass(w) and not isinstance(w, type):
                        return dataclasses.replace(w, used_percent=pct)
                    import copy
                    w2 = copy.copy(w)
                    setattr(w2, "used_percent", pct)
                    return w2

                if snap is not None:
                    windows_list = list(snap.get("windows") if isinstance(snap, dict) else snap.windows) or []
                    windows_list = [
                        _woverride(w, overrides_to_apply[_wget2(w, "label")])
                        if _wget2(w, "label") in overrides_to_apply else w
                        for w in windows_list
                    ]
                    if isinstance(snap, dict):
                        snap = dict(snap)
                        snap["windows"] = windows_list
                        snapshots["anthropic"] = snap
                    else:
                        import dataclasses
                        if dataclasses.is_dataclass(snap) and not isinstance(snap, type):
                            snapshots["anthropic"] = dataclasses.replace(snap, windows=windows_list)
                        else:
                            import copy
                            snap2 = copy.copy(snap)
                            snap2.windows = windows_list
                            snapshots["anthropic"] = snap2
        core.apply_refresh(st, snapshots, now, cfg)
        core.apply_cache(st, cache_outcome, now, cfg)
        if credits_outcome is not None:
            ok, message, parsed = credits_outcome
            core.note_fetch(st, "openai-codex", slot="reset_credits", ok=ok, kind=None if ok else "fetch_error",
                            message=message, now=now, cfg=cfg)
            if ok:
                # Stamped with this refresh's start (<= the GET start), never apply time: a
                # peer's redemption settled mid-GET must still invalidate this list.
                core.apply_reset_credits(st, parsed, now)

        # Auto-redeem banked Codex reset credits. Claim (decision + cap-slot reservation) is
        # atomic in this locked section, so overlapping refreshes (cron + startup +
        # status --refresh) cannot both pass the cap check and double-redeem. The redeem
        # itself is a network call and runs AFTER the lock releases, against the reserved
        # slot; settle_auto_redeem then keeps the slot on success or rolls it back on
        # failure, in a final locked section. Never passes force: the runtime guard refuses
        # below 100% usage on every window, and the backend's own nothing_to_reset is a
        # third layer — a stale snapshot cannot spend a credit into a half-used window.
        redeem, reason = core.claim_auto_redeem(st, now, cfg)
        credit_id = None
        if not redeem:
            # Not exhausted: spend a credit that would otherwise lapse (targeted, forced).
            redeem, reason, credit_id = core.claim_expiry_redeem(
                st, now, cfg, supports_credit_id=_codex_api() is not None)
            if not redeem:
                return core.drain_pending(st) if drain else []

    if credit_id is not None:
        outcome, message = _redeem_codex_credit(credit_id)
        logger.info("quota-router: expiry auto-redeem attempt (%s): %s — %s", reason, outcome, message)
        with core.locked_state(core.state_path()) as st:
            core.settle_expiry_redeem(st, now, reason, outcome, credit_id, settled_at=time.time())
            return core.drain_pending(st) if drain else []

    try:
        from agent.account_usage import redeem_codex_reset_credit
        result = redeem_codex_reset_credit()
        outcome, message = result.status, result.message
    except Exception as exc:  # pragma: no cover - redeem is never-raise by design
        outcome, message = "error", f"{type(exc).__name__}: {exc}"
    logger.info("quota-router: auto-redeem attempt (%s): %s — %s", reason, outcome, message)

    with core.locked_state(core.state_path()) as st:
        core.settle_auto_redeem(st, now, reason, outcome, settled_at=time.time())
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


def export_line(since: Optional[float] = None) -> str:
    """Build the ``QRJSON <payload>`` line for ``export``. Read-only: never fetches, never
    drains the queue, never writes state. ``since`` filters samples to ``ts > since``."""
    import socket
    core = _core()
    payload = core.export_payload(core.read_state(core.state_path()), since, socket.gethostname())
    return "QRJSON " + json.dumps(payload)


def _export_line_with_since(since: Optional[float]) -> str:
    return export_line(since)


def _since_value(raw: Optional[str]) -> Optional[float]:
    """Parse ``--since``; ``None`` means omitted. Raises ``ValueError`` for anything that is
    not a finite number (the CLI turns that into one ``quota-router: ...`` stderr line)."""
    if raw is None:
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"--since must be a unix timestamp, got {raw!r}") from None
    if not math.isfinite(v):
        raise ValueError(f"--since must be a finite number, got {raw!r}")
    return v


def _cli_setup(sub):
    sp = sub.add_subparsers(dest="qr_cmd")
    s = sp.add_parser("status", help="Show quota state (add --refresh to fetch live usage first)")
    s.add_argument("--refresh", action="store_true")
    s.add_argument("--json", action="store_true", help="Print one QRJSON <payload> line for the desktop widget")
    sp.add_parser("refresh", help="Fetch live usage; print only new alerts/transitions (cron-friendly)")
    sp.add_parser("path", help="Print the host-level state file path")
    th = sp.add_parser("throttle", help="Host-local burn-rate throttle (ladder levels; fast/constrained by default)")
    tsp = th.add_subparsers(dest="qr_throttle_cmd")
    tsp.add_parser("apply", help="Decide the level and reconcile throttled profiles (cron path)")
    ts = tsp.add_parser("status", help="Show the throttle level, reasons and per-profile results")
    ts.add_argument("--json", action="store_true")
    h = tsp.add_parser("hold", help="Force a level (a ladder level name) for a while (overrides everything)")
    h.add_argument("level", nargs="?")
    h.add_argument("--for", dest="duration", default="2h")
    h.add_argument("--clear", action="store_true")
    d = tsp.add_parser("disable", help="Restore a level (default: the first, fast for levels:) and stop "
                                       "following the policy")
    d.add_argument("--level", default=None)
    d.add_argument("--clear", action="store_true", help="Resume following the policy")
    a1 = tsp.add_parser("apply-one", help="(internal) reconcile THIS profile's config to a level")
    a1.add_argument("level")
    a1.add_argument("--levels-json", required=True)
    a1.add_argument("--kanban", action="store_true")
    sr = tsp.add_parser("stamp-resume", help="Mark throttled profiles' TUI/desktop chats to follow config on "
                                            "resume (needs throttle.stamp_resume: true; cron path)")
    sr.add_argument("--dry-run", action="store_true", help="Report what would be marked; write nothing "
                                                           "(works with stamp_resume off)")
    sr.add_argument("--json", action="store_true")
    e = sp.add_parser("export", help="Print one QRJSON line of history samples for downstream "
                                     "consumers (read-only; account windows only)")
    e.add_argument("--since", default=None, metavar="UNIX_TS",
                   help="only points with ts > this unix timestamp (strict)")
    e.add_argument("--json", action="store_true",
                   help="accepted for symmetry; the output is the QRJSON line either way")


def _throttle_handler(args) -> int:
    core, thr = _core(), _throttle()
    cmd = getattr(args, "qr_throttle_cmd", None) or "status"
    now = time.time()
    if cmd == "apply-one":
        print(thr.child_main(args.level, args.levels_json, args.kanban, now))
        return 0
    t = _throttle_cfg()
    path = core.state_path()
    if cmd == "stamp-resume":
        return _stamp_resume(args, core, thr, t, path, now)
    if cmd == "status":
        s = thr.throttle_status(core.read_state(path), t, core.root_home(), now)
        print("QRTHROTTLE_STATUS " + json.dumps(s) if args.json else thr.render_throttle_status(s))
        return 0
    if cmd in ("hold", "disable"):
        names = core.throttle_level_names(t)
        try:
            with core.locked_state(path) as st:
                if cmd == "hold" and args.clear:
                    core.throttle_clear_hold(st)
                elif cmd == "hold":
                    core.throttle_hold(st, args.level or "", now + thr.parse_duration(args.duration), now, names)
                else:
                    core.throttle_disable(st, None if args.clear else (args.level or names[0]), now, names)
        except ValueError as exc:
            print(f"quota-router: {exc}")
            return 2
    # hold/disable apply at once but leave the queue (their transition line included) for the cron
    # to deliver; only the cron path drains (README "Alert queue").
    for ln in thr.run_apply(path, _cfg(), t, thr.subprocess_runner, time.time(), drain=cmd == "apply"):
        print(ln)
    return 0


def _stamp_resume(args, core, thr, t: dict, path, now: float) -> int:
    """``throttle stamp-resume``: a silent no-op unless the throttle is enabled with ``stamp_resume: true``
    and a valid config (``--dry-run`` ignores the switches but not an invalid config; no ``throttle`` block
    at all is an opt-out either way). Prints ``quota-router:`` lines for stamps
    made, and for a refusal or a per-profile failure once per distinct message (the cron repeats)."""
    dry = bool(args.dry_run)
    if not t.get("configured"):
        print("resume stamp: off (no throttle block)")
        return 0
    errs = core.validate_throttle(t, _cfg())
    if not dry and (errs or not t.get("enabled") or t.get("stamp_resume") is not True):
        why = errs[0] if errs else "needs throttle.enabled and throttle.stamp_resume: true"
        print(f"resume stamp: off ({why})")
        return 0
    if dry and errs:
        print(f"resume stamp: throttle config invalid ({errs[0]})")
        return 2
    res = thr.run_stamp(core.root_home(), t, dry_run=dry)
    print("QRSTAMP " + json.dumps(res) if args.json else thr.render_stamp(res))
    if dry:
        return 0
    lines = thr.stamp_alerts(res)
    failures = [ln for ln in lines if " marked " not in ln]
    with core.locked_state(path) as st:
        rec = st.setdefault("throttle", {}).setdefault("resume_stamp", {})
        seen = set(rec.get("alerted") or [])
        rec["alerted"] = failures
        rec["last"] = {"at": now, "refused": res["refused"],
                       "stamped": {p: r.get("stamp", 0) for p, r in res["profiles"].items()}}
    for ln in lines:
        if ln not in failures or ln not in seen:
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
    if cmd == "export":
        try:
            since = _since_value(getattr(args, "since", None))
        except ValueError as exc:
            print(f"quota-router: {exc}", file=sys.stderr)
            return 2
        print(_export_line_with_since(since))
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


# ── startup refresh ────────────────────────────────────────────────────────────
# On session start, fold a live usage refresh in so /quota has data without waiting for the
# cron. Runs in a daemon thread (session start must never block on a network fetch). Two
# guards keep the usage endpoints unhammered:
# - STARTUP_REFRESH_THROTTLE_S: a provider is fetched only if it has no successful core
#   fetch younger than this — so a permanently broken credential (revoked, not configured)
#   never forces repeated fetches of the HEALTHY provider; each provider is judged alone.
# - _STARTUP_LOCK: concurrent session starts (in-process fan-out) share one refresh;
#   the thread that wins the non-blocking lock fetches, the others skip entirely.
#   Note the lock is per-process; cross-process starts are bounded by the throttle
#   and by core.fetch_due backoff instead.
# The plugin's per-provider fetch backoff (core.fetch_due) still applies inside refresh().
STARTUP_REFRESH_THROTTLE_S = 10 * 60
_STARTUP_LOCK = threading.Lock()


def _startup_refresh_due(core, now: float) -> set:
    """Providers a startup refresh should fetch now (possibly empty). A provider is due
    when it has no successful core fetch younger than STARTUP_REFRESH_THROTTLE_S. Any
    state-read problem counts everything as due — fail open toward fetching."""
    try:
        providers = core.read_state(core.state_path()).get("providers") or {}
        due = set()
        for p in core.WATCHED:
            fslot = ((providers.get(p) if isinstance(providers, dict) else None) or {}).get("fetch") or {}
            last_ok = fslot.get("core", {}).get("last_ok_at") if isinstance(fslot, dict) else None
            try:
                fresh = last_ok is not None and now - float(last_ok) <= STARTUP_REFRESH_THROTTLE_S
            except (TypeError, ValueError):
                fresh = False
            if not fresh:
                due.add(p)
        return due
    except Exception:
        return set(core.WATCHED)


def _on_session_start(session_id=None, **_kw):
    try:
        core = _core()
        due = _startup_refresh_due(core, time.time())
        if not due:
            return
        if not _STARTUP_LOCK.acquire(blocking=False):
            return  # a startup refresh is already in flight; it covers us
        try:
            threading.Thread(target=_startup_refresh_worker, name="quota-router-startup-refresh",
                             daemon=True).start()
        except Exception as exc:
            _STARTUP_LOCK.release()
            logger.debug("quota-router: startup refresh thread failed to start: %s", exc)
    except Exception as exc:
        logger.debug("quota-router: startup refresh hook failed: %s", exc)


def _startup_refresh_worker():
    try:
        core = _core()
        due = _startup_refresh_due(core, time.time())  # re-check: state may have moved on
        if not due:
            return
        # A provider inside its failure backoff window (core.fetch_due False) is skipped by
        # refresh() and simply waits for the cron / a later session start — expected and quiet.
        refresh(drain=False, only=due)  # alerts stay queued for the cron per README
    except Exception as exc:
        logger.debug("quota-router: startup refresh failed: %s", exc)
    finally:
        _STARTUP_LOCK.release()


def register(ctx):
    ctx.register_hook("api_request_error", _on_api_error)
    ctx.register_hook("post_api_request", _on_api_success)
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_cli_command("quota-router", "Quota-aware routing state for anthropic / openai-codex",
                             _cli_setup, _cli_handler)
    ctx.register_command("quota", _slash, "Show provider quota state (/quota refresh to fetch live)",
                         args_hint="[refresh]")
