"""quota-router core: host-level provider quota state, tiers with hysteresis, mode, alerts.

Pure functions over plain dicts plus a small locked JSON store. No Hermes imports at module level,
so the policy is unit-testable in isolation.

State lives at the ROOT Hermes home (not the profile home) so every profile on the host and the
gateway share one view: ``<root>/cache/quota-router/state.json``.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

WATCHED = ("anthropic", "openai-codex")
DEFAULTS = {
    "enter_tight": 85.0,      # any window >= this -> tight
    "leave_tight": 75.0,      # every window < this to leave tight
    "enter_exhausted": 97.0,  # any window >= this -> exhausted (percent-based)
    "leave_exhausted": 90.0,
    "min_dwell_s": 15 * 60,   # minimum time in a tier before de-escalating
    "ignore_reset_within_s": 30 * 60,  # a window resetting sooner than this cannot ESCALATE a tier
    "stale_after_s": 30 * 60,  # snapshots older than this are ignored (fail open to normal)
    "block_ttl_s": 6 * 3600,   # observed hard-block without a known reset expires after this
    "alert_thresholds": [75.0, 90.0],
}
TIER_ORDER = {"ok": 0, "tight": 1, "exhausted": 2}


# ── paths / store ──────────────────────────────────────────────────────────────

def root_home(hermes_home: Optional[str | Path] = None) -> Path:
    """Root Hermes home for this host: a profile home ``<root>/profiles/<name>`` maps to ``<root>``."""
    if hermes_home is None:
        try:
            from hermes_constants import get_hermes_home
            hermes_home = get_hermes_home()
        except Exception:
            hermes_home = os.environ.get("HERMES_HOME") or Path.home() / ".hermes"
    p = Path(hermes_home).expanduser()
    return p.parent.parent if p.parent.name == "profiles" else p


def state_path(hermes_home: Optional[str | Path] = None) -> Path:
    return root_home(hermes_home) / "cache" / "quota-router" / "state.json"


PENDING_CAP = 50


def empty_state() -> dict:
    return {"version": 1, "providers": {}, "mode": "normal", "mode_since": 0.0, "alerts_sent": {},
            "transitions": [], "blocks": {}, "errors": [], "pending": []}


def _is_deliverable(line: str) -> bool:
    return line.startswith("⚠️") or line.startswith("quota-router:")


def queue(state: dict, *lines: str) -> None:
    """Append deliverable lines to ``state['pending']``: enforce a deliverable prefix, dedupe
    already-pending lines, and cap at ``PENDING_CAP`` evicting oldest non-alert lines first."""
    pending = list(state.get("pending") or [])
    for line in lines:
        if not line:
            continue
        if not _is_deliverable(line):
            line = f"quota-router: {line}"
        if line in pending:
            continue
        pending.append(line)
    while len(pending) > PENDING_CAP:
        for i, line in enumerate(pending):
            if not line.startswith("⚠️"):
                pending.pop(i)
                break
        else:
            pending.pop(0)
    state["pending"] = pending


def drain_pending(state: dict) -> list[str]:
    out, state["pending"] = list(state.get("pending") or []), []
    return out


@contextlib.contextmanager
def locked_state(path: Path):
    """Exclusive lock + read-modify-write. Yields the state dict; written back atomically on exit."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(".lock")
    with open(lock, "a+") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            state = read_state(path)
            yield state
            tmp = path.with_suffix(f".tmp.{os.getpid()}")
            tmp.write_text(json.dumps(state, indent=1, sort_keys=True, default=str))
            os.replace(tmp, path)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def read_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        if isinstance(data, dict) and data.get("version") == 1:
            base = empty_state()
            base.update(data)
            return base
    except (OSError, ValueError):
        pass
    return empty_state()


# ── snapshot normalisation ─────────────────────────────────────────────────────

def _epoch(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        return value.timestamp() if value.tzinfo else value.replace(tzinfo=timezone.utc).timestamp()
    try:
        s = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt.timestamp() if dt.tzinfo else dt.replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def normalize_snapshot(snapshot: Any, now: float) -> dict:
    """AccountUsageSnapshot | its JSON dict -> {'fetched_at', 'windows': [{label, used, resets_at}], ...}."""
    if snapshot is None:
        return {"fetched_at": now, "windows": [], "unavailable": "no snapshot"}
    get = (lambda k, d=None: snapshot.get(k, d)) if isinstance(snapshot, dict) else (
        lambda k, d=None: getattr(snapshot, k, d))
    windows = []
    for w in get("windows") or ():
        wget = (lambda k, d=None: w.get(k, d)) if isinstance(w, dict) else (lambda k, d=None: getattr(w, k, d))
        used = wget("used_percent")
        if used is None:
            continue
        windows.append({"label": str(wget("label") or "?"), "used": float(used),
                        "resets_at": _epoch(wget("resets_at", None) or wget("reset_at", None))})
    details = [str(d) for d in (get("details") or ())]
    return {"fetched_at": _epoch(get("fetched_at")) or now, "windows": windows,
            "unavailable": get("unavailable_reason"), "plan": get("plan"), "details": details}


# ── policy ─────────────────────────────────────────────────────────────────────

def percent_tier(windows: Iterable[dict], current: str, now: float, cfg: dict) -> str:
    """Tier implied by usage percentages alone, with enter/leave hysteresis per level."""
    escalating = [w for w in windows
                  if not (w.get("resets_at") and w["resets_at"] - now < cfg["ignore_reset_within_s"])]
    all_windows = list(windows)
    peak_escalate = max((w["used"] for w in escalating), default=0.0)
    peak_all = max((w["used"] for w in all_windows), default=0.0)
    if peak_escalate >= cfg["enter_exhausted"]:
        return "exhausted"
    if current == "exhausted" and peak_all >= cfg["leave_exhausted"]:
        return "exhausted"
    if peak_escalate >= cfg["enter_tight"]:
        return "tight"
    if current in ("tight", "exhausted") and peak_all >= cfg["leave_tight"]:
        return "tight"
    return "ok"


def active_block(block: Optional[dict], now: float) -> bool:
    return bool(block) and float(block.get("until") or 0) > now


def compute_tier(pstate: dict, block: Optional[dict], now: float, cfg: dict) -> str:
    current = pstate.get("tier", "ok")
    snap = pstate.get("snapshot") or {}
    fresh = bool(snap) and now - float(snap.get("fetched_at") or 0) <= cfg["stale_after_s"]
    target = percent_tier(snap.get("windows") or [], current, now, cfg) if fresh else "ok"
    if active_block(block, now):
        target = "exhausted"
    # De-escalation waits out the dwell time; escalation is immediate. Stale data drops at once
    # (fail open to normal) unless an observed block still holds.
    if fresh and TIER_ORDER[target] < TIER_ORDER.get(current, 0):
        if now - float(pstate.get("tier_since") or 0) < cfg["min_dwell_s"]:
            return current
    return target


def compute_mode(tiers: dict) -> str:
    bad = [p for p in WATCHED if tiers.get(p, "ok") != "ok"]
    if len(bad) == len(WATCHED):
        return "internal"
    return "shift" if bad else "normal"


def block_until(windows: list, observed_at: float, cfg: dict) -> float:
    """Reset time of the most-used window at/after the block, else observed_at + block_ttl_s."""
    future = [w for w in windows if w.get("resets_at") and w["resets_at"] > observed_at]
    if future:
        top = max(future, key=lambda w: w["used"])
        return float(top["resets_at"])
    return observed_at + cfg["block_ttl_s"]


def alert_lines(provider: str, windows: list, sent: dict, cfg: dict) -> list[str]:
    """New threshold crossings, once per (provider, window, reset cycle, threshold). Mutates ``sent``."""
    out = []
    for w in windows:
        cycle = str(int(w["resets_at"])) if w.get("resets_at") else "none"
        for t in sorted(cfg["alert_thresholds"]):
            key = f"{provider}|{w['label']}|{cycle}|{int(t)}"
            if w["used"] >= t and key not in sent:
                sent[key] = True
                reset = (datetime.fromtimestamp(w["resets_at"]).strftime("%a %H:%M")
                         if w.get("resets_at") else "unknown")
                out.append(f"⚠️ {provider} {w['label']} at {w['used']:.0f}% (≥{int(t)}%), resets {reset}")
    return out


def prune_alerts(sent: dict, now: float) -> None:
    for key in list(sent):
        cycle = key.split("|")[2]
        if cycle != "none" and float(cycle) < now:
            sent.pop(key, None)


def apply_refresh(state: dict, snapshots: dict, now: float, cfg: dict) -> list[str]:
    """Fold fresh snapshots into ``state``; return human lines for alerts/transitions (empty = quiet)."""
    lines: list[str] = []
    prune_alerts(state["alerts_sent"], now)
    for provider, snap in snapshots.items():
        pstate = state["providers"].setdefault(provider, {"tier": "ok", "tier_since": now})
        pstate["snapshot"] = normalize_snapshot(snap, now)
        lines += alert_lines(provider, pstate["snapshot"]["windows"], state["alerts_sent"], cfg)
    lines += recompute(state, now, cfg)
    queue(state, *lines)
    return lines


def recompute(state: dict, now: float, cfg: dict) -> list[str]:
    lines = []
    for provider in WATCHED:
        pstate = state["providers"].setdefault(provider, {"tier": "ok", "tier_since": now})
        block = state["blocks"].get(provider)
        if block and not active_block(block, now):
            state["blocks"].pop(provider, None)
            block = None
        tier = compute_tier(pstate, block, now, cfg)
        if tier != pstate.get("tier"):
            lines.append(f"quota-router: {provider} {pstate.get('tier', 'ok')} → {tier}")
            pstate["tier"], pstate["tier_since"] = tier, now
    mode = compute_mode({p: state["providers"][p]["tier"] for p in WATCHED})
    if mode != state.get("mode"):
        lines.append(f"quota-router: mode {state.get('mode', 'normal')} → {mode} ({mode_hint(mode, state)})")
        state["transitions"] = (state.get("transitions") or [])[-49:] + [
            {"at": now, "from": state.get("mode"), "to": mode}]
        state["mode"], state["mode_since"] = mode, now
    return lines


def mode_hint(mode: str, state: dict) -> str:
    if mode == "normal":
        return "all providers healthy"
    bad = [p for p in WATCHED if state["providers"].get(p, {}).get("tier", "ok") != "ok"]
    if mode == "internal":
        return "anthropic and codex both constrained; fallback chains carry work to their final (non-watched) entry"
    return f"{', '.join(bad)} constrained; fallback chains carry work to the next provider"


QUOTA_STATUS = {402, 429}
QUOTA_REASONS = {"rate_limit", "billing"}
HARD_QUOTA_MARKERS = ("usage_limit_reached", "usage limit", "weekly", "quota", "limit reached",
                      "exceeded your", "rate_limit_error")


def record_error(state: dict, *, provider: str, status_code: Any, reason: Any, message: str,
                 model: str, now: float, cfg: dict) -> Optional[str]:
    """Record a provider error; a quota-shaped one on a watched provider becomes a block. Returns a
    transition line when the block is new."""
    provider = (provider or "").strip().lower()
    reason = str(getattr(reason, "value", reason) or "").lower()
    try:
        status = int(status_code) if status_code is not None else None
    except (TypeError, ValueError):
        status = None
    quotaish = status in QUOTA_STATUS or reason in QUOTA_REASONS
    if not quotaish:
        return None
    msg = (message or "")[:400]
    state["errors"] = (state.get("errors") or [])[-29:] + [
        {"at": now, "provider": provider, "model": model, "status": status, "reason": reason, "message": msg}]
    if provider not in WATCHED:
        return None
    hard = status == 402 or reason == "billing" or any(m in msg.lower() for m in HARD_QUOTA_MARKERS)
    if not hard:
        return None
    windows = ((state["providers"].get(provider) or {}).get("snapshot") or {}).get("windows") or []
    until = block_until(windows, now, cfg)
    had = active_block(state["blocks"].get(provider), now)
    state["blocks"][provider] = {"since": now, "until": until, "status": status, "reason": reason,
                                 "message": msg[:200]}
    return None if had else f"quota-router: observed hard quota block on {provider} until {datetime.fromtimestamp(until):%a %H:%M}"


def clear_block_on_success(state: dict, provider: str, now: float) -> bool:
    """A served request is authoritative: drop the block AND the dwell hold it caused, so the
    tier falls straight back to what usage percentages say."""
    provider = (provider or "").strip().lower()
    if provider in state["blocks"] and active_block(state["blocks"][provider], now):
        state["blocks"].pop(provider, None)
        pstate = state["providers"].setdefault(provider, {"tier": "ok", "tier_since": now})
        pstate["tier_since"] = 0.0
        return True
    return False


def render_status(state: dict, now: float, cfg: dict) -> str:
    out = [f"mode: {state.get('mode', 'normal')}"
           + (f" (since {datetime.fromtimestamp(state['mode_since']):%a %H:%M})" if state.get("mode_since") else "")]
    for p in WATCHED:
        ps = state["providers"].get(p) or {}
        snap = ps.get("snapshot") or {}
        age = now - float(snap.get("fetched_at") or 0) if snap else None
        stale = age is None or age > cfg["stale_after_s"]
        out.append(f"{p}: tier={ps.get('tier', 'ok')}"
                   + (f"  snapshot {int(age // 60)}m old{' (STALE — ignored)' if stale else ''}" if age is not None else "  no snapshot"))
        for w in snap.get("windows") or []:
            reset = datetime.fromtimestamp(w["resets_at"]).strftime("%a %H:%M") if w.get("resets_at") else "?"
            out.append(f"    {w['label']:<16} {w['used']:5.1f}%  resets {reset}")
        for d in snap.get("details") or []:
            out.append(f"    {d}")
        b = state["blocks"].get(p)
        if active_block(b, now):
            out.append(f"    OBSERVED BLOCK until {datetime.fromtimestamp(b['until']):%a %H:%M}: {b.get('message', '')[:120]}")
    errs = state.get("errors") or []
    if errs:
        out.append("recent quota-shaped errors:")
        for e in errs[-5:]:
            out.append(f"    {datetime.fromtimestamp(e['at']):%a %H:%M} {e['provider']} {e.get('status')} "
                       f"{e.get('reason')}: {e.get('message', '')[:100]}")
    return "\n".join(out)
