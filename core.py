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
import math
import os
import re
import sqlite3
import sys
import time
import urllib.parse
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
    "cycle_tolerance_s": 10 * 60,  # resets_at moving more than this starts a new history cycle
    # Fetch backoff (v2): after backoff_failures non-ok core fetches within backoff_window_s, a
    # provider is fetched at most every backoff_interval_s until a fetch succeeds.
    "backoff_failures": 3,
    "backoff_window_s": 60 * 60,
    "backoff_interval_s": 15 * 60,
    # Forecast (v2 B5). Window labels are core's; kind picks the rate maths below.
    # openai-codex: core labels its 5 h primary window "Session" and its secondary "Weekly".
    "forecast_windows": {"anthropic": {"Current session": "session", "Current week": "weekly",
                                       "Opus week": "weekly"},
                         "openai-codex": {"Session": "session", "Weekly": "weekly"}},
    "forecast_kinds": {
        # long rate: least squares over the whole current cycle
        "session": {"period_s": 5 * 3600, "short_lookback_s": 30 * 60, "long_rate": "lsq"},
        # long rate: cycle average (used / elapsed cycle time) so quiet nights count
        "weekly": {"period_s": 7 * 24 * 3600, "short_lookback_s": 6 * 3600, "long_rate": "cycle_average"},
    },
    # The short-rate projection extrapolates r_short no further than this many seconds
    # ahead (a recent burst decays within hours or days, not across a whole weekly tail).
    "forecast_short_horizon_s": 24 * 3600,
    "forecast_min_points": 4,
    "forecast_min_span_s": 40 * 60,
    "forecast_min_used": 20.0,
    # cache efficiency (display + alert only; never feeds tier/mode/blocks)
    "cache_alert_below": 80.0,             # provider 24h hit % below this ...
    "cache_alert_min_tokens": 20_000_000,  # ... on at least this much 24h input -> alert
    "cache_write_alert_tokens": 10_000_000,  # one session writing this much in 24h ...
    "cache_write_alert_below": 50.0,       # ... at a hit % below this -> alert
    # Auto-redeem banked Codex reset credits (feature). A banked reset restores the FULL 5h +
    # weekly allowance instantly, so its value is realised the moment it is redeemed — but the
    # weekly window is the scarce one (it does not self-heal for days), while a dead 5h window
    # empties itself in hours. Redemption therefore fires ONLY on weekly exhaustion; a dead
    # 5h window alone never spends a credit — the throttle ladder covers that case on GLM.
    # The redeem never passes force: the runtime client-side guard
    # (account_usage._codex_reset_guard) refuses unless the BUSIEST window is at 100%, and
    # the backend's own nothing_to_reset is a third layer. The plugin's weekly gate is what
    # keeps the 5h-window case out entirely (see claim_auto_redeem).
    "auto_reset": None,                    # None = off; {"weekly_threshold": 97.0} to enable
    "auto_reset_max_redeems_per_week": 1,  # guard against repeated bad data spending the bank
    # Each banked credit expires (30 days after grant on the live account). Expiry-driven
    # redemption (claim_expiry_redeem) spends the soonest-expiring credit, by id and with force,
    # as soon as it would otherwise lapse: it expires before the current Weekly window resets
    # (so it is unusable in any later window) and Weekly is at least auto_reset.expiry_min_used
    # (default 10%) used. Waiting longer restores nothing — inside this window the credit is
    # lost either way, and spending early returns the fresh allowance with the whole remaining
    # window to use it. Set auto_reset.expiry_lead_s to a positive number of seconds to cap how
    # early it may fire (0 = off, the historical 24 h behaviour is expiry_lead_s: 86400).
    # The credit list is re-fetched this often:
    "reset_credits_refresh_s": 60 * 60,
}
EXPIRY_LEAD_S = 0.0
EXPIRY_MIN_USED = 10.0
EXPIRY_RETRY_S = 30 * 60        # after a failed expiry-driven attempt
EXPIRY_INFLIGHT_S = 15 * 60     # a claim older than this cannot still be in flight
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


def state_db_path(hermes_home: Optional[str | Path] = None) -> Path:
    """Hermes' session database for this host (read-only input to the cache section)."""
    return root_home(hermes_home) / "state.db"


PENDING_CAP = 50
HISTORY_MAX_AGE_S = 7 * 24 * 3600
HISTORY_MAX_POINTS = 2500     # per (provider, window)
FETCH_LOG_MAX_AGE_S = 24 * 3600
FETCH_LOG_MAX = 1000


def empty_state() -> dict:
    return {"version": 1, "providers": {}, "mode": "normal", "mode_since": 0.0, "alerts_sent": {},
            "transitions": [], "blocks": {}, "errors": [], "pending": [],
            "history": {}, "fetch_log": []}


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
                        "resets_at": _epoch(wget("resets_at", None) or wget("reset_at", None)),
                        "scope": wget("scope", None),
                        **({"stale": True} if wget("stale", False) else {})})
    details = [str(d) for d in (get("details") or ())]
    return {"fetched_at": _epoch(get("fetched_at")) or now, "windows": windows,
            "unavailable": get("unavailable_reason"), "plan": get("plan"), "details": details}


# ── policy ─────────────────────────────────────────────────────────────────────

def is_account_window(window: dict) -> bool:
    """Account-wide quota, including persisted legacy windows without an explicit scope.

    Hermes labels account windows ``account`` and model windows ``model``; the plugin's
    additional model limits use their display names. Unknown scopes never gate an account.
    """
    return window.get("scope") in (None, "", "account")


def percent_tier(windows: Iterable[dict], current: str, now: float, cfg: dict) -> str:
    """Tier implied by usage percentages alone, with enter/leave hysteresis per level.

    Model-scoped windows are display+alert only and never feed the account tier.
    """
    windows = [w for w in windows if is_account_window(w)]
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
    """Reset time of the most-used window at/after the block, else observed_at + block_ttl_s.

    Model-scoped windows are excluded: a scoped 100% window must not set the wrong block expiry.
    """
    windows = [w for w in windows if is_account_window(w)]
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


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v


def _valid_point(p: Any) -> bool:
    return (isinstance(p, list) and len(p) == 3 and _num(p[0]) and _num(p[1])
            and (p[2] is None or _num(p[2])))


def export_payload(state: dict, since: Optional[float], host: str) -> dict:
    """Read-only export payload for ``hermes quota-router export`` (issue #22).

    One sample per retained, well-formed history point whose label is an ACCOUNT window of the
    provider's CURRENT snapshot (labels missing from the snapshot are excluded — fail closed;
    a provider with history but no snapshot exports nothing). Pure: no I/O, no lock, no
    mutation of ``state``. ``since`` filters points to ``ts > since`` (strict); ``None``
    keeps every retained point. No snapshot-age staleness filter: history already carries
    only windows ``append_history`` accepted (non-stale at write time).
    """
    history = state.get("history")
    providers = state.get("providers")
    samples = []
    if not isinstance(history, dict):
        return {"v": 1, "host": host, "samples": samples}
    for provider, per in history.items():
        if not isinstance(per, dict):
            continue
        ps = (providers or {}).get(provider) if isinstance(providers, dict) else None
        snap = ps.get("snapshot") if isinstance(ps, dict) else None
        windows = (snap or {}).get("windows") if isinstance(snap, dict) else None
        if not isinstance(windows, list):
            continue
        account_labels = {w["label"] for w in windows
                          if isinstance(w, dict) and isinstance(w.get("label"), str)
                          and is_account_window(w)}
        for label, pts in per.items():
            if label not in account_labels or not isinstance(pts, list):
                continue
            for p in pts:
                if _valid_point(p) and (since is None or p[0] > since):
                    samples.append({"ts": p[0], "provider": provider, "window": label,
                                    "used_pct": p[1], "resets_at": p[2]})
    return {"v": 1, "host": host, "samples": samples}


def append_history(state: dict, provider: str, snap: dict, now: float,
                   models: list[str] | None = None) -> None:
    """Append ``[fetched_at, used, resets_at]`` per account-wide, non-stale window of a normalized
    snapshot to ``state['history'][provider][label]``. A snapshot no newer than the last point is
    ignored (manual status refreshes re-fold the same data). Points from earlier reset cycles are
    kept; pruning is by age (``HISTORY_MAX_AGE_S``) and count (``HISTORY_MAX_POINTS``) only.
    Malformed history is repaired by dropping the bad parts, never raised. When explicitly given
    models, also retain their family caps for the opt-in throttle's model-specific forecasts."""
    if not isinstance(state.get("history"), dict):
        state["history"] = {}
    per = state["history"].get(provider)
    if not isinstance(per, dict):
        per = state["history"][provider] = {}
    t = float(snap.get("fetched_at") or now)
    for w in snap.get("windows") or []:
        if (w.get("stale") or (not is_account_window(w) and not _model_window(w, models or []))
                or (models is not None and not _valid_usage(w))):
            continue
        pts = per.get(w["label"])
        pts = [p for p in pts if _valid_point(p)] if isinstance(pts, list) else []
        if not pts or t > pts[-1][0]:
            pts.append([t, float(w["used"]), w.get("resets_at")])
        per[w["label"]] = [p for p in pts if p[0] >= now - HISTORY_MAX_AGE_S][-HISTORY_MAX_POINTS:]


def _same_cycle(a: Optional[float], b: Optional[float], cfg: dict) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= cfg["cycle_tolerance_s"]


def current_cycle(points: list, cfg: dict) -> list:
    """Trailing points sharing the last point's reset cycle. A ``resets_at`` move beyond
    ``cycle_tolerance_s``, or a null↔value change, starts a new cycle."""
    out: list = []
    for p in reversed(points):
        if out and not _same_cycle(p[2], out[-1][2], cfg):
            break
        out.append(p)
    return out[::-1]


def _slope_per_h(points: list) -> Optional[float]:
    """Least-squares slope of used% over time, in %/h (None if fewer than 2 distinct times)."""
    n = len(points)
    if n < 2:
        return None
    mt = sum(p[0] for p in points) / n
    mu = sum(p[1] for p in points) / n
    den = sum((p[0] - mt) ** 2 for p in points)
    if den <= 0:
        return None
    return sum((p[0] - mt) * (p[1] - mu) for p in points) / den * 3600.0


def forecast(points: Any, now: float, kind: dict, cfg: dict) -> dict:
    """Projected usage at reset for one window (v2 B5). Pure.

    ``r_long``: least squares over the current cycle (``lsq``) or the cycle average
    (``cycle_average``: used ÷ elapsed cycle time). ``r_short``: least squares over the last
    ``short_lookback_s``. ``projected_at_reset = used + max(r_long, 0) × hours_to_reset``.
    ``projected_short`` uses ``r_short`` over min(hours_to_reset, forecast_short_horizon_s / 3600),
    clamped to [used, 200]. ``accel = r_short − r_long``.
    ``unknown`` with a ``reason`` when there is no reset time, fewer than ``forecast_min_points``
    points, a span under ``forecast_min_span_s`` or ``used`` under ``forecast_min_used``.
    """
    pts = [p for p in (points if isinstance(points, list) else []) if _valid_point(p) and p[0] <= now]
    cycle = current_cycle(sorted(pts, key=lambda p: p[0]), cfg)
    if not cycle:
        return {"status": "unknown", "reason": "no history", "confidence": 0}
    last_t, used, resets_at = cycle[-1]
    base = {"used": used, "resets_at": resets_at, "confidence": len(cycle)}
    if resets_at is None:
        return {"status": "unknown", "reason": "no reset time", **base}
    period = float(kind["period_s"])
    cycle = [p for p in cycle if p[0] >= resets_at - period]
    base["confidence"] = len(cycle)
    if len(cycle) < cfg["forecast_min_points"]:
        return {"status": "unknown", "reason": f"{len(cycle)} points < {cfg['forecast_min_points']}", **base}
    span = cycle[-1][0] - cycle[0][0]
    if span < cfg["forecast_min_span_s"]:
        return {"status": "unknown", "reason": f"span {int(span // 60)}m < {int(cfg['forecast_min_span_s'] // 60)}m",
                **base}
    if used < cfg["forecast_min_used"]:
        return {"status": "unknown", "reason": f"used {used:.0f}% < {cfg['forecast_min_used']:.0f}%", **base}
    if kind.get("long_rate") == "cycle_average":
        elapsed = last_t - (resets_at - period)
        r_long = used / (elapsed / 3600.0) if elapsed > 0 else 0.0
    else:
        r_long = _slope_per_h(cycle) or 0.0
    recent = [p for p in cycle if p[0] >= last_t - kind["short_lookback_s"]]
    r_short = _slope_per_h(recent) if len(recent) >= 3 else None
    r_short = r_long if r_short is None else r_short
    hours = max(resets_at - now, 0.0) / 3600.0
    # A short-lookback trend is a burst, not a cycle-long rate: extrapolate it no further
    # than ``forecast_short_horizon_s`` so an old burst cannot claim the whole remaining
    # tail of a long window (e.g. a 6h trend across 143 weekly hours pinned the router).
    short_hours = min(hours, cfg["forecast_short_horizon_s"] / 3600.0)

    def project(rate: float, h: float) -> float:
        return min(max(used + max(rate, 0.0) * h, used), 200.0)

    return {"status": "ok", **base, "rate_pct_h": r_long, "r_short": r_short, "accel": r_short - r_long,
            "projected_at_reset": project(r_long, hours), "projected_short": project(r_short, short_hours)}


def forecast_state(state: dict, now: float, cfg: dict) -> dict:
    """``{provider: {label: forecast + {'kind'}}}`` for every configured ``forecast_windows``
    label that has history."""
    out: dict = {}
    hist = state.get("history")
    hist = hist if isinstance(hist, dict) else {}
    for provider, labels in (cfg.get("forecast_windows") or {}).items():
        per = hist.get(provider)
        per = per if isinstance(per, dict) else {}
        for label, kind_name in labels.items():
            kind = cfg["forecast_kinds"].get(kind_name)
            if kind is None or label not in per:
                continue
            out.setdefault(provider, {})[label] = {**forecast(per[label], now, kind, cfg), "kind": kind_name}
    return out


def apply_refresh(state: dict, snapshots: dict, now: float, cfg: dict) -> list[str]:
    """Fold fresh snapshots into ``state``; return human lines for alerts/transitions (empty = quiet)."""
    lines: list[str] = []
    prune_alerts(state["alerts_sent"], now)
    for provider, snap in snapshots.items():
        pstate = state["providers"].setdefault(provider, {"tier": "ok", "tier_since": now})
        pstate["snapshot"] = normalize_snapshot(snap, now)
        append_history(state, provider, pstate["snapshot"], now)
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


# Adapted from rarf/hermes-quota-plugin quota_providers/builtin.py (MIT, © Ronald Reis et al.)
_SKIP_SCOPE_NAMES = {"opus", "sonnet"}

# Raw payload key -> core window label, for the direct-percent override below.
_CORE_OVERRIDE_KEYS = (("five_hour", "Current session"), ("seven_day", "Current week"))


def parse_core_overrides(payload: Any) -> dict[str, float]:
    """Direct-percent fix for ``agent.account_usage``'s ``five_hour``/``seven_day`` windows.

    Core's ``_usage_windows(..., fraction=True)`` multiplies ``utilization`` by 100 whenever it
    is <= 1, assuming a 0..1 fraction. Anthropic's ``/api/oauth/usage`` now returns ``utilization``
    already in percent (0..100), so a near-empty window (e.g. 1.0 meaning "1% used") gets
    misread as "1.0 == fully-used fraction" and reported as 100% used. This plugin already makes
    its own raw GET to that endpoint for scoped model limits (see ``_fetch_anthropic_raw`` in
    ``__init__.py``); reuse the same payload to read ``utilization`` as a plain percent, no
    fraction heuristic, and override core's two corresponding windows before display.

    ``percent`` must be a real, finite, non-bool number; it is clamped to 0..100 rather than
    rejected out of range (mirrors ``parse_scoped_limits``). A missing/invalid entry is simply
    absent from the returned dict so the caller falls back to core's own (possibly wrong) value
    rather than inventing one.
    """
    if not isinstance(payload, dict):
        return {}
    out: dict[str, float] = {}
    for key, label in _CORE_OVERRIDE_KEYS:
        window = payload.get(key)
        if not isinstance(window, dict):
            continue
        pct = window.get("utilization")
        if isinstance(pct, bool) or not isinstance(pct, (int, float)):
            continue
        pct = float(pct)
        if pct != pct or pct in (float("inf"), float("-inf")):  # NaN / inf
            continue
        out[label] = max(0.0, min(100.0, pct))
    return out


def parse_scoped_limits(payload: Any) -> list[dict]:
    """Extract Anthropic model-scoped weekly windows from a raw ``/api/oauth/usage`` payload.

    Only ``kind == "weekly_scoped"`` entries with a non-empty ``scope.model.display_name`` are
    kept. Entries named ``opus``/``sonnet`` (case-insensitive) are skipped: core already reports
    those as model windows. ``percent`` must be a real, finite, non-bool number; it is
    clamped to 0..100 rather than rejected out of range. Duplicate names keep the highest percent.
    ``resets_at`` is normalized to an epoch float (or ``None``). Returned dicts never change the
    tier: readers must use ``is_account_window`` (``percent_tier``, ``block_until``).
    """
    if not isinstance(payload, dict):
        return []
    limits = payload.get("limits")
    if not isinstance(limits, list):
        return []
    best: dict[str, dict] = {}
    for entry in limits:
        if not isinstance(entry, dict) or entry.get("kind") != "weekly_scoped":
            continue
        scope = entry.get("scope")
        model = (scope or {}).get("model") if isinstance(scope, dict) else None
        name = (model or {}).get("display_name") if isinstance(model, dict) else None
        if not isinstance(name, str) or not name.strip():
            continue
        name = name.strip()
        if name.lower() in _SKIP_SCOPE_NAMES:
            continue
        pct = entry.get("percent")
        if isinstance(pct, bool) or not isinstance(pct, (int, float)):
            continue
        pct = float(pct)
        if pct != pct or pct in (float("inf"), float("-inf")):  # NaN / inf
            continue
        pct = max(0.0, min(100.0, pct))
        existing = best.get(name)
        if existing is not None and existing["used_percent"] >= pct:
            continue
        best[name] = {"label": f"{name} week (model)", "used_percent": pct,
                      "resets_at": _epoch(entry.get("resets_at")), "scope": name}
    return list(best.values())


_SECRET_PATTERNS = None


def scrub(text: str) -> str:
    """Redact bearer tokens / API keys that might leak into an error message."""
    import re
    global _SECRET_PATTERNS
    if _SECRET_PATTERNS is None:
        _SECRET_PATTERNS = [re.compile(p) for p in
                            (r"Bearer\s+\S+", r"sk-ant-\S+", r"oat\w*-\S+")]
    for pat in _SECRET_PATTERNS:
        text = pat.sub("[redacted]", text)
    return text


def is_stale(snapshot: Optional[dict], now: float, cfg: dict) -> bool:
    if not snapshot:
        return True
    age = now - float(snapshot.get("fetched_at") or 0)
    return age > cfg["stale_after_s"]


# ── cache efficiency (host-local, read-only over state.db; display + alert only) ──

CACHE_PROVIDERS = ("anthropic", "openai-codex", "other")
CACHE_WINDOWS = (("24h", 24 * 3600), ("7d", 7 * 24 * 3600))
CACHE_TOP_N = 5
CACHE_ALERT_REPEAT_S = 24 * 3600
# Session totals are cumulative, so a session counts in the window it was LAST active in.
_ACTIVE = "COALESCE(last_activity_at, started_at)"
_SUMS = ("COUNT(*), COALESCE(SUM(input_tokens),0), COALESCE(SUM(cache_read_tokens),0), "
         "COALESCE(SUM(cache_write_tokens),0), COALESCE(SUM(output_tokens),0)")
_SESSION_COLS = ("id, title, source, model, billing_provider, COALESCE(input_tokens,0), "
                 "COALESCE(cache_read_tokens,0), COALESCE(cache_write_tokens,0), COALESCE(output_tokens,0)")


def cache_provider(billing_provider: Any) -> str:
    """Map a ``sessions.billing_provider`` onto the cache section's provider buckets."""
    p = str(billing_provider or "").strip().lower()
    return p if p in WATCHED else "other"


def hit_pct(inp: int, read: int, write: int) -> Optional[float]:
    """``cache_read / (input + cache_read + cache_write)`` as a percent, or ``None`` with no input."""
    total = inp + read + write
    return round(100.0 * read / total, 1) if total else None


def _agg(sessions: int, inp: int, read: int, write: int, out: int) -> dict:
    return {"sessions": sessions, "input": inp, "cache_read": read, "cache_write": write,
            "output": out, "hit": hit_pct(inp, read, write)}


def _session_row(row) -> dict:
    sid, title, source, model, provider, inp, read, write, out = row
    return {"id": sid, "title": (title or None) and str(title)[:80], "source": source, "model": model,
            "provider": provider, "input": inp, "cache_read": read, "cache_write": write, "output": out,
            "burn": inp + write, "hit": hit_pct(inp, read, write)}


def collect_cache_outcome(db_path: Path, now: float, cfg: dict, timeout_s: float = 0.5):
    """Aggregate prompt-cache stats from ``state.db``. Returns ``(ok, kind, message, data|None)``.

    Opens the DB read-only (``mode=ro`` URI, which never creates a file) with a short busy
    timeout; kinds are ``missing``, ``locked`` and ``db_error``. ``data`` carries per-provider,
    per-source aggregates for each window, the top burners by uncached input plus cache writes
    over 24h, and the sessions that trip the cache-write alert.
    """
    db_path = Path(db_path)
    if not db_path.is_file():
        return False, "missing", f"no state.db at {db_path.name}", None
    uri = f"file:{urllib.parse.quote(str(db_path))}?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=timeout_s)
        try:
            providers = {p: {} for p in CACHE_PROVIDERS}
            for label, span in CACHE_WINDOWS:
                totals = {p: [0] * 5 for p in CACHE_PROVIDERS}
                sources: dict = {p: {} for p in CACHE_PROVIDERS}
                rows = con.execute(f"SELECT billing_provider, source, {_SUMS} FROM sessions "
                                   f"WHERE {_ACTIVE} >= ? GROUP BY billing_provider, source",
                                   (now - span,)).fetchall()
                for provider, source, *nums in rows:
                    p = cache_provider(provider)
                    src = sources[p].setdefault(source or "?", [0] * 5)
                    for i, n in enumerate(nums):
                        totals[p][i] += n
                        src[i] += n
                for p in CACHE_PROVIDERS:
                    agg = _agg(*totals[p])
                    agg["sources"] = {s: _agg(*v) for s, v in sorted(sources[p].items())}
                    providers[p][label] = agg
            since = now - CACHE_WINDOWS[0][1]
            top = con.execute(
                f"SELECT {_SESSION_COLS} FROM sessions WHERE {_ACTIVE} >= ? "
                "AND COALESCE(input_tokens,0) + COALESCE(cache_write_tokens,0) > 0 "
                "ORDER BY COALESCE(input_tokens,0) + COALESCE(cache_write_tokens,0) DESC, id "
                "LIMIT ?", (since, CACHE_TOP_N)).fetchall()
            writers = con.execute(
                f"SELECT {_SESSION_COLS} FROM sessions WHERE {_ACTIVE} >= ? "
                "AND COALESCE(cache_write_tokens,0) >= ? ORDER BY cache_write_tokens DESC, id",
                (since, cfg["cache_write_alert_tokens"])).fetchall()
        finally:
            con.close()
    except sqlite3.OperationalError as exc:
        msg = str(exc)
        kind = "locked" if "locked" in msg or "busy" in msg else "db_error"
        return False, kind, msg[:200], None
    except sqlite3.DatabaseError as exc:
        return False, "db_error", str(exc)[:200], None
    writers = [w for w in map(_session_row, writers)
               if w["hit"] is not None and w["hit"] < cfg["cache_write_alert_below"]]
    return True, None, "", {"providers": providers, "top": [_session_row(r) for r in top],
                            "writers": writers}


def fmt_tokens(n: Any) -> str:
    n = float(n or 0)
    for div, suffix, digits in ((1e9, "B", 2), (1e6, "M", 1), (1e3, "K", 1)):
        if n >= div:
            return f"{n / div:.{digits}f}{suffix}"
    return f"{int(n)}"


def _fmt_hit(hit: Optional[float]) -> str:
    return "—" if hit is None else f"{hit:.1f}%"


def cache_alert_lines(data: dict, sent: dict, now: float, cfg: dict) -> list[str]:
    """Low-hit and heavy-cache-write alerts, each at most once per condition per 24h. Mutates ``sent``."""
    for key in [k for k, at in sent.items() if now - float(at) >= CACHE_ALERT_REPEAT_S]:
        sent.pop(key, None)
    out = []
    for p in CACHE_PROVIDERS:
        agg = data["providers"][p]["24h"]
        volume = agg["input"] + agg["cache_read"] + agg["cache_write"]
        key = f"hit|{p}"
        if (agg["hit"] is not None and agg["hit"] < cfg["cache_alert_below"]
                and volume >= cfg["cache_alert_min_tokens"] and key not in sent):
            sent[key] = now
            out.append(f"⚠️ cache: {p} 24h hit {_fmt_hit(agg['hit'])} (<{cfg['cache_alert_below']:g}%) "
                       f"on {fmt_tokens(volume)} input")
    for w in data.get("writers") or []:
        key = f"write|{w['id']}"
        if key in sent:
            continue
        sent[key] = now
        out.append(f"⚠️ cache: session {w['id']} ({w['provider'] or '?'}/{w['source']}, {w['model'] or '?'}) "
                   f"wrote {fmt_tokens(w['cache_write'])} to cache in 24h at {_fmt_hit(w['hit'])} hit "
                   f"(<{cfg['cache_write_alert_below']:g}%)")
    return out


def apply_cache(state: dict, outcome: tuple, now: float, cfg: dict) -> list[str]:
    """Fold a ``collect_cache_outcome`` result into ``state['cache']`` (aggregates only) and queue
    any new alert lines, which are returned. A failure records ``cache.fetch.error`` (keeping the
    incident's ``since``) and leaves the last good aggregates in place. Never touches tier or mode."""
    ok, kind, message, data = outcome
    cache = state.setdefault("cache", {})
    fetch = cache.setdefault("fetch", {"last_ok_at": None, "error": None})
    if not ok:
        since = (fetch.get("error") or {}).get("since", now)
        fetch["error"] = {"since": since, "at": now, "kind": kind, "message": scrub(message or "")[:200]}
        return []
    fetch["last_ok_at"], fetch["error"] = now, None
    cache["collected_at"] = now
    cache["providers"] = data["providers"]
    cache["top"] = data["top"]
    lines = cache_alert_lines(data, cache.setdefault("alerts_sent", {}), now, cfg)
    queue(state, *lines)
    return lines


def cache_payload(state: dict, now: float, cfg: dict) -> dict:
    cache = state.get("cache") or {}
    fetch = cache.get("fetch") or {}
    collected = cache.get("collected_at")
    return {"collected_at": collected,
            "stale": collected is None or now - float(collected) > cfg["stale_after_s"],
            "alert_below": cfg["cache_alert_below"],
            "providers": cache.get("providers") or {},
            "top": cache.get("top") or [],
            "fetch": {"last_ok_at": fetch.get("last_ok_at"), "error": fetch.get("error")}}


def render_cache(state: dict, now: float) -> list[str]:
    cache = state.get("cache") or {}
    err = (cache.get("fetch") or {}).get("error")
    collected = cache.get("collected_at")
    if collected is None:
        out = ["cache: no data yet (collected by refresh)"]
    else:
        out = [f"cache: (prompt-cache hit = read / (input + read + write), collected {int((now - collected) // 60)}m ago)"]
        shown = False
        for p in CACHE_PROVIDERS:
            windows = (cache.get("providers") or {}).get(p) or {}
            d, w = windows.get("24h") or {}, windows.get("7d") or {}
            if not w.get("sessions"):
                continue
            shown = True
            out.append(f"    {p:<14} 24h {_fmt_hit(d.get('hit')):>6} of {fmt_tokens(_in(d)):>6}   "
                       f"7d {_fmt_hit(w.get('hit')):>6} of {fmt_tokens(_in(w)):>6}")
            srcs = sorted(set(d.get("sources") or {}) | set(w.get("sources") or {}))
            for s in srcs:
                sd, sw = (d.get("sources") or {}).get(s) or {}, (w.get("sources") or {}).get(s) or {}
                out.append(f"        {s:<10} 24h {_fmt_hit(sd.get('hit')):>6} of {fmt_tokens(_in(sd)):>6}   "
                           f"7d {_fmt_hit(sw.get('hit')):>6} of {fmt_tokens(_in(sw)):>6}")
        if not shown:
            out.append("    no sessions in the last 7d")
        top = cache.get("top") or []
        if top:
            out.append("    top burners (24h, uncached input + cache writes):")
            for t in top:
                title = f"  \"{t['title'][:40]}\"" if t.get("title") else ""
                out.append(f"        {t['id']}  {t.get('provider') or '?'}/{t.get('source')}  {t.get('model') or '?'}  "
                           f"hit {_fmt_hit(t.get('hit'))}  burn {fmt_tokens(t.get('burn'))} "
                           f"(in {fmt_tokens(t.get('input'))}, write {fmt_tokens(t.get('cache_write'))}){title}")
    if err:
        fage = int((now - float(err.get("at") or now)) // 60)
        out.append(f"    cache stats fetch failed {fage}m ago — {err.get('kind')}: {(err.get('message') or '')[:100]}")
    return out


def _in(agg: dict) -> int:
    return sum(int(agg.get(k) or 0) for k in ("input", "cache_read", "cache_write"))


def status_payload(state: dict, now: float, cfg: dict, host: str) -> dict:
    """Machine-readable status for the desktop widget: see the plan's JSON contract."""
    providers = {}
    forecasts = forecast_state(state, now, cfg)
    for p in WATCHED:
        ps = state["providers"].get(p) or {}
        snap = ps.get("snapshot") or {}
        fetch = ps.get("fetch") or {}
        core_fetch = fetch.get("core") or {}
        scoped_fetch = fetch.get("scoped") or {}
        pf = forecasts.get(p) or {}
        windows = [{"label": w["label"], "used": w["used"], "resets_at": w.get("resets_at"),
                   "scope": w.get("scope"), "affects_tier": is_account_window(w),
                   "stale": bool(w.get("stale")),
                   "forecast": pf.get(w["label"]) if is_account_window(w) else None}
                  for w in (snap.get("windows") or [])]
        block = state["blocks"].get(p)
        block_out = {"until": block["until"], "message": block.get("message", "")[:200]} \
            if active_block(block, now) else None
        providers[p] = {
            "tier": ps.get("tier", "ok"),
            "fetched_at": snap.get("fetched_at"),
            "stale": is_stale(snap, now, cfg) if snap else True,
            "plan": snap.get("plan"),
            "windows": windows,
            "block": block_out,
            "fetch": {"core": {"last_ok_at": core_fetch.get("last_ok_at"),
                               "error": core_fetch.get("error")},
                     "scoped": {"last_ok_at": scoped_fetch.get("last_ok_at"),
                               "error": scoped_fetch.get("error")}},
            "details": snap.get("details") or [],
        }
        rc = _reset_credits_slot(state) if p == "openai-codex" else {}
        if _num(rc.get("fetched_at")):
            # Ids are omitted: the widget only needs counts and expiry times.
            providers[p]["reset_credits"] = {
                "fetched_at": rc["fetched_at"], "available_count": rc.get("available_count"),
                "credits": [{"expires_at": c.get("expires_at"), "granted_at": c.get("granted_at")}
                            for c in rc.get("credits") or [] if isinstance(c, dict)]}
    errs = [{"at": e["at"], "provider": e["provider"], "status": e.get("status"),
            "reason": e.get("reason"), "message": e.get("message", "")[:200]}
           for e in (state.get("errors") or [])[-5:]]
    return {"v": 1, "host": host, "now": now, "mode": state.get("mode", "normal"),
           "mode_since": state.get("mode_since"), "providers": providers,
           "recent_errors": errs, "pending_count": len(state.get("pending") or []),
           "cache": cache_payload(state, now, cfg)}


def note_fetch(state: dict, provider: str, *, slot: str = "core", ok: bool, kind: Optional[str] = None,
               message: str = "", now: float, cfg: dict) -> None:
    """Record a fetch outcome for ``provider``/``slot`` (``'core'`` or ``'scoped'``).

    Success clears the error and, if there was a previously-alerted core incident, queues a
    recovery line. Failure preserves ``error.since`` from the first failure of the incident (a
    change of ``kind`` does not reset it) and, for ``slot == 'core'`` only, queues one alert once
    the incident has lasted ``cfg['stale_after_s']``.
    """
    pstate = state["providers"].setdefault(provider, {"tier": "ok", "tier_since": now})
    fetch = pstate.setdefault("fetch", {})
    fslot = fetch.setdefault(slot, {"last_ok_at": None, "error": None, "alerted": False})
    if slot == "core":
        log = state.get("fetch_log")
        log = [e for e in log if isinstance(e, list) and len(e) == 3 and _num(e[0])] \
            if isinstance(log, list) else []
        log.append([now, provider, "ok" if ok else (kind or "error")])
        state["fetch_log"] = [e for e in log if e[0] >= now - FETCH_LOG_MAX_AGE_S][-FETCH_LOG_MAX:]
    if ok:
        had_alerted = bool(fslot.get("error") and fslot.get("alerted"))
        fslot["last_ok_at"] = now
        fslot["error"] = None
        fslot["alerted"] = False
        fslot.pop("backoff_alerted", None)
        if had_alerted and slot == "core":
            queue(state, f"quota-router: {provider} usage fetch recovered")
        return
    since = (fslot.get("error") or {}).get("since", now)
    message = scrub(message or "")
    fslot["error"] = {"since": since, "at": now, "kind": kind, "message": message[:200]}
    if slot == "core" and not fslot.get("alerted") and now - since >= cfg["stale_after_s"]:
        mins = int((now - since) // 60)
        queue(state, f"quota-router: {provider} usage unavailable for {mins}m+ ({kind}: {message[:80]})")
        fslot["alerted"] = True


def fetch_due(state: dict, provider: str, now: float, cfg: dict) -> bool:
    """Whether a core usage fetch for ``provider`` should run now. After ``backoff_failures``
    non-ok fetches within ``backoff_window_s`` (from ``fetch_log``) with no success since, fetches
    are spaced ``backoff_interval_s`` apart; one alert per incident. A success clears it."""
    log = state.get("fetch_log")
    log = log if isinstance(log, list) else []
    mine = [e for e in log if isinstance(e, list) and len(e) == 3 and e[1] == provider and _num(e[0])]
    recent = [e for e in mine if e[0] >= now - cfg["backoff_window_s"]]
    last_ok = max((e[0] for e in mine if e[2] == "ok"), default=None)
    bad = [e for e in recent if e[2] != "ok" and (last_ok is None or e[0] > last_ok)]
    if len(bad) < cfg["backoff_failures"]:
        return True
    fslot = state["providers"].setdefault(provider, {"tier": "ok", "tier_since": now}) \
        .setdefault("fetch", {}).setdefault("core", {"last_ok_at": None, "error": None, "alerted": False})
    if not fslot.get("backoff_alerted"):
        fslot["backoff_alerted"] = True
        queue(state, f"quota-router: {provider} usage fetch backing off to {int(cfg['backoff_interval_s'] // 60)}m "
                     f"({len(bad)} non-ok fetches in {int(cfg['backoff_window_s'] // 60)}m)")
    return now - max(e[0] for e in mine) >= cfg["backoff_interval_s"]


# ── auto-redeem banked Codex reset credits ────────────────────────────────────

def claim_auto_redeem(state: dict, now: float, cfg: dict) -> tuple[bool, str]:
    """Whether to redeem one banked openai-codex reset credit now — and reserve the cap slot.

    MUST be called under ``locked_state``: the decision and the slot reservation are one
    atomic step, so overlapping refreshes (cron + startup + ``status --refresh``) cannot
    both pass the cap check and double-redeem. The reservation is a placeholder entry in
    ``auto_reset.redeems``; ``settle_auto_redeem`` keeps it on success and rolls it back on
    failure. Returns ``(claimed, reason)``.

    Fires only when ALL of these hold:

    - ``auto_reset`` is configured (a dict; ``None`` = feature off);
    - the provider is ``openai-codex`` with a fresh core snapshot (no ``unavailable``);
    - the snapshot reports banked credits (``available_count`` > 0);
    - the **weekly** window (the secondary window — the one that does not self-heal for
      days) is at or above ``weekly_threshold`` (default 97.0) AND its ``resets_at`` is
      still in the future (a window whose reset has passed is a freshly refilled NEW
      week — its shown exhaustion is history). A dead 5h window alone never spends a
      credit: it empties itself in hours, while a reset restores the FULL 5h + weekly
      allowance — spending it into a half-used weekly wastes most of its value;
    - fewer than ``auto_reset_max_redeems_per_week`` redemptions (including one in-flight
      reservation) are recorded in the trailing 7 days, so repeated bad data or overlapping
      refreshes cannot drain the bank;
    - no expiry-driven redemption (``claim_expiry_redeem``) is in flight: its credit may be
      resetting this same window, and a second credit spent into it would be wasted.

    ``reason`` explains the decision either way (for the alert line / tests).
    """
    if _expiry_inflight(state, now):
        return False, "an expiry-driven redemption is in flight"
    return _auto_redeem(state, now, cfg)


def _auto_redeem(state: dict, now: float, cfg: dict, *, planning: bool = False) -> tuple[bool, str]:
    """Shared eligibility checks. Planning skips the actual exhaustion gate and NEVER
    reserves a slot; claim_auto_redeem always uses the exhaustion gate and reservation.
    Planning is stricter about missing/future/stale data because it promises future capacity.
    """
    acfg = cfg.get("auto_reset")
    if not isinstance(acfg, dict):
        return False, "auto_reset not configured"
    pstate = (state.get("providers") or {}).get("openai-codex") or {}
    snap = pstate.get("snapshot") or {}
    if not snap or snap.get("unavailable"):
        return False, "no fresh snapshot"
    try:
        fetched_at = float(snap.get("fetched_at") or 0)
        if fetched_at != fetched_at:  # NaN
            raise ValueError
    except (TypeError, ValueError):
        return False, "invalid snapshot fetched_at"
    if now - fetched_at > cfg["stale_after_s"]:
        return False, "snapshot stale"
    if planning and (not _num(snap.get("fetched_at")) or fetched_at > now
                     or fetched_at in (float("inf"), float("-inf"))):
        return False, "invalid snapshot fetched_at for planning"
    windows_in = snap.get("windows")
    if not isinstance(windows_in, list):
        return False, "invalid snapshot windows"
    windows = [w for w in windows_in if isinstance(w, dict) and is_account_window(w)]
    for w in windows:
        if not isinstance(w.get("label"), str):
            return False, "invalid window label"
    by_label = {w.get("label"): w for w in windows}
    weekly = by_label.get("Weekly")
    if weekly is None:
        return False, "no Weekly window in snapshot"
    if planning:
        reset = weekly.get("resets_at")
        if (weekly.get("stale") or reset is None or not _num(reset)
                or reset in (float("inf"), float("-inf")) or reset <= now):
            return False, "no usable Weekly reset for planning"
    session = by_label.get("Session")
    # Guard BOTH window reads against provider NaN/garbage: float(nan) is truthy, and
    # nan < threshold is False, so an unguarded `used or 0.0` lets NaN sail through the
    # below-threshold early return and spend a banked credit on bad data. (r3-F1, high —
    # reproduced end-to-end on the real refresh path.) _num() also catches None/inf/str.
    for label, w in (("Weekly", weekly), ("Session", session)):
        if w is not None:
            u = w.get("used")
            if not _num(u) or u in (float("inf"), float("-inf")) or abs(u) > 1e15:
                return False, f"invalid {label} usage data"
    # A Weekly window whose reset has already passed is a NEW (freshly refilled) week — the
    # old exhaustion it shows is history, and redeeming into it would waste the credit.
    if weekly.get("resets_at"):
        try:
            resets_at = float(weekly["resets_at"])
        except (TypeError, ValueError):
            return False, "invalid Weekly resets_at"
        if resets_at <= now:
            return False, "Weekly window already reset (stale exhaustion)"
    try:
        threshold = float(acfg.get("weekly_threshold", 97.0))
        if not threshold == threshold:  # NaN
            raise ValueError
    except (TypeError, ValueError):
        return False, "invalid auto_reset.weekly_threshold config"
    try:
        max_redeems = int(cfg.get("auto_reset_max_redeems_per_week", 1))
    except (TypeError, ValueError, OverflowError):
        return False, "invalid auto_reset_max_redeems_per_week config"
    if planning and not 0 < threshold <= 100:
        return False, "unreachable auto_reset.weekly_threshold for planning"
    if not planning and float(weekly.get("used") or 0.0) < threshold:
        return False, f"Weekly {weekly.get('used', 0):.0f}% < {threshold:.0f}%"
    details_in = snap.get("details") or []
    if not isinstance(details_in, list) or not all(isinstance(d, str) for d in details_in):
        return False, "invalid snapshot details"
    details = [d.lower() for d in details_in]
    banked = next((int(d.split()[2]) for d in details if d.startswith("you have") and " banked" in d
                   and d.split()[2].isdigit()), 0)
    if banked <= 0:
        return False, "no banked credits"
    if planning:
        # Err toward constraining: a credit that lapses before the Weekly reset may be gone (or
        # spent early by claim_expiry_redeem) before exhaustion, so it is not a full refill.
        listed = usable_reset_credits(state, now, cfg)
        if listed is not None and not any(c["expires_at"] is None or c["expires_at"] >= float(weekly["resets_at"])
                                          for c in listed["credits"]):
            return False, "banked credits all expire before the Weekly reset"
    history = state.get("auto_reset")
    if history is not None and not isinstance(history, dict):
        return False, "invalid auto_reset state"
    history = history or {}
    # Every entry counts against the cap: bare floats (settled successes) AND dicts
    # (in-flight reservations from claim_auto_redeem). Dict reservations age out on the
    # same trailing window: a claim older than 7d cannot still be in flight (the redeem
    # call settles in seconds), so a crash between claim and settle must not strand the
    # feature — the reservation silently expires and the next eligible claim proceeds.
    def _entry_age(r):
        # in-flight reservation dict -> its claim time; settled success float -> itself.
        # Unparseable entries age out immediately (0.0 = ancient).
        t = r.get("at") if isinstance(r, dict) else r
        return t if _num(t) else 0.0

    redeems_in = history.get("redeems") or []
    if not isinstance(redeems_in, list):
        return False, "invalid auto_reset.redeems state"
    redeems = [r for r in redeems_in
               if _entry_age(r) >= now - 7 * 24 * 3600]
    if len(redeems) >= max_redeems:
        return False, f"weekly redeem cap reached ({len(redeems)} in trailing 7d)"
    reason = f"Weekly at {weekly.get('used', 0):.0f}% (>= {threshold:.0f}%), {banked} banked"
    if planning:
        return True, f"one future Weekly refill available ({banked} banked)"
    # Reserve the slot NOW (placeholder dict, distinct from the bare-float successes), so a
    # concurrent refresh's claim sees it and stays under the cap.
    ar = state.setdefault("auto_reset", {})
    if not isinstance(ar, dict):
        # persisted auto_reset was None/non-dict: setdefault returned it unchanged —
        # replace it so the reservation can be written.
        ar = {}
        state["auto_reset"] = ar
    ar["redeems"] = redeems + [{"at": now, "claim": reason}]
    return True, reason


def settle_auto_redeem(state: dict, now: float, reason: str, outcome: str,
                       settled_at: Optional[float] = None) -> None:
    """Settle a claimed auto-redeem slot after the network call. Must be under ``locked_state``.

    ``outcome == "reset"`` keeps the reservation (converted to a plain success timestamp);
    any other outcome rolls the reservation back so the cap is not burned by a refused or
    failed attempt (the runtime guard refuses below 100% usage; that must not block the
    real redeem once the weekly window actually reaches 100%). Records ``last`` and queues
    one alert line either way.
    """
    ar = state.setdefault("auto_reset", {})
    if not isinstance(ar, dict):
        # persisted auto_reset is a non-dict (corrupt state): replace rather than raise,
        # settle must not crash — but preserve nothing unparseable.
        ar = {}
        state["auto_reset"] = ar
    ar["last"] = {"at": now, "reason": reason, "outcome": outcome}
    # Claims are interchangeable — matching by reason string would fail when two concurrent
    # claims render the same reason. Settle touches exactly ONE reservation (this attempt's,
    # i.e. the first placeholder dict); all other entries (settled successes + sibling
    # in-flight claims) are preserved untouched.
    redeems_in = ar.get("redeems") or []
    redeems = list(redeems_in) if isinstance(redeems_in, list) else []
    for i, r in enumerate(redeems):
        if isinstance(r, dict):
            if outcome == "reset":
                redeems[i] = now  # this attempt's reservation -> plain success stamp
            else:
                del redeems[i]    # roll back: a refused/failed attempt must not burn the cap
            break
    ar["redeems"] = redeems
    _mark_reset_credits_stale(state, now if settled_at is None else max(now, settled_at))
    verb = "auto-redeemed" if outcome == "reset" else f"auto-redeem not done ({outcome})"
    # ⚠️ prefix: queue() evicts plain "quota-router:" lines first under PENDING_CAP, and a
    # redemption (or a refused attempt) is exactly the event the user must not lose.
    queue(state, f"⚠️ quota-router: {verb} a banked codex reset credit ({reason})")


# ── banked reset credit list + expiry-driven redemption ───────────────────────────────────────

def parse_reset_credits(payload: Any) -> dict:
    """``GET <codex backend>/rate-limit-reset-credits`` -> ``{"available_count", "credits"}``.

    Keeps only credits the account can spend now (``status == "available"`` and not
    ``is_supported_by_plan: false``) as ``{"id", "expires_at", "granted_at"}`` in epoch seconds
    (``expires_at`` None = does not expire), soonest-expiring first — the order the Codex CLI's
    picker offers them. Raises ``ValueError`` when the payload has no credit list.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("credits"), list):
        raise ValueError("reset-credits payload has no credits list")
    credits = []
    for c in payload["credits"]:
        if not isinstance(c, dict) or str(c.get("status") or "").lower() != "available" \
                or c.get("is_supported_by_plan") is False:
            continue
        cid = c.get("id")
        if not isinstance(cid, str) or not cid:
            continue
        exp, granted = _epoch(c.get("expires_at")), _epoch(c.get("granted_at"))
        credits.append({"id": cid,
                        "expires_at": exp if exp is not None and math.isfinite(exp) else None,
                        "granted_at": granted if granted is not None and math.isfinite(granted) else None})
    credits.sort(key=lambda c: (c["expires_at"] is None, c["expires_at"] or 0.0))
    count = payload.get("available_count")
    return {"available_count": count if _num(count) else None, "credits": credits}


def _reset_credits_slot(state: dict) -> dict:
    slot = ((state.get("providers") or {}).get("openai-codex") or {}).get("reset_credits")
    return slot if isinstance(slot, dict) else {}


def apply_reset_credits(state: dict, parsed: dict, now: float) -> None:
    pstate = state["providers"].setdefault("openai-codex", {"tier": "ok", "tier_since": now})
    pstate["reset_credits"] = {"fetched_at": now, "available_count": parsed.get("available_count"),
                               "credits": list(parsed.get("credits") or [])}


def _mark_reset_credits_stale(state: dict, settled_at: float) -> None:
    """Invalidate every credit list fetched at or before ``settled_at`` (a redemption attempt's
    wall-clock settle time; a failed or timed-out POST may still have spent a credit).

    Kept in ``auto_reset``, not on the list, so a list another process fetched BEFORE the settle
    but applies AFTER it (``apply_reset_credits`` replaces the slot) is still invalid: lists are
    stamped no later than their GET started.
    """
    ar = state.get("auto_reset")
    if not isinstance(ar, dict):
        ar = {}
        state["auto_reset"] = ar
    prev = ar.get("credits_invalid_before")
    ar["credits_invalid_before"] = max(settled_at, prev) if _num(prev) and math.isfinite(prev) else settled_at


def _reset_credits_refresh_s(cfg: dict) -> float:
    v = cfg.get("reset_credits_refresh_s")
    return float(v) if _num(v) and math.isfinite(v) and v > 0 else float(DEFAULTS["reset_credits_refresh_s"])


def reset_credits_due(state: dict, now: float, cfg: dict) -> bool:
    """Whether to (re)fetch the credit list: none yet, older than ``reset_credits_refresh_s``,
    or not newer than the latest redemption attempt, claim, or settle."""
    at = _reset_credits_slot(state).get("fetched_at")
    return not _num(at) or at <= _last_redeem_at(state) or now - at >= _reset_credits_refresh_s(cfg)


def _last_redeem_at(state: dict) -> float:
    """Latest redemption, in-flight claim, or settle of either kind (0.0 when none)."""
    ar = state.get("auto_reset")
    if not isinstance(ar, dict):
        return 0.0
    inv = ar.get("credits_invalid_before")
    times = [inv] if _num(inv) and math.isfinite(inv) else []
    redeems = ar.get("redeems")
    for r in redeems if isinstance(redeems, list) else []:
        t = r.get("at") if isinstance(r, dict) else r
        if _num(t):
            times.append(t)
    ex = ar.get("expiry")
    if isinstance(ex, dict):
        inflight = ex.get("inflight")
        if isinstance(inflight, dict) and _num(inflight.get("at")):
            times.append(inflight["at"])
        redeemed = ex.get("redeemed")
        for r in redeemed if isinstance(redeemed, list) else []:
            if isinstance(r, dict) and _num(r.get("at")):
                times.append(r["at"])
    return max(times, default=0.0)


def usable_reset_credits(state: dict, now: float, cfg: dict) -> Optional[dict]:
    """The stored credit list when it can drive a decision, else None: fetched within two
    refresh intervals, and AFTER the latest redemption, in-flight claim, or settle (an older list
    may still show the credit that was just spent)."""
    slot = _reset_credits_slot(state)
    at = slot.get("fetched_at")
    if not _num(at) or at > now or not isinstance(slot.get("credits"), list):
        return None
    if now - at > 2 * _reset_credits_refresh_s(cfg):
        return None
    if at <= _last_redeem_at(state):
        return None
    return slot


def redeem_target(state: dict, now: float, cfg: dict) -> Optional[str]:
    """Id of the soonest-expiring unexpired credit from a usable list (None: let the backend pick)."""
    listed = usable_reset_credits(state, now, cfg)
    for c in (listed or {}).get("credits") or []:
        if c.get("expires_at") is None or c["expires_at"] > now:
            return c["id"]
    return None


def _expiry_state(state: dict) -> dict:
    ar = state.get("auto_reset")
    if not isinstance(ar, dict):
        ar = {}
        state["auto_reset"] = ar
    ex = ar.get("expiry")
    if not isinstance(ex, dict):
        ex = {}
        ar["expiry"] = ex
    return ex


def _expiry_inflight(state: dict, now: float) -> bool:
    ar = state.get("auto_reset")
    ex = ar.get("expiry") if isinstance(ar, dict) else None
    inflight = ex.get("inflight") if isinstance(ex, dict) else None
    return isinstance(inflight, dict) and _num(inflight.get("at")) and 0 <= now - inflight["at"] < EXPIRY_INFLIGHT_S


def _exhaustion_inflight(state: dict, now: float) -> bool:
    """A recent unsettled ``claim_auto_redeem`` reservation (placeholder dict in ``redeems``)."""
    ar = state.get("auto_reset")
    redeems = ar.get("redeems") if isinstance(ar, dict) else None
    return any(isinstance(r, dict) and _num(r.get("at")) and 0 <= now - r["at"] < EXPIRY_INFLIGHT_S
               for r in (redeems if isinstance(redeems, list) else []))


def _fmt_in(seconds: float) -> str:
    return f"{seconds / 3600:.0f}h" if seconds < 48 * 3600 else f"{seconds / 86400:.1f}d"


def claim_expiry_redeem(state: dict, now: float, cfg: dict, *, supports_credit_id: bool
                        ) -> tuple[bool, str, Optional[str]]:
    """Whether to spend the soonest-expiring banked credit now because it would otherwise lapse,
    and reserve it. MUST be called under ``locked_state``. Returns ``(claimed, reason, credit_id)``.

    Fires only when ALL of these hold:

    - ``auto_reset`` is configured;
    - a fresh openai-codex snapshot has a valid Weekly window whose reset is in the future;
    - the credit list is usable (``usable_reset_credits``: fresh and newer than every redemption);
    - its soonest unexpired credit expires no later than the Weekly reset (otherwise it can
      still be spent at exhaustion in the next window);
    - Weekly is at least ``expiry_min_used`` (default 10%) used: that much is restored instead of lost;
    - ``expiry_lead_s`` (default 0 = no cap) is either 0 or at least the time to expiry;
    - no claim of either kind is in flight (a peer's exhaustion redeem may be resetting the
      window this credit would restore), and no expiry claim failed within ``EXPIRY_RETRY_S``;
    - Hermes can redeem a specific credit id. Untargeted, the backend may spend a later credit and
      the expiring one lapses anyway, so without it this refuses and queues one alert per credit.

    Unlike weekly exhaustion this spends below 100% (the caller passes ``force``), and it does
    not count toward ``auto_reset_max_redeems_per_week``: the credit is lost otherwise. It fires
    as soon as the above holds rather than near expiry: within this window the credit lapses
    anyway, so spending early loses nothing and returns the fresh allowance with the whole
    remaining window to use it.
    """
    acfg = cfg.get("auto_reset")
    if not isinstance(acfg, dict):
        return False, "auto_reset not configured", None
    lead, min_used = acfg.get("expiry_lead_s", EXPIRY_LEAD_S), acfg.get("expiry_min_used", EXPIRY_MIN_USED)
    if not _num(lead) or not _num(min_used) or lead < 0 or not math.isfinite(min_used):
        return False, "invalid auto_reset.expiry_lead_s/expiry_min_used config", None
    snap = ((state.get("providers") or {}).get("openai-codex") or {}).get("snapshot") or {}
    fetched_at = snap.get("fetched_at")
    if not snap or snap.get("unavailable") or not _num(fetched_at) or fetched_at > now \
            or now - fetched_at > cfg["stale_after_s"]:
        return False, "no fresh snapshot", None
    windows = snap.get("windows")
    weekly = next((w for w in windows if isinstance(w, dict) and w.get("label") == "Weekly"
                   and is_account_window(w)), None) if isinstance(windows, list) else None
    if weekly is None:
        return False, "no Weekly window in snapshot", None
    used, reset = weekly.get("used"), weekly.get("resets_at")
    if not _num(used) or not math.isfinite(used) or weekly.get("stale"):
        return False, "invalid Weekly usage data", None
    if not _num(reset) or not math.isfinite(reset) or reset <= now:
        return False, "no usable Weekly reset", None
    listed = usable_reset_credits(state, now, cfg)
    if listed is None:
        return False, "reset-credit list missing, stale, or older than the last redemption", None
    target = next((c for c in listed["credits"] if c.get("expires_at") is not None and c["expires_at"] > now), None)
    if target is None:
        return False, "no expiring banked credit", None
    expires = target["expires_at"]
    when = f"{datetime.fromtimestamp(expires):%a %m-%d %H:%M} (in {_fmt_in(expires - now)})"
    if lead and expires - now > lead:
        return False, f"next credit expires {when}, outside expiry_lead_s", None
    if reset < expires:
        return False, f"Weekly resets before the credit expires {when}; it stays spendable next window", None
    if used < min_used:
        return False, f"Weekly {used:.0f}% < expiry_min_used {min_used:.0f}%; credit expiring {when}", None
    if _expiry_inflight(state, now):
        return False, "an expiry-driven redemption is already in flight", None
    if _exhaustion_inflight(state, now):
        return False, "a weekly-exhaustion redemption is in flight", None
    ex = _expiry_state(state)
    failed = ex.get("failed_at")
    if _num(failed) and 0 <= now - failed < EXPIRY_RETRY_S:
        return False, "retrying a failed expiry-driven redemption later", None
    reason = f"credit expiring {when} before the Weekly reset; Weekly at {used:.0f}%"
    if not supports_credit_id:
        alerted = ex.get("alerted_unsupported")
        alerted = alerted if isinstance(alerted, list) else []
        if target["id"] not in alerted:
            ex["alerted_unsupported"] = (alerted + [target["id"]])[-20:]
            queue(state, f"⚠️ quota-router: a banked codex reset credit will lapse {when}: the plugin "
                         f"cannot reach Hermes's Codex helpers to redeem it by credit_id, so it was "
                         f"not auto-redeemed. Run /usage reset --force to spend one now.")
        return False, f"{reason}, but targeted (credit_id) redemption is unavailable", None
    ex["inflight"] = {"at": now, "credit_id": target["id"], "claim": reason}
    return True, reason, target["id"]


def settle_expiry_redeem(state: dict, now: float, reason: str, outcome: str, credit_id: Optional[str],
                         settled_at: Optional[float] = None) -> None:
    """Settle a ``claim_expiry_redeem`` reservation. Must be under ``locked_state``. Success is
    recorded in ``auto_reset.expiry.redeemed`` (not the weekly cap); a failure starts the
    ``EXPIRY_RETRY_S`` back-off. Either way the credit list is marked for re-fetch and one alert
    is queued."""
    ex = _expiry_state(state)
    ex["inflight"] = None
    if outcome == "reset":
        redeemed = ex.get("redeemed")
        redeemed = [r for r in redeemed if isinstance(r, dict) and _num(r.get("at")) and r["at"] >= now - 60 * 86400] \
            if isinstance(redeemed, list) else []
        ex["redeemed"] = redeemed + [{"at": now, "credit_id": credit_id}]
        ex.pop("failed_at", None)
    else:
        ex["failed_at"] = now
    state["auto_reset"]["last"] = {"at": now, "reason": reason, "outcome": outcome, "kind": "expiry"}
    _mark_reset_credits_stale(state, now if settled_at is None else max(now, settled_at))
    verb = "auto-redeemed" if outcome == "reset" else f"auto-redeem not done ({outcome}) for"
    queue(state, f"⚠️ quota-router: {verb} an expiring banked codex reset credit ({reason})")


def reset_credits_text(state: dict, now: float) -> Optional[str]:
    slot = _reset_credits_slot(state)
    credits = [c for c in slot.get("credits") or [] if isinstance(c, dict)
               and (c.get("expires_at") is None or (_num(c["expires_at"]) and c["expires_at"] > now))]
    if not _num(slot.get("fetched_at")):
        return None
    text = f"reset credits: {len(credits)} available"
    nxt = next((c["expires_at"] for c in credits if c.get("expires_at") is not None), None)
    if nxt is not None:
        text += f"; next expires {datetime.fromtimestamp(nxt):%a %m-%d %H:%M} (in {_fmt_in(nxt - now)})"
    return text + f"  [list {int((now - slot['fetched_at']) // 60)}m old]"


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


def _forecast_text(f: Optional[dict]) -> str:
    if not f:
        return ""
    if f.get("status") != "ok":
        return f"  forecast unknown ({f.get('reason', '?')})"
    return (f"  → {f['projected_at_reset']:.0f}% at reset ({f['rate_pct_h']:.1f}%/h, "
            f"short {f['r_short']:.1f}%/h, n={f['confidence']})")


def render_status(state: dict, now: float, cfg: dict) -> str:
    forecasts = forecast_state(state, now, cfg)
    out = [f"mode: {state.get('mode', 'normal')}"
           + (f" (since {datetime.fromtimestamp(state['mode_since']):%a %H:%M})" if state.get("mode_since") else "")]
    for p in WATCHED:
        ps = state["providers"].get(p) or {}
        snap = ps.get("snapshot") or {}
        age = now - float(snap.get("fetched_at") or 0) if snap else None
        stale = is_stale(snap, now, cfg) if snap else True
        out.append(f"{p}: tier={ps.get('tier', 'ok')}"
                   + (f"  snapshot {int(age // 60)}m old{' (STALE — ignored)' if stale else ''}" if age is not None else "  no snapshot"))
        core_windows = [w for w in (snap.get("windows") or []) if is_account_window(w)]
        scoped_windows = [w for w in (snap.get("windows") or []) if not is_account_window(w)]
        for w in core_windows:
            reset = datetime.fromtimestamp(w["resets_at"]).strftime("%a %H:%M") if w.get("resets_at") else "?"
            out.append(f"    {w['label']:<16} {w['used']:5.1f}%  resets {reset}"
                       + _forecast_text((forecasts.get(p) or {}).get(w["label"])))
        if scoped_windows:
            out.append("    model limits:")
            for w in scoped_windows:
                reset = datetime.fromtimestamp(w["resets_at"]).strftime("%a %H:%M") if w.get("resets_at") else "?"
                stale_tag = " (stale)" if w.get("stale") else ""
                out.append(f"        {w['label']:<24} {w['used']:5.1f}%  resets {reset}{stale_tag}")
        for d in snap.get("details") or []:
            out.append(f"    {d}")
        credits_line = reset_credits_text(state, now) if p == "openai-codex" else None
        if credits_line:
            out.append(f"    {credits_line}")
        b = state["blocks"].get(p)
        if active_block(b, now):
            out.append(f"    OBSERVED BLOCK until {datetime.fromtimestamp(b['until']):%a %H:%M}: {b.get('message', '')[:120]}")
        fetch = ps.get("fetch") or {}
        core_err = (fetch.get("core") or {}).get("error")
        if core_err:
            fage = int((now - float(core_err.get("at") or now)) // 60)
            since_str = datetime.fromtimestamp(core_err["since"]).strftime("%H:%M")
            out.append(f"    last fetch failed {fage}m ago (since {since_str}) — "
                       f"{core_err.get('kind')}: {(core_err.get('message') or '')[:100]}")
        scoped_err = (fetch.get("scoped") or {}).get("error")
        if scoped_err:
            fage = int((now - float(scoped_err.get("at") or now)) // 60)
            out.append(f"    scoped-limits fetch failed {fage}m ago — "
                       f"{scoped_err.get('kind')}: {(scoped_err.get('message') or '')[:100]}")
    errs = state.get("errors") or []
    if errs:
        out.append("recent quota-shaped errors:")
        for e in errs[-5:]:
            out.append(f"    {datetime.fromtimestamp(e['at']):%a %H:%M} {e['provider']} {e.get('status')} "
                       f"{e.get('reason')}: {e.get('message', '')[:100]}")
    out += render_cache(state, now)
    return "\n".join(out)


# ── throttle policy (host-local; v2 B1/B4/B5, precedence hold > disable > block > policy) ──────
#
# Each gate provider runs the fast/constrained policy below on its own windows, and keeps its own
# ``ok``/``constrained`` state, dwell and reasons (``state['throttle']['providers']``). The selected
# level is the first ladder level whose gate providers are all ``ok``; the last level has no gate.
# The legacy ``levels: {fast, constrained}`` config is the two-level ladder
# ``[fast gated by protected_provider, constrained]``.

THROTTLE_LEVELS = ("fast", "constrained")   # the legacy level names
THROTTLE_LOG_MAX = 300   # ~25 h of decisions at a 5-minute cadence (shadow report input)
THROTTLE_DEFAULTS = {
    "enabled": False,               # shadow mode: compute + log only, never write configs
    "profiles": [],
    "levels": {},                   # model specs are config, never code defaults
    "protected_provider": "anthropic",
    "ladder": None,                 # ordered levels; None means the legacy levels + protected_provider
    "prefer_short_windows": False,  # opt-in: spend replenishing session headroom before weekly-only
    "stamp_resume": False,          # opt-in: mark throttled profiles' chats to follow config on resume
    "enter_projected": 95.0,        # rule a
    "enter_used": 85.0,             # rule b
    "early_projected": 85.0,        # rule c (with acceleration)
    "accel_ratio": 1.5,
    "leave_accel_ratio": 1.25,      # rule c hysteresis: hold until r_short < this × r_long
    "leave_projected": 80.0,
    "leave_used": 75.0,
    "min_dwell_s": 30 * 60,
    "ignore_reset_within_s": 30 * 60,
    "stale_hold_s": 60 * 60,
}


def throttle_config(user: Optional[dict]) -> dict:
    """``plugins.entries.quota-router.throttle`` merged over ``THROTTLE_DEFAULTS``. ``configured``
    is false when there is no throttle block at all (a host that never opted in stays silent)."""
    t = dict(THROTTLE_DEFAULTS)
    if isinstance(user, dict):
        t.update({k: v for k, v in user.items() if k in THROTTLE_DEFAULTS})
    t["configured"] = isinstance(user, dict) and bool(user)
    return t


def level_chain(spec: dict) -> list[tuple[str, str]]:
    """``[(provider, model), ...]``: the level's primary then its fallback entries, in order."""
    chain = [(str(spec.get("provider") or ""), str(spec.get("model") or ""))]
    for f in spec.get("fallback") or []:
        chain.append((str(f.get("provider") or ""), str(f.get("model") or "")))
    return chain


def validate_levels(levels: Any, protected: str = "anthropic") -> list[str]:
    """Config errors for the two level specs; empty means valid. B1: the constrained level's whole
    chain (primary and fallback) must not contain the protected provider."""
    errs = []
    if not isinstance(levels, dict):
        return ["throttle.levels must be a mapping with 'fast' and 'constrained'"]
    for name in THROTTLE_LEVELS:
        spec = levels.get(name)
        if not isinstance(spec, dict) or not spec.get("provider") or not spec.get("model"):
            errs.append(f"throttle.levels.{name} needs provider and model")
            continue
        fb = spec.get("fallback") or []
        if not isinstance(fb, list) or not all(isinstance(f, dict) and f.get("provider") and f.get("model")
                                               for f in fb):
            errs.append(f"throttle.levels.{name}.fallback must be a list of {{provider, model}}")
    spec = levels.get("constrained")
    if isinstance(spec, dict) and not errs:
        if any(p.strip().lower() == protected for p, _m in level_chain(spec)):
            errs.append(f"throttle.levels.constrained must not contain {protected} (primary or fallback)")
    return errs


LADDER_KEYS = ("name", "gate", "provider", "model", "fallback", "max_in_progress")


def _norm_provider(p: Any) -> str:
    return str(p or "").strip().lower()


# Hermes' fallback routing (resolve_provider_client → agent.auxiliary_client._normalize_aux_provider)
# drops a "custom:" prefix and maps these aliases onto the tracked providers. The chatgpt spellings
# route to openai-codex there too; without them the check would fail open standalone.
TRACKED_ALIASES = {"claude": "anthropic", "claude-code": "anthropic",
                   "codex": "openai-codex", "chatgpt": "openai-codex", "chatgpt-codex": "openai-codex"}


def _hermes_normalizer():
    """Hermes' own fallback-routing normaliser, only when this already runs inside Hermes: importing
    Hermes' config module fresh seeds files in HERMES_HOME, so it is never imported from here."""
    if "hermes_cli.config" not in sys.modules:
        return None
    try:
        from agent.auxiliary_client import _normalize_aux_provider
    except Exception:
        return None
    return _normalize_aux_provider


def canonical_provider(name: Any) -> str:
    """The provider Hermes routes ``name`` to, as far as the tracked providers go: Hermes' own
    normaliser when available, then the built-in map either way (so an alias is never missed)."""
    n = _norm_provider(name)
    normalize = _hermes_normalizer()
    if normalize is not None and n != "main":    # "main" reads Hermes' config; the ladder resolves it
        try:
            n = _norm_provider(normalize(n)) or n
        except Exception:
            pass
    if n.startswith("custom:"):
        n = n.split(":", 1)[1].strip() or "custom"
    return TRACKED_ALIASES.get(n, n)


def throttle_ladder(t: dict) -> list[dict]:
    """The levels in preference order: ``[{'name', 'gate': [provider, ...], 'spec': {...}}]``.

    ``throttle.ladder`` as configured, or the legacy ``levels`` as ``[fast gated by
    protected_provider, constrained]``. Tolerant of a malformed ladder, which ``validate_throttle``
    reports (and ``run_apply`` refuses); never empty, so a decision can always be made and logged."""
    raw = t.get("ladder")
    rungs = []
    for i, entry in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(entry, dict):
            continue
        gate = entry.get("gate")
        gate = [canonical_provider(g) for g in gate if isinstance(g, str) and g.strip()] if isinstance(gate, list) else []
        rungs.append({"name": str(entry.get("name") or f"level{i}"), "gate": list(dict.fromkeys(gate)),
                      "spec": {k: v for k, v in entry.items() if k not in ("name", "gate")}})
    if rungs:
        return rungs
    levels = t.get("levels") if isinstance(t.get("levels"), dict) else {}
    return [{"name": "fast", "gate": [t.get("protected_provider")], "spec": levels.get("fast") or {}},
            {"name": "constrained", "gate": [], "spec": levels.get("constrained") or {}}]


def throttle_level_names(t: dict) -> list[str]:
    return [r["name"] for r in throttle_ladder(t)]


def gate_providers(rungs: list[dict]) -> list[str]:
    """Every provider that gates a level, in ladder order."""
    return list(dict.fromkeys(p for r in rungs for p in r["gate"]))


def throttle_apply_levels(t: dict) -> Any:
    """What ``apply-one`` receives: the legacy ``levels`` mapping unchanged, or the ladder list."""
    return t.get("levels") if t.get("ladder") is None else t.get("ladder")


def validate_ladder(ladder: Any, cfg: Optional[dict] = None) -> list[str]:
    """Config errors for ``throttle.ladder``; empty means valid.

    B1 generalised: a provider that gates ANY level may appear in a level's chain (primary or
    fallback) only if it also gates that level, so a level never fails over onto a provider that
    is known to be capped. Providers are compared as Hermes routes them (``canonical_provider``:
    ``claude`` is ``anthropic``, ``codex`` is ``openai-codex``, a ``custom:`` prefix is dropped), and
    a ``main`` fallback is the level's own primary. The last level is the floor and has no gate; no
    other level may be ungated, and no level's gate may include every gate provider of an earlier
    level (either way policy could never select it). Gate providers must be tracked, and (with
    ``cfg``) have forecast windows."""
    if not isinstance(ladder, list):
        return ["throttle.ladder must be a list of levels, highest preference first"]
    if len(ladder) < 2:
        return ["throttle.ladder needs at least two levels (the last one is the floor)"]
    errs, names = [], []
    for i, entry in enumerate(ladder):
        if not isinstance(entry, dict):
            errs.append(f"throttle.ladder[{i}] must be a mapping")
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            errs.append(f"throttle.ladder[{i}] needs a name")
            continue
        if name in names:
            errs.append(f"throttle.ladder has two levels named {name!r}")
        names.append(name)
        where = f"throttle.ladder.{name}"
        unknown = sorted(k for k in entry if k not in LADDER_KEYS)
        if unknown:
            errs.append(f"{where} has unknown keys: {', '.join(map(str, unknown))}")
        if not entry.get("provider") or not entry.get("model"):
            errs.append(f"{where} needs provider and model")
        fb = entry.get("fallback") or []
        if not isinstance(fb, list) or not all(isinstance(f, dict) and f.get("provider") and f.get("model")
                                               for f in fb):
            errs.append(f"{where}.fallback must be a list of {{provider, model}}")
        gate = entry.get("gate") or []
        if not isinstance(gate, list) or not all(isinstance(g, str) and g.strip() for g in gate):
            errs.append(f"{where}.gate must be a list of providers")
            continue
        for g in gate:
            if canonical_provider(g) not in WATCHED:
                errs.append(f"{where}.gate: {g} is not a tracked provider ({', '.join(WATCHED)})")
    if errs:
        return errs
    rungs = throttle_ladder({"ladder": ladder})
    if rungs[-1]["gate"]:
        errs.append(f"throttle.ladder.{rungs[-1]['name']} is the floor and must have no gate")
    for r in rungs[:-1]:
        if not r["gate"]:
            errs.append(f"throttle.ladder.{r['name']} has no gate, so no level after it can be selected; "
                        "only the last level may be ungated")
    for i, r in enumerate(rungs):
        earlier = next((e for e in rungs[:i] if e["gate"] and set(e["gate"]) <= set(r["gate"])), None)
        if r["gate"] and earlier:
            errs.append(f"throttle.ladder.{r['name']} can never be selected: its gate includes every gate "
                        f"provider of {earlier['name']}, which comes first")
    gated_by = {p: [r["name"] for r in rungs if p in r["gate"]] for p in gate_providers(rungs)}
    kanbanned = [r["name"] for r in rungs if isinstance(r["spec"].get("max_in_progress"), int)
                 and not isinstance(r["spec"].get("max_in_progress"), bool)]
    if kanbanned and len(kanbanned) != len(rungs):
        # `max_in_progress` applies to all levels or none: a partial set silently makes
        # kanban_managed() False, and kanban stops being managed with nothing on screen.
        missing = ", ".join(r["name"] for r in rungs if r["name"] not in kanbanned)
        errs.append(f"throttle.ladder: max_in_progress must be set on every level or on none; "
                    f"{missing} {'has' if len(missing.split(', ')) == 1 else 'have'} none")
    for r in rungs:
        chain = level_chain(r["spec"])
        primary = canonical_provider(chain[0][0])
        for raw in dict.fromkeys(p for p, _m in chain):
            p = canonical_provider(raw)
            p = primary if p == "main" else p
            if p in gated_by and p not in r["gate"]:
                spelled = "" if _norm_provider(raw) == p else f", written {raw.strip()!r}"
                errs.append(f"throttle.ladder.{r['name']} must not contain {p} (primary or fallback{spelled}): "
                            f"{p} gates {', '.join(gated_by[p])} but not {r['name']}")
    if cfg is not None:
        windows = cfg.get("forecast_windows") or {}
        for p, levels in gated_by.items():
            if not windows.get(p):
                errs.append(f"throttle.ladder.{levels[0]} gates on {p}, which has no forecast_windows, "
                            "so its usage can never constrain it")
    return errs


def legacy_alias_warnings(t: dict) -> list[str]:
    """``levels:`` configs, the names-as-written gap: like 0.3.0, ``validate_levels`` compares
    provider names as written, so an alias in a chain or in ``protected_provider`` is accepted but
    never matches the forecast windows (keyed by the canonical name) and never throttles. A warning
    keeps every decision and log record byte-identical; rejecting would change 0.3.0 behaviour."""
    errs = validate_levels(t.get("levels"), t["protected_provider"])
    if errs or t.get("ladder") is not None or not isinstance(t.get("levels"), dict):
        return []
    warns = []
    protected = _norm_provider(t.get("protected_provider"))
    if protected and canonical_provider(protected) in WATCHED and protected not in WATCHED:
        warns.append(f"throttle.protected_provider {t['protected_provider']!r} is the alias of "
                     f"{canonical_provider(protected)}; the forecast windows are keyed "
                     f"{canonical_provider(protected)}, so it never throttles")
    for name in THROTTLE_LEVELS:
        spec = t["levels"].get(name)
        if not isinstance(spec, dict):
            continue
        for raw, _m in level_chain(spec):
            p = _norm_provider(raw)
            if p and canonical_provider(p) in WATCHED and p not in WATCHED:
                warns.append(f"throttle.levels.{name} contains {raw.strip()!r}, the alias of "
                             f"{canonical_provider(p)}; the legacy check compares names as written, "
                             f"so write {canonical_provider(p)} or use ladder:")
    return list(dict.fromkeys(warns))


def validate_throttle(t: dict, cfg: Optional[dict] = None) -> list[str]:
    """Config errors for the throttle block: the ladder rules, or exactly the legacy B1 check."""
    if not isinstance(t.get("prefer_short_windows", False), bool):
        return ["throttle.prefer_short_windows must be a boolean"]
    if not isinstance(t.get("stamp_resume", False), bool):
        return ["throttle.stamp_resume must be a boolean"]
    if t.get("ladder") is not None:
        if t.get("levels"):
            return ["set throttle.ladder or throttle.levels, not both"]
        return validate_ladder(t["ladder"], cfg)
    return validate_levels(t.get("levels"), t["protected_provider"])


def _valid_usage(w: dict) -> bool:
    used = w.get("used")
    return isinstance(used, (int, float)) and not isinstance(used, bool) and math.isfinite(used) and used >= 0


def _model_window(w: dict, models: Iterable[str]) -> bool:
    """Recognised Anthropic family caps apply only to chains using that family."""
    scope = str(w.get("scope") or "").lower()
    if scope not in ("model", "opus", "sonnet", "haiku", "fable"):
        return False
    for model in models:
        family = re.search(r"(?:^|/)claude-(opus|sonnet|haiku|fable)(?:-|$)", model.lower())
        if family and (scope == family[1] or (scope == "model" and
                       re.search(r"\b" + family[1] + r"\b", str(w.get("label") or "").lower()))):
            return True
    return False


def _throttle_windows(state: dict, now: float, cfg: dict, provider: str,
                      models: list[str] | None = None) -> tuple[list, Optional[float]]:
    """Last-known tier-affecting windows for ``provider`` joined with their forecasts, plus the
    snapshot's ``fetched_at``."""
    snap = ((state.get("providers") or {}).get(provider) or {}).get("snapshot") or {}
    labels = (cfg.get("forecast_windows") or {}).get(provider) or {}
    fc = forecast_state(state, now, cfg).get(provider) or {}
    wins = [dict(w, forecast=fc.get(w["label"])) for w in snap.get("windows") or []
            if (models is None or _valid_usage(w)) and (
                (w["label"] in labels and is_account_window(w))
                or (provider == "anthropic" and models is not None and _model_window(w, models)))]
    if provider == "openai-codex" and _auto_redeem(state, now, cfg, planning=True)[0]:
        for w in wins:
            if w["label"] == "Weekly":
                # Forecasts cap at 200%, so credit at most one refill even for a larger bank.
                w["projection_allowance"] = 100.0
    return wins, (float(snap["fetched_at"]) if snap.get("fetched_at") else None)


def _accelerating(w: dict, ratio: float, t: dict) -> bool:
    """Rule (c)'s condition at ``ratio``: recent burn ≥ ``ratio`` × the long rate with the long
    projection ≥ ``early_projected`` (and, under a projection allowance, the short one too)."""
    f = w.get("forecast") or {}
    allowance = w.get("projection_allowance", 0.0)
    return (f.get("status") == "ok" and f["rate_pct_h"] > 0 and f["r_short"] >= ratio * f["rate_pct_h"]
            and f["projected_at_reset"] >= t["early_projected"] + allowance
            and (not allowance or f["projected_short"] >= t["early_projected"] + allowance))


def throttle_policy(level: str, since: float, windows: list, now: float, t: dict
                    ) -> tuple[str, Optional[str], Optional[str]]:
    """Pure fresh-data policy: ``(level, rule, window_label)``. Rule a requires both long- and
    short-rate projections ≥ ``enter_projected``. Rules a/b/c cannot escalate a window resetting
    within ``ignore_reset_within_s``; leaving needs every window's short-rate projection ≤
    ``leave_projected`` (an unknown forecast counts as satisfied) and used < ``leave_used``, and no
    window still accelerating at ``leave_accel_ratio`` (rule c's hysteresis, capped at
    ``accel_ratio``; windows resetting within ``ignore_reset_within_s`` are skipped as on entry),
    after ``min_dwell_s``."""
    if level != "constrained":
        for w in windows:
            if w.get("resets_at") and w["resets_at"] - now < t["ignore_reset_within_s"]:
                continue
            f = w.get("forecast") or {}
            ok = f.get("status") == "ok"
            allowance = w.get("projection_allowance", 0.0)
            # A high cycle-average projection can outlast a burst after recent burn subsides.
            # Require recent-rate confirmation too; otherwise short-rate recovery immediately
            # re-enters on the old long projection, producing enter/dwell/leave oscillation.
            if ok and f["projected_at_reset"] >= t["enter_projected"] + allowance \
                    and f["projected_short"] >= t["enter_projected"] + allowance:
                return "constrained", "a", w["label"]
            if w["used"] >= t["enter_used"]:
                return "constrained", "b", w["label"]
            if _accelerating(w, t["accel_ratio"], t):
                return "constrained", "c", w["label"]
        return "fast", None, None
    for w in windows:
        f = w.get("forecast") or {}
        if w["used"] >= t["leave_used"]:
            return "constrained", "hold_used", w["label"]
        if (f.get("status") == "ok"
                and f["projected_short"] > t["leave_projected"] + w.get("projection_allowance", 0.0)):
            return "constrained", "hold_projected", w["label"]
    # Rule (c)'s entry is invisible to the projection/used checks above, so without this the level
    # left on the very data that entered it and re-entered at the next refresh (flapping).
    leave_ratio = min(t["leave_accel_ratio"], t["accel_ratio"])
    for w in windows:
        if w.get("resets_at") and w["resets_at"] - now < t["ignore_reset_within_s"]:
            continue
        if _accelerating(w, leave_ratio, t):
            return "constrained", "hold_accel", w["label"]
    if now - since < t["min_dwell_s"]:
        return "constrained", "dwell", None
    return "fast", "leave", None


def throttle_hold(state: dict, level: str, until: float, now: float,
                  names: Iterable[str] = THROTTLE_LEVELS) -> None:
    """Force ``level`` (one of ``names``, the ladder's level names) until ``until``."""
    if level not in tuple(names):
        raise ValueError(f"level must be one of {tuple(names)}")
    state.setdefault("throttle", {})["hold"] = {"level": level, "until": float(until), "since": now}


def throttle_clear_hold(state: dict) -> None:
    state.setdefault("throttle", {}).pop("hold", None)


def throttle_disable(state: dict, level: Optional[str], now: float,
                     names: Iterable[str] = THROTTLE_LEVELS) -> None:
    """Force ``level`` (normally the first level, ``fast``) until cleared; ``None`` clears the disable."""
    th = state.setdefault("throttle", {})
    if level is None:
        th.pop("disabled", None)
        return
    if level not in tuple(names):
        raise ValueError(f"level must be one of {tuple(names)}")
    th["disabled"] = {"level": level, "since": now}


def _why(source: str, rule: Optional[str], label: Optional[str], windows: list) -> str:
    if source == "policy" and label:
        w = next((w for w in windows if w["label"] == label), None) or {}
        f = w.get("forecast") or {}
        bits = [f"{label} {w.get('used', 0):.0f}%"]
        if f.get("status") == "ok":
            reset = datetime.fromtimestamp(f["resets_at"]).strftime("%a %H:%M")
            bits.append(f"projected {f['projected_at_reset']:.0f}% at reset {reset}, "
                        f"rate {f['rate_pct_h']:.1f}%/h")
        return ", ".join(bits) + f"; rule {rule}"
    if source == "policy":
        return f"rule {rule}"
    if source == "preference":
        return f"short window {label}" if rule == "short_window" else "provider preference dwell"
    return {"hold": "manual hold", "disable": "throttle disabled", "block": "observed quota block",
            "stale": "stale usage data"}.get(source, source)


def _provider_policy(state: dict, provider: str, level: str, since: float, windows: list,
                     fetched_at: Optional[float], alerted_for: Any, now: float, cfg: dict, t: dict) -> tuple:
    """One provider's fast/constrained decision below hold/disable: observed block > stale rules >
    policy. Returns ``(level, source, rule, label, stale)``; ``stale`` is ``(age_s, window)`` when a
    stale-data escalation alert is due for this snapshot (``alerted_for`` dedupes it)."""
    rule = label = stale = None
    stale_age = None if fetched_at is None else now - fetched_at
    if active_block((state.get("blocks") or {}).get(provider), now):
        new, source = "constrained", "block"
    elif stale_age is None or stale_age > t["stale_hold_s"]:
        known = [w for w in windows if not (w.get("resets_at") and w["resets_at"] <= now)]
        high = [w for w in known if w["used"] >= t["leave_used"]]
        new, source = ("constrained" if high else "fast"), "stale"
        if high and alerted_for != fetched_at:
            stale = (stale_age or 0, high[0])
    elif stale_age > cfg["stale_after_s"]:
        new, source = level, "stale_hold"
    else:
        new, rule, label = throttle_policy(level, since, windows, now, t)
        source = "policy"
    return new, source, rule, label, stale


def throttle_provider_states(th: dict, t: dict, gates: Iterable[str]) -> dict:
    """``th['providers']``, created on first use; hold and disable stay where they are.

    The legacy config (no ``ladder``) keeps 0.3.0's model: one ``level`` and ``since`` for whatever
    the protected provider is now (a missing level is 0.3.0's default, ``fast``). So its record is
    re-derived from ``th`` (state, ``since``, stale-alert dedupe) whenever it disagrees with that
    level or was decided at a different time than ``th`` was. That covers a 0.3.0 state file, a
    ``protected_provider`` switched on a live config (the level and its dwell carry over), and a
    downgrade and back (0.3.0 moves ``th`` but not ``providers``). For a ladder, a 0.3.0 record
    seeds the protected provider once, and again if 0.3.0 decided after this code last did.

    Only ``fast``/``constrained`` describe the protected provider: a recorded ladder level (a
    ``ladder:`` config reverted to ``levels:``) says nothing about it, so its own state is kept."""
    per = th.get("providers")
    per = per if isinstance(per, dict) else {}
    protected = t.get("protected_provider")
    recorded, decided = th.get("level"), th.get("decided_at")
    if t.get("ladder") is None:
        level = "fast" if recorded is None else recorded
        rec = per.get(protected) if isinstance(per.get(protected), dict) else {}
        stale = level in THROTTLE_LEVELS and (
            rec.get("state") != ("constrained" if level == "constrained" else "ok") or rec.get("decided_at") != decided)
    else:
        level = recorded
        stamps = [float(ps.get("decided_at") or 0) for ps in per.values() if isinstance(ps, dict)]
        stale = level in THROTTLE_LEVELS and (
            not per or (decided is not None and max(stamps, default=0.0) < float(decided)))
    if stale:
        rec = {"state": "constrained" if level == "constrained" else "ok"}
        for key in ("since", "source", "rule", "stale_alerted_for", "decided_at"):
            if th.get(key) is not None:
                rec[key] = th[key]
        per[protected] = rec
    th["providers"] = per
    for p in gates:
        if not isinstance(per.get(p), dict):
            per[p] = {"state": "ok"}
    return per


def forced_provider_states(rungs: list[dict], index: int, states: dict) -> dict:
    """The provider states a forced level implies: its gate providers ``ok``, and every earlier
    level made unselectable by marking its other gate providers ``constrained``. For the legacy
    ladder: forcing ``fast`` means the protected provider is ok, forcing ``constrained`` that it
    is constrained, exactly as the single 0.3.0 level did."""
    out = dict(states)
    own = set(rungs[index]["gate"])
    for p in own:
        out[p] = "ok"
    for r in rungs[:index]:
        if all(out.get(p) == "ok" for p in r["gate"]):
            for p in r["gate"]:
                if p not in own:
                    out[p] = "constrained"
    return out


def select_level(rungs: list[dict], states: dict) -> str:
    """The first level whose gate providers are all ``ok`` (the floor, if none is)."""
    for r in rungs:
        if all(states.get(p) == "ok" for p in r["gate"]):
            return r["name"]
    return rungs[-1]["name"]


def _short_window(state: dict, provider: str, now: float, cfg: dict, t: dict) -> str | None:
    """Positive evidence of usable, fresh account session capacity; absence is unknown, not zero.

    Zero usage without a reset is the provider's idle session, whose clock starts on first use.
    A running window must have a future reset consistent with its configured session duration.
    """
    snap = ((state.get("providers") or {}).get(provider) or {}).get("snapshot") or {}
    at = snap.get("fetched_at")
    if (not isinstance(at, (int, float)) or not math.isfinite(at)
            or not 0 <= now - at <= cfg["stale_after_s"] or snap.get("unavailable")):
        return None
    kinds = (cfg.get("forecast_windows") or {}).get(provider) or {}
    for w in snap.get("windows") or []:
        label = w.get("label")
        if (kinds.get(label) != "session" or not is_account_window(w) or w.get("stale")
                or not _valid_usage(w) or w["used"] >= min(100, t["enter_used"])):
            continue
        period = (cfg.get("forecast_kinds", {}).get("session") or {}).get("period_s", 0)
        if not isinstance(period, (int, float)) or not 0 < period <= 5 * 3600:
            continue
        reset = w.get("resets_at")
        if reset is None and w["used"] == 0:
            return label
        if (isinstance(reset, (int, float)) and not isinstance(reset, bool) and math.isfinite(reset)
                and 0 < reset - now <= period + cfg["cycle_tolerance_s"]):
            return label
    return None


def _preferred_level(state: dict, rungs: list[dict], per: dict, level: str | None,
                     now: float, cfg: dict, t: dict) -> tuple[str, str | None, str | None]:
    """Rank only already-eligible gated levels; static order breaks ties, dwell avoids flapping."""
    eligible = [r for r in rungs if r["gate"] and all(per[p]["state"] == "ok" for p in r["gate"])]
    if not eligible:
        return rungs[-1]["name"], None, None
    preferred, label = eligible[0], None
    for r in eligible:
        provider = canonical_provider(r["spec"]["provider"])
        # A fallback's replenishing budget must not promote a weekly-only primary.
        if provider in r["gate"] and (short := _short_window(state, provider, now, cfg, t)):
            preferred, label = r, f"{provider}: {short}"
            break
    th = state.get("throttle") or {}
    if (level is not None and preferred["name"] != level and any(r["name"] == level for r in eligible)
            and th.get("level") == level and th.get("since") is not None
            and now - float(th["since"]) < t["min_dwell_s"]):
        return level, "preference_dwell", label
    return preferred["name"], "short_window" if label else None, label


def _window_summary(windows: list) -> dict:
    wsum = {}
    for w in windows:
        f = w.get("forecast") or {}
        ok = f.get("status") == "ok"
        wsum[w["label"]] = {"used": w["used"], "projected": f.get("projected_at_reset") if ok else None,
                            "projected_short": f.get("projected_short") if ok else None,
                            "r_long": f.get("rate_pct_h") if ok else None,
                            "r_short": f.get("r_short") if ok else None}
        if w.get("projection_allowance"):
            wsum[w["label"]]["projection_allowance"] = w["projection_allowance"]
    return wsum


def throttle_step(state: dict, now: float, cfg: dict, t: dict) -> dict:
    """Decide the throttle level, record it in ``state['throttle']`` and queue alerts. Returns the
    decision. Pure over ``state``.

    Precedence: hold > disable > (per provider) observed block > stale rules > policy. A forced
    level (hold/disable) sets the provider states it implies (``forced_provider_states``) and
    freezes the gate providers of that level and every level before it; the others keep following
    their policy. Otherwise each gate provider follows its own policy and the first level whose
    gate providers are all ``ok`` is selected.

    Alerts: with one gate provider (the legacy config) one line per level change, exactly as
    0.3.0 wrote it. With several, one line per provider state change and one per level change.
    A stale-data escalation alert replaces that provider's own line."""
    th = state.setdefault("throttle", {})
    rungs = throttle_ladder(t)
    names = [r["name"] for r in rungs]
    gates = gate_providers(rungs)
    single = len(gates) == 1
    tag = "" if t.get("enabled") else " (shadow)"
    recorded = th.get("level")
    # A recorded level this config doesn't have (a ladder name after a revert to levels:, or the
    # reverse) is a level change like any other: it moves profiles, so it is alerted.
    unknown = recorded is not None and recorded not in names
    level = recorded if recorded in names or unknown else names[0]
    pstates = throttle_provider_states(th, t, gates)
    hold = th.get("hold")
    if hold and float(hold.get("until") or 0) <= now:
        th.pop("hold", None)
        hold = None
    ignored = [f"{kind} {rec.get('level')!r}" for kind, rec in (("hold", hold), ("disable", th.get("disabled")))
               if rec and rec.get("level") not in names]
    if ignored and th.get("ignored_alerted") != ignored:
        queue(state, f"quota-router: throttle{tag} ignoring {' and '.join(ignored)}: "
                     f"not a ladder level ({', '.join(names)})")
    if ignored:
        th["ignored_alerted"] = ignored
    else:
        th.pop("ignored_alerted", None)
    forced = None
    if hold and hold.get("level") in names:
        forced = (hold["level"], "hold")
    elif th.get("disabled") and th["disabled"].get("level") in names:
        forced = (th["disabled"]["level"], "disable")
    current = {p: "constrained" if pstates[p].get("state") == "constrained" else "ok" for p in gates}
    frozen, implied = set(), current
    if forced:
        index = names.index(forced[0])
        frozen = {p for r in rungs[:index + 1] for p in r["gate"]}
        implied = forced_provider_states(rungs, index, current)
    per = {}
    for p in gates:
        ps = pstates[p]
        models = [m for r in rungs for provider, m in level_chain(r["spec"])
                  if canonical_provider(provider if provider != "main" else r["spec"]["provider"]) == p
                  ] if t.get("prefer_short_windows") else None
        if models is not None:
            snap = ((state.get("providers") or {}).get(p) or {}).get("snapshot") or {}
            if snap and not snap.get("unavailable"):
                append_history(state, p, snap, now, models=models if p == "anthropic" else [])
        windows, fetched_at = _throttle_windows(state, now, cfg, p, models)
        rule = label = stale = None
        if p in frozen:
            new_state, source = implied[p], forced[1]
        else:
            lv, source, rule, label, stale = _provider_policy(
                state, p, "constrained" if current[p] == "constrained" else "fast", float(ps.get("since") or 0.0),
                windows, fetched_at, ps.get("stale_alerted_for"), now, cfg, t)
            new_state = "constrained" if lv == "constrained" else "ok"
        if stale:
            ps["stale_alerted_for"] = fetched_at
        if source != "stale":
            ps.pop("stale_alerted_for", None)
        changed = new_state != current[p]
        if changed:
            ps["since"] = now
        elif not ps.get("since"):
            ps["since"] = now
        ps.update(state=new_state, source=source, rule=rule, label=label, decided_at=now)
        per[p] = {"state": new_state, "previous": current[p], "changed": changed, "source": source,
                  "rule": rule, "label": label, "windows": _window_summary(windows),
                  "_raw": windows, "_stale": stale}
    new = forced[0] if forced else select_level(rungs, {p: per[p]["state"] for p in gates})
    if forced:
        source, rule, label = forced[1], None, None
    elif single:
        only = per[gates[0]]
        source, rule, label = only["source"], only["rule"], only["label"]
    else:
        source, rule, label = "policy", None, None
    if not forced and t.get("prefer_short_windows"):
        new, preference_rule, preference_label = _preferred_level(state, rungs, per, level, now, cfg, t)
        if preference_rule:
            source, rule, label = "preference", preference_rule, preference_label
    changed = new != level
    note = f"{level} is not a level of this config" if unknown else None
    if single:
        only = per[gates[0]]
        if only["_stale"]:  # names the transition itself, so no second line
            age, w = only["_stale"]
            queue(state, f"quota-router: throttle{tag} stale usage data ({int(age // 60)}m old); "
                         f"{level} → {new} ({w['label']} last {w['used']:.0f}%)")
        elif changed:
            queue(state, f"quota-router: throttle{tag} {level} → {new} "
                         f"({note or _why(source, rule, label, only['_raw'])})")
    else:
        for p in gates:
            d = per[p]
            if d["_stale"]:
                age, w = d["_stale"]
                queue(state, f"quota-router: throttle{tag} stale usage data for {p} ({int(age // 60)}m old); "
                             f"{d['previous']} → constrained ({w['label']} last {w['used']:.0f}%)")
            elif d["changed"]:
                queue(state, f"quota-router: throttle{tag} {p} {d['previous']} → {d['state']} "
                             f"({_why(d['source'], d['rule'], d['label'], d['_raw'])})")
        if changed:
            why = note or ", ".join(f"{p} {per[p]['state']}" for p in gates if per[p]["changed"]) or \
                _why(source, rule, label, [])
            queue(state, f"quota-router: throttle{tag} {level} → {new} ({why})")
    if changed:
        th["since"] = now
    elif not th.get("since"):
        th["since"] = now
    th.update(level=new, source=source, rule=rule, decided_at=now)
    # With one gate provider, keep its stale-alert dedupe where 0.3.0 reads it too, so a 0.3.0 run on
    # this state (a downgrade) does not send the stale alert again.
    alerted_for = pstates[gates[0]].get("stale_alerted_for") if single else None
    if alerted_for is not None:
        th["stale_alerted_for"] = alerted_for
    else:
        th.pop("stale_alerted_for", None)
    for d in per.values():
        d.pop("_raw")
        d.pop("_stale")
    decision = {"level": new, "previous": level, "changed": changed, "source": source, "rule": rule,
                "label": label, "windows": per[gates[0]]["windows"] if single else {}, "at": now,
                "providers": per}
    rec = {k: decision[k] for k in ("at", "level", "source", "rule", "label", "windows")}
    if not single:
        rec["providers"] = {p: {k: d[k] for k in ("state", "source", "rule", "label", "windows")}
                            for p, d in per.items()}
    log = th.get("log") if isinstance(th.get("log"), list) else []
    th["log"] = (log + [rec])[-THROTTLE_LOG_MAX:]
    return decision
