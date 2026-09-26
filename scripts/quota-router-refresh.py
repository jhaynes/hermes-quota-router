#!/usr/bin/env python3
"""No-agent cron job: refresh quota-router state, then run the throttle step; stdout (alerts and
transitions only) is delivered. Silent when quiet.

``throttle apply`` runs even when ``refresh`` failed, so holds expire and the stale-data rules run.
It is a no-op on a host without a ``plugins.entries.quota-router.throttle`` block, and in shadow
mode (``throttle.enabled: false``) it only decides and logs.
"""
import os
import subprocess
import sys
from pathlib import Path

hermes = os.environ.get("QR_HERMES_BIN") or str(Path.home() / ".local/bin/hermes")
timeout = float(os.environ.get("QR_STEP_TIMEOUT") or 300)
failed = False
for argv in (["refresh"], ["throttle", "apply"]):
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
