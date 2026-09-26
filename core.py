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
import sqlite3
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
    # cache efficiency (display + alert only; never feeds tier/mode/blocks)
    "cache_alert_below": 80.0,             # provider 24h hit % below this ...
    "cache_alert_min_tokens": 20_000_000,  # ... on at least this much 24h input -> alert
    "cache_write_alert_tokens": 10_000_000,  # one session writing this much in 24h ...
    "cache_write_alert_below": 50.0,       # ... at a hit % below this -> alert
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


def state_db_path(hermes_home: Optional[str | Path] = None) -> Path:
    """Hermes' session database for this host (read-only input to the cache section)."""
    return root_home(hermes_home) / "state.db"


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
                        "resets_at": _epoch(wget("resets_at", None) or wget("reset_at", None)),
                        "scope": wget("scope", None),
                        **({"stale": True} if wget("stale", False) else {})})
    details = [str(d) for d in (get("details") or ())]
    return {"fetched_at": _epoch(get("fetched_at")) or now, "windows": windows,
            "unavailable": get("unavailable_reason"), "plan": get("plan"), "details": details}


# ── policy ─────────────────────────────────────────────────────────────────────

def percent_tier(windows: Iterable[dict], current: str, now: float, cfg: dict) -> str:
    """Tier implied by usage percentages alone, with enter/leave hysteresis per level.

    Model-scoped windows (``scope`` set) are display+alert only and never feed the tier.
    """
    windows = [w for w in windows if not w.get("scope")]
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
    windows = [w for w in windows if not w.get("scope")]
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


# Adapted from rarf/hermes-quota-plugin quota_providers/builtin.py (MIT, © Ronald Reis et al.)
_SKIP_SCOPE_NAMES = {"opus", "sonnet"}


def parse_scoped_limits(payload: Any) -> list[dict]:
    """Extract Anthropic model-scoped weekly windows from a raw ``/api/oauth/usage`` payload.

    Only ``kind == "weekly_scoped"`` entries with a non-empty ``scope.model.display_name`` are
    kept. Entries named ``opus``/``sonnet`` (case-insensitive) are skipped: core already reports
    those and they feed the tier. ``percent`` must be a real, finite, non-bool number; it is
    clamped to 0..100 rather than rejected out of range. Duplicate names keep the highest percent.
    ``resets_at`` is normalized to an epoch float (or ``None``). Returned dicts never change the
    tier: readers must filter on ``scope`` themselves (``percent_tier``, ``block_until``).
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
    for p in WATCHED:
        ps = state["providers"].get(p) or {}
        snap = ps.get("snapshot") or {}
        fetch = ps.get("fetch") or {}
        core_fetch = fetch.get("core") or {}
        scoped_fetch = fetch.get("scoped") or {}
        windows = [{"label": w["label"], "used": w["used"], "resets_at": w.get("resets_at"),
                   "scope": w.get("scope"), "affects_tier": not w.get("scope"),
                   "stale": bool(w.get("stale"))}
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
    if ok:
        had_alerted = bool(fslot.get("error") and fslot.get("alerted"))
        fslot["last_ok_at"] = now
        fslot["error"] = None
        fslot["alerted"] = False
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
        stale = is_stale(snap, now, cfg) if snap else True
        out.append(f"{p}: tier={ps.get('tier', 'ok')}"
                   + (f"  snapshot {int(age // 60)}m old{' (STALE — ignored)' if stale else ''}" if age is not None else "  no snapshot"))
        core_windows = [w for w in (snap.get("windows") or []) if not w.get("scope")]
        scoped_windows = [w for w in (snap.get("windows") or []) if w.get("scope")]
        for w in core_windows:
            reset = datetime.fromtimestamp(w["resets_at"]).strftime("%a %H:%M") if w.get("resets_at") else "?"
            out.append(f"    {w['label']:<16} {w['used']:5.1f}%  resets {reset}")
        if scoped_windows:
            out.append("    model limits:")
            for w in scoped_windows:
                reset = datetime.fromtimestamp(w["resets_at"]).strftime("%a %H:%M") if w.get("resets_at") else "?"
                stale_tag = " (stale)" if w.get("stale") else ""
                out.append(f"        {w['label']:<24} {w['used']:5.1f}%  resets {reset}{stale_tag}")
        for d in snap.get("details") or []:
            out.append(f"    {d}")
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
