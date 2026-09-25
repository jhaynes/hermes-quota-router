"""E2E: exercise quota-router through the REAL plugin manager and a REAL agent session.
1. api_request_error via hermes_cli.plugins.invoke_hook (real registration, real payload shape)
2. a real one-shot anthropic chat must clear a planted anthropic block via post_api_request
State file is backed up and restored around the test."""
import json, shutil, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, str(Path.home() / ".hermes/hermes-agent"))

from hermes_cli.plugins import invoke_hook, discover_plugins  # noqa
import hermes_cli.plugins as hp

state = Path.home() / ".hermes/cache/quota-router/state.json"
backup = state.with_suffix(".e2e-backup")
shutil.copy2(state, backup)
ok = True
try:
    hp._ensure_plugins_discovered(force=True)
    print("hooks registered:", hp.has_hook("api_request_error") if hasattr(hp, "has_hook") else "?")
    # 1. error path: a codex-shaped hard quota 429 on anthropic (watched provider)
    invoke_hook("api_request_error", provider="anthropic", model="claude-opus-5-5", status_code=429,
                reason="rate_limit", error={"type": "RateLimitError",
                                            "message": "429 usage_limit_reached: weekly limit reached"},
                session_id="e2e", task_id="e2e", turn_id="e2e", api_request_id="e2e")
    s = json.loads(state.read_text())
    blk = s["blocks"].get("anthropic")
    print("1. block recorded:", bool(blk), "| tier:", s["providers"]["anthropic"]["tier"], "| mode:", s["mode"])
    ok &= bool(blk) and s["providers"]["anthropic"]["tier"] == "exhausted" and s["mode"] == "internal"
    # transient 429 must not block copilot/custom and must not block on RPM text
    invoke_hook("api_request_error", provider="custom", model="glm", status_code=429, reason="rate_limit",
                error={"message": "usage limit"})
    s = json.loads(state.read_text())
    ok &= "custom" not in s["blocks"]
    # 2. success path: real anthropic call in a fresh process clears the block
    r = subprocess.run([str(Path.home() / ".local/bin/hermes"), "chat", "-q", "Reply with exactly: pong",
                        "--provider", "anthropic", "-m", "claude-sonnet-5"],
                       capture_output=True, text=True, timeout=240)
    print("2. chat exit:", r.returncode, "| tail:", (r.stdout.strip().splitlines() or [""])[-1][:80])
    s = json.loads(state.read_text())
    cleared = "anthropic" not in s["blocks"]
    print("   block cleared by real post_api_request:", cleared, "| tier:", s["providers"]["anthropic"]["tier"],
          "| mode:", s["mode"])
    ok &= cleared
    print("   tier back to ok after success:", s["providers"]["anthropic"]["tier"] == "ok")
    ok &= s["providers"]["anthropic"]["tier"] == "ok"
finally:
    shutil.copy2(backup, state)
    backup.unlink()
print("E2E", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
