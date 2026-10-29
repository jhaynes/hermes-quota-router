#!/usr/bin/env python3
"""No-agent cron job: refresh quota-router state, then run the throttle step; stdout (alerts and
transitions only) is delivered. Silent when quiet.

``throttle apply`` runs even when ``refresh`` failed, so holds expire and the stale-data rules run.
It is a no-op on a host without a ``plugins.entries.quota-router.throttle`` block, and in shadow
mode (``throttle.enabled: false``) it only decides and logs. ``throttle stamp-resume`` is a no-op
unless ``throttle.stamp_resume: true`` (README "Resumed sessions").
"""
import math
import os
import subprocess
import sys
from pathlib import Path

hermes = os.environ.get("QR_HERMES_BIN") or str(Path.home() / ".local/bin/hermes")
# QR_STEP_TIMEOUT is an ops seam: garbage must fall back to the 300 s default with a
# one-line warning, not abort every step with a traceback (issue #16). Non-finite values
# (inf/-inf/nan, which float() accepts) are also refused: an unbounded subprocess timeout
# is a hang, not a configuration. Zero/negative stay "valid" — steps then fail fast.
_raw_timeout = os.environ.get("QR_STEP_TIMEOUT") or 300
try:
    timeout = float(_raw_timeout)
except (TypeError, ValueError):
    print(f"QR_STEP_TIMEOUT={_raw_timeout!r} is not a number; using 300s")
    timeout = 300.0
else:
    if not math.isfinite(timeout):
        print(f"QR_STEP_TIMEOUT={_raw_timeout!r} is not finite; using 300s")
        timeout = 300.0
failed = False
for argv in (["refresh"], ["throttle", "apply"], ["throttle", "stamp-resume"]):
    step = f"quota-router {' '.join(argv)}"
    try:
        r = subprocess.run([hermes, "quota-router", *argv], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"{step} failed (timed out after {timeout:g}s)")
        failed = True
        continue
    except OSError as exc:
        print(f"{step} failed ({type(exc).__name__}: {exc})")
        failed = True
        continue
    out = "\n".join(l for l in r.stdout.splitlines() if l.startswith(("⚠️", "quota-router:")))
    if out:
        print(out)
    if r.returncode != 0:
        print(f"{step} failed (exit {r.returncode}): {r.stderr.strip()[-300:]}")
        failed = True
sys.exit(1 if failed else 0)
