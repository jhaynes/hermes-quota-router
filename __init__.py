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

def refresh(now: float | None = None, snapshots: dict | None = None, drain: bool = False,
            outcomes: dict | None = None) -> list[str]:
    core = _core()
    now = time.time() if now is None else now
    if snapshots is None:
        from agent.account_usage import fetch_account_usage
        snapshots = {}
        outcomes = {}
        for p in core.WATCHED:
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
    with core.locked_state(core.state_path()) as st:
        for p, (ok, kind, message) in outcomes.items():
            core.note_fetch(st, p, ok=ok, kind=kind, message=message, now=now, cfg=cfg)
        core.apply_refresh(st, snapshots, now, cfg)
        return core.drain_pending(st) if drain else []


def status_text(fetch: bool = False) -> str:
    core = _core()
    if fetch:
        refresh(drain=False)
    return core.render_status(core.read_state(core.state_path()), time.time(), _cfg())


def _cli_setup(sub):
    sp = sub.add_subparsers(dest="qr_cmd")
    s = sp.add_parser("status", help="Show quota state (add --refresh to fetch live usage first)")
    s.add_argument("--refresh", action="store_true")
    sp.add_parser("refresh", help="Fetch live usage; print only new alerts/transitions (cron-friendly)")
    sp.add_parser("path", help="Print the host-level state file path")


def _cli_handler(args):
    cmd = getattr(args, "qr_cmd", None) or "status"
    if cmd == "refresh":
        for ln in refresh(drain=True):
            print(ln)
        return 0
    if cmd == "path":
        print(_core().state_path())
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
