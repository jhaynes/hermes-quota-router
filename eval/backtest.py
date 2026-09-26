#!/usr/bin/env python3
"""Back-test the throttle policy over recorded usage history and a threshold grid.

Pure over history (``state['history'][provider]``) plus config variants; no model calls, no writes.
Reads the state file read-only:

    python eval/backtest.py --state ~/.hermes/cache/quota-router/state.json [--json]

Biases (said in every report): history is the usage that actually happened, so it already reflects
periods when the throttle (or a person) moved work off the provider, and it includes other hosts'
usage of the shared account. A variant that would have throttled earlier can't show the usage it
would have saved.
"""
from __future__ import annotations

import argparse
import bisect
import importlib.util
import json
import sys
from pathlib import Path

_here = Path(__file__).resolve().parent


def _core():
    mod = sys.modules.get("quota_router_core")
    if mod is None:
        spec = importlib.util.spec_from_file_location("quota_router_core", _here.parent / "core.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["quota_router_core"] = mod
        spec.loader.exec_module(mod)
    return mod


MISS_USED = 97.0              # a window at/above this is a miss if the throttle was fast in the hour before
MISS_LOOKBACK_S = 3600.0
FALSE_ALARM_BELOW = 85.0      # a constrained period whose window's cycle ended below this is a false alarm
MIN_DAYS = 7.0                # no back-test before this much history
# History is pruned to 7 days (core.HISTORY_MAX_AGE_S), so a full window always spans slightly less
# than 7.0 days; up to one hour short still counts as seven days.
MIN_DAYS_SLACK = 1 / 24.0
LOOSEN_MIN_DAYS = 21.0        # loosening proposals need three weeks of history
STEP = 5.0                    # grid step (percentage points)
BOUNDS = {"enter_projected": (85.0, 100.0), "leave_projected": (70.0, 90.0)}
TIGHTER_IS_LOWER = ("enter_projected", "leave_projected")  # lower enter = earlier; lower leave = later


def _clean(history: dict) -> dict:
    core = _core()
    out = {}
    for label, pts in (history or {}).items():
        if isinstance(pts, list):
            good = sorted((p for p in pts if core._valid_point(p)), key=lambda p: p[0])
            if good:
                out[label] = good
    return out


def replay(history: dict, blocks: list, t: dict, cfg: dict) -> list[dict]:
    """Run ``core.throttle_step`` at every recorded sample time, as if live. Returns the decision
    per step (``at``, ``level``, ``source``, ``rule``, ``label``)."""
    core = _core()
    hist = _clean(history)
    times = sorted({p[0] for pts in hist.values() for p in pts})
    idx = {label: [p[0] for p in pts] for label, pts in hist.items()}
    t = dict(t, enabled=False)
    th: dict = {}
    out = []
    for now in times:
        per, windows = {}, []
        for label, pts in hist.items():
            n = bisect.bisect_right(idx[label], now)
            if n:
                per[label] = pts[:n]
                last = pts[n - 1]
                windows.append({"label": label, "used": last[1], "resets_at": last[2]})
        st = core.empty_state()
        st["history"] = {t["protected_provider"]: per}
        st["providers"][t["protected_provider"]] = {"snapshot": {"fetched_at": now, "windows": windows}}
        st["blocks"] = {t["protected_provider"]: dict(b)} if (b := next(
            (b for b in blocks or [] if b["since"] <= now < b["until"]), None)) else {}
        st["throttle"] = th
        d = core.throttle_step(st, now, cfg, t)
        th = st["throttle"]
        th["log"] = []
        out.append({k: d[k] for k in ("at", "level", "source", "rule", "label")})
    return out


def _cycle_end_used(pts: list, at: float, cfg: dict) -> float:
    """Last recorded ``used`` of the window's cycle that was current at ``at``."""
    core = _core()
    upto = [p for p in pts if p[0] <= at]
    if not upto:
        return 0.0
    cur = upto[-1][2]
    last = upto[-1][1]
    for p in pts[len(upto):]:
        if not core._same_cycle(p[2], cur, cfg):
            break
        last = p[1]
    return last


def score(decisions: list, history: dict, blocks: list, t: dict, cfg: dict | None = None) -> dict:
    core = _core()
    cfg = cfg or dict(core.DEFAULTS)
    hist = _clean(history)
    periods, cur = [], None
    for i, d in enumerate(decisions):
        end = decisions[i + 1]["at"] if i + 1 < len(decisions) else d["at"]
        if d["level"] == "constrained":
            if cur is None:
                cur = {"start": d["at"], "end": end, "label": d.get("label"), "source": d["source"]}
            cur["end"] = end
        elif cur is not None:
            periods.append(cur)
            cur = None
    if cur is not None:
        periods.append(cur)
    hours = sum(p["end"] - p["start"] for p in periods) / 3600.0

    def fast_within(at: float) -> bool:
        return any(d["level"] == "fast" for d in decisions if at - MISS_LOOKBACK_S <= d["at"] <= at)

    misses = []
    for label, pts in hist.items():
        seen = set()
        for p in pts:
            key = p[2]
            if p[1] >= MISS_USED and key not in seen:
                seen.add(key)
                if fast_within(p[0]):
                    misses.append({"kind": "window", "label": label, "at": p[0], "used": p[1]})
    for b in blocks or []:
        if fast_within(b["since"] - 1):
            misses.append({"kind": "block", "label": None, "at": b["since"], "used": None})
    false_alarms = []
    for p in periods:
        labels = [p["label"]] if p.get("label") in hist else list(hist)
        peak = max((_cycle_end_used(hist[lb], p["start"], cfg) for lb in labels), default=0.0)
        if peak < FALSE_ALARM_BELOW:
            false_alarms.append({"start": p["start"], "label": p.get("label"), "cycle_end_used": peak})
    return {"periods": periods, "constrained_hours": hours, "opus_hours_given_up": hours,
            "misses": misses, "false_alarms": false_alarms}


def grid(t: dict) -> list[dict]:
    """Current thresholds first, then ±STEP variants, clamped to BOUNDS, with leave < enter."""
    cur = {k: float(t[k]) for k in BOUNDS}
    vals = {}
    for k, (lo, hi) in BOUNDS.items():
        vals[k] = sorted({min(max(cur[k] + d, lo), hi) for d in (-STEP, 0.0, STEP)})
    out = [cur]
    for e in vals["enter_projected"]:
        for lv in vals["leave_projected"]:
            g = {"enter_projected": e, "leave_projected": lv}
            if lv < e and g not in out:
                out.append(g)
    return out


def propose(rows: list[dict], current: dict, days: float) -> dict:
    """Best variant by (misses, constrained hours). Tightening changes are proposed automatically;
    any loosening change is labelled Justin's judgement (and needs ``LOOSEN_MIN_DAYS``)."""
    best = min(rows, key=lambda r: (r["misses"], r["constrained_hours"]))["params"]
    auto, judgement = {}, {}
    loosens = [k for k in best if best[k] > current[k]]
    for k in best:
        if best[k] < current[k] and not loosens:
            auto[k] = best[k]
    if loosens:
        judgement = {k: v for k, v in best.items() if v != current[k]}
    note = ""
    if loosens:
        note = "Loosening is Justin's judgement"
        if days < LOOSEN_MIN_DAYS:
            note += f" and needs 3 weeks of history (have {days:.1f} days)"
        note += "."
    return {"best": best, "auto": auto, "judgement": judgement, "note": note}


def run(history: dict, blocks: list, t: dict, cfg: dict) -> dict:
    hist = _clean(history)
    ts = [p[0] for pts in hist.values() for p in pts]
    days = (max(ts) - min(ts)) / 86400.0 if ts else 0.0
    if days < MIN_DAYS - MIN_DAYS_SLACK:
        return {"skipped": f"needs 7 days of history, have {days:.1f}", "days": days}
    rows = []
    for params in grid(t):
        tv = dict(t, **params)
        s = score(replay(hist, blocks, tv, cfg), hist, blocks, tv, cfg)
        rows.append({"params": params, "misses": len(s["misses"]), "false_alarms": len(s["false_alarms"]),
                     "constrained_hours": s["constrained_hours"], "detail": s})
    current = {k: float(t[k]) for k in BOUNDS}
    return {"days": days, "grid": rows, "proposal": propose(rows, current, days)}


def render_markdown(out: dict) -> str:
    lines = ["## Throttle back-test", ""]
    if out.get("skipped"):
        return "\n".join(lines + [f"Skipped: {out['skipped']}."]) + "\n"
    lines += [f"History: {out['days']:.1f} days. The back-test is biased: recorded usage already reflects "
              "constrained periods and includes other hosts' use of the shared account.", "",
              "| enter_projected | leave_projected | misses | false alarms | constrained h |",
              "|---|---|---|---|---|"]
    for i, r in enumerate(out["grid"]):
        mark = " (current)" if i == 0 else ""
        lines.append(f"| {r['params']['enter_projected']:.0f}{mark} | {r['params']['leave_projected']:.0f} | "
                     f"{r['misses']} | {r['false_alarms']} | {r['constrained_hours']:.1f} |")
    p = out["proposal"]
    lines += ["", "### Proposal", ""]
    lines.append(f"- Tighten automatically: {p['auto'] or 'none'}")
    lines.append(f"- For Justin's judgement: {p['judgement'] or 'none'}")
    if p["note"]:
        lines.append(f"- {p['note']}")
    return "\n".join(lines) + "\n"


def load_state(path: Path, provider: str) -> tuple[dict, list]:
    """(history for ``provider``, observed block intervals) from a state file, read-only. Blocks
    come from the throttle log's block-sourced decisions (the state keeps no block history)."""
    core = _core()
    st = core.read_state(Path(path))
    hist = (st.get("history") or {}).get(provider) or {}
    blocks, cur = [], None
    for rec in ((st.get("throttle") or {}).get("log") or []):
        if rec.get("source") == "block":
            cur = cur or {"since": rec["at"], "until": rec["at"]}
            cur["until"] = rec["at"]
        elif cur:
            blocks.append(cur)
            cur = None
    if cur:
        blocks.append(cur)
    return (hist if isinstance(hist, dict) else {}), blocks


def main(argv: list[str] | None = None) -> int:
    core = _core()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--state", required=True, type=Path)
    ap.add_argument("--throttle-json", help="throttle config overrides as JSON (default: code defaults)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    t = core.throttle_config(json.loads(a.throttle_json) if a.throttle_json else {"profiles": []})
    hist, blocks = load_state(a.state.expanduser(), t["protected_provider"])
    out = run(hist, blocks, t, dict(core.DEFAULTS))
    if a.json:
        for r in out.get("grid") or []:
            r.pop("detail", None)
        print(json.dumps(out, indent=1))
    else:
        print(render_markdown(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
