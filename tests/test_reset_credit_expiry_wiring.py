"""refresh() wiring for the reset-credit list and expiry-driven redemption, plus the exact HTTP
shape of the targeted consume (no network: httpx.MockTransport)."""
import importlib.util
import json
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import httpx

_root = Path(__file__).resolve().parents[1]
NOW = 1_800_000_000.0
H = 3600.0


def _load(tmp_home, monkeypatch):
    core_spec = importlib.util.spec_from_file_location("qr_core_expiry_wiring", _root / "core.py")
    core_mod = importlib.util.module_from_spec(core_spec)
    monkeypatch.setitem(sys.modules, "qr_core_expiry_wiring", core_mod)
    core_spec.loader.exec_module(core_mod)
    plugin_spec = importlib.util.spec_from_file_location("qr_plugin_expiry_wiring", _root / "__init__.py")
    plugin_mod = importlib.util.module_from_spec(plugin_spec)
    monkeypatch.setitem(sys.modules, "qr_plugin_expiry_wiring", plugin_mod)
    plugin_spec.loader.exec_module(plugin_mod)
    monkeypatch.setitem(sys.modules, "quota_router_core", core_mod)
    core_mod.state_path = lambda hermes_home=None: tmp_home / "cache" / "quota-router" / "state.json"
    core_mod.state_db_path = lambda hermes_home=None: tmp_home / "state.db"

    def _cfg():
        return dict(core_mod.DEFAULTS, auto_reset={"weekly_threshold": 97.0})
    monkeypatch.setattr(plugin_mod, "_cfg", _cfg)
    return plugin_mod, core_mod


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


CREDITS = {"available_count": 2, "credits": [
    {"id": "late", "status": "available", "is_supported_by_plan": True,
     "granted_at": iso(NOW - 20 * 86400), "expires_at": iso(NOW + 10 * 86400)},
    {"id": "soon", "status": "available", "is_supported_by_plan": True,
     "granted_at": iso(NOW - 29 * 86400), "expires_at": iso(NOW + 6 * H)}]}


def _fake_hermes(monkeypatch, week_used, handler, calls):
    """agent.account_usage with a live-looking codex snapshot and real-shaped helpers whose HTTP
    goes through ``handler`` (an httpx.MockTransport handler)."""
    fake = types.ModuleType("agent.account_usage")
    codex = {"fetched_at": NOW - 5, "details": ["You have 2 resets banked - use /usage reset to activate"],
             "windows": [{"label": "Session", "used_percent": 0.0, "resets_at": NOW + 3 * H},
                         {"label": "Weekly", "used_percent": float(week_used), "resets_at": NOW + 3 * 86400}]}
    fake.fetch_account_usage = lambda p: codex if p == "openai-codex" else None
    tokens = iter(["tok-1", "tok-2", "tok-3"])

    def resolve(base_url, api_key, *, force_refresh=False):
        calls.append(("resolve", force_refresh))
        return next(tokens), "https://chatgpt.com/backend-api/codex", "acct"
    fake._resolve_codex_usage_credentials = resolve
    fake._codex_backend_urls = lambda base: ("https://chatgpt.com/backend-api/wham/usage",
                                            "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits",
                                            "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume")
    fake._codex_headers = lambda token, account: {"Authorization": f"Bearer {token}"}
    fake._codex_reset_outcome = lambda body, available: types.SimpleNamespace(
        status=body.get("code", "unexpected"), message="m")
    fake._unavailable = lambda m: types.SimpleNamespace(status="unavailable", message=m)
    fake.redeem_codex_reset_credit = lambda **kw: calls.append(("untargeted", kw)) or \
        types.SimpleNamespace(status="reset", message="m")
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.account_usage", fake)
    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))


def _handler(calls, consume_code="reset", first_401=False):
    seen = {"401": not first_401}

    def handle(req):
        calls.append((req.method, req.url.path, req.headers.get("authorization"),
                      json.loads(req.content) if req.content else None))
        if not seen["401"]:
            seen["401"] = True
            return httpx.Response(401, json={})
        if req.method == "GET":
            return httpx.Response(200, json=CREDITS)
        return httpx.Response(200, json={"code": consume_code, "windows_reset": 2})
    return handle


def test_live_refresh_gathers_the_list_then_redeems_the_expiring_credit_by_id(tmp_path, monkeypatch):
    plugin, core = _load(tmp_path / ".hermes", monkeypatch)
    calls = []
    _fake_hermes(monkeypatch, 45.0, _handler(calls), calls)
    plugin.refresh(now=NOW)
    http = [c for c in calls if c[0] in ("GET", "POST")]
    assert [(m, p) for m, p, *_ in http] == [
        ("GET", "/backend-api/wham/rate-limit-reset-credits"),
        ("POST", "/backend-api/wham/rate-limit-reset-credits/consume")]
    body = http[1][3]
    assert body["credit_id"] == "soon" and body["redeem_request_id"]
    assert not any(c[0] == "untargeted" for c in calls)
    st = core.read_state(core.state_path())
    rc = st["providers"]["openai-codex"]["reset_credits"]
    assert [c["id"] for c in rc["credits"]] == ["soon", "late"] and rc["needs_refresh"] is True
    assert st["auto_reset"]["expiry"]["redeemed"][0]["credit_id"] == "soon"
    assert any("expiring" in p for p in st["pending"])


def test_a_fresh_list_is_not_refetched(tmp_path, monkeypatch):
    plugin, core = _load(tmp_path / ".hermes", monkeypatch)
    calls = []
    _fake_hermes(monkeypatch, 5.0, _handler(calls), calls)    # below expiry_min_used: no redeem
    plugin.refresh(now=NOW)
    plugin.refresh(now=NOW + 600)
    assert [c[0] for c in calls if c[0] in ("GET", "POST")] == ["GET"]
    plugin.refresh(now=NOW + core.DEFAULTS["reset_credits_refresh_s"])
    assert [c[0] for c in calls if c[0] in ("GET", "POST")] == ["GET", "GET"]


def test_injected_snapshots_never_touch_the_network(tmp_path, monkeypatch):
    plugin, core = _load(tmp_path / ".hermes", monkeypatch)
    calls = []
    _fake_hermes(monkeypatch, 45.0, _handler(calls), calls)
    plugin.refresh(now=NOW, snapshots={})
    assert not [c for c in calls if c[0] in ("GET", "POST")]


def test_targeted_consume_retries_401_with_refreshed_credentials_and_the_same_request_id(tmp_path, monkeypatch):
    plugin, core = _load(tmp_path / ".hermes", monkeypatch)
    calls = []
    _fake_hermes(monkeypatch, 45.0, _handler(calls, first_401=True), calls)
    assert plugin._redeem_codex_credit("soon") == ("reset", "m")
    posts = [c for c in calls if c[0] == "POST"]
    assert [p[2] for p in posts] == ["Bearer tok-1", "Bearer tok-2"]
    assert posts[0][3] == posts[1][3] and posts[0][3]["credit_id"] == "soon"
    assert ("resolve", True) in calls


def test_backend_refusal_backs_off_and_keeps_the_credit_counted(tmp_path, monkeypatch):
    plugin, core = _load(tmp_path / ".hermes", monkeypatch)
    calls = []
    _fake_hermes(monkeypatch, 45.0, _handler(calls, consume_code="nothing_to_reset"), calls)
    plugin.refresh(now=NOW)
    st = core.read_state(core.state_path())
    ex = st["auto_reset"]["expiry"]
    assert ex["failed_at"] and not ex.get("redeemed")
    assert any("not done (nothing_to_reset)" in p for p in st["pending"])
