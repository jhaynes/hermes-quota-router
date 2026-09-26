#!/usr/bin/env python3
"""Regenerate the widget's CLI-output fixture from REAL ``status --json`` output.

    hermes quota-router status --json --refresh > /tmp/out.txt 2>&1
    python3 scripts/make_widget_fixture.py /tmp/out.txt > desktop/tests/fixtures/status_v1_cli_output.txt

Takes the last ``QRJSON`` line, replaces the host name and session titles with neutral
placeholders (they are host-specific), and wraps it in the kind of stderr noise ``cli.exec``
joins into its output, so the widget's parser is tested against the real contract.
"""
import json
import sys

src = open(sys.argv[1], encoding="utf-8").read().splitlines()
line = [ln for ln in src if ln.startswith("QRJSON ")][-1]
payload = json.loads(line[len("QRJSON "):])
payload["host"] = "host-a"
for i, t in enumerate((payload.get("cache") or {}).get("top") or [], 1):
    t["title"] = f"session {i}"
print("WARNING: plugin loader: 1 plugin skipped (disabled)")
print("quota-router: anthropic ok → tight")
print("QRJSON " + json.dumps(payload))
print("INFO: cli.exec finished in 2.1s")
