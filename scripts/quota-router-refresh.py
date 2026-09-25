#!/usr/bin/env python3
"""No-agent cron job: refresh quota-router state; stdout (alerts/transitions only) is delivered. Silent when quiet."""
import subprocess
import sys
from pathlib import Path

hermes = Path.home() / ".local/bin/hermes"
r = subprocess.run([str(hermes), "quota-router", "refresh"], capture_output=True, text=True, timeout=120)
out = "\n".join(l for l in r.stdout.splitlines() if l.startswith(("⚠️", "quota-router:")))
if out:
    print(out)
if r.returncode != 0:
    print(f"quota-router refresh failed (exit {r.returncode}): {r.stderr.strip()[-300:]}")
    sys.exit(1)
