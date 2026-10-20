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
    assert [c["id"] for c in rc["credits"]] == ["soon", "late"]
    assert core.reset_credits_due(st, NOW + 1, core.DEFAULTS)
    assert core.usable_reset_credits(st, NOW + 1, core.DEFAULTS) is None
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


def test_a_peer_redemption_settled_during_our_get_blocks_our_claim(tmp_path, monkeypatch):
    """Process A's GET is in flight while process B claims, POSTs and settles a redemption.
    A applies its (pre-redemption) list after B's settle; A must not POST on it."""
    plugin, core = _load(tmp_path / ".hermes", monkeypatch)
    calls = []
    inner = _handler(calls)

    def handle(req):
        if req.method == "GET":
            with core.locked_state(core.state_path()) as st:   # B settles mid-GET
                st.setdefault("auto_reset", {})["redeems"] = [{"at": NOW - 5, "claim": "b"}]
                core.settle_auto_redeem(st, NOW - 5, "b", "reset", settled_at=NOW + 3)
        return inner(req)
    _fake_hermes(monkeypatch, 45.0, handle, calls)
    plugin.refresh(now=NOW)
    assert [c[0] for c in calls if c[0] in ("GET", "POST")] == ["GET"]
    st = core.read_state(core.state_path())
    assert core.reset_credits_due(st, NOW + 4, core.DEFAULTS)
    assert not (st["auto_reset"].get("expiry") or {}).get("inflight")


def _no_native_credential(monkeypatch, calls, error=RuntimeError):
    """Native resolution fails the way a model-cooled pool makes it fail; explicit
    (base, key) resolution behaves like Hermes: the key back, or a refreshed key on force."""
    fake = sys.modules["agent.account_usage"]

    def resolve(base_url, api_key, *, force_refresh=False):
        calls.append(("resolve", api_key, force_refresh))
        if not api_key:
            raise error("No available openai-codex credential in credential pool")
        return ("row-tok-2" if force_refresh else api_key), base_url, None
    fake._resolve_codex_usage_credentials = resolve


def test_reset_credit_list_falls_back_to_an_eligible_pool_row_and_heals_its_401(tmp_path, monkeypatch):
    plugin, core = _load(tmp_path / ".hermes", monkeypatch)
    calls = []
    _fake_hermes(monkeypatch, 5.0, _handler(calls, first_401=True), calls)
    _no_native_credential(monkeypatch, calls)
    monkeypatch.setattr(plugin, "_codex_pool_credentials",
                        lambda: iter([("https://chatgpt.com/backend-api/codex", "row-tok")]))
    plugin.refresh(now=NOW)
    gets = [c for c in calls if c[0] == "GET"]
    assert [g[2] for g in gets] == ["Bearer row-tok", "Bearer row-tok-2"]
    assert ("resolve", "row-tok", True) in calls
    st = core.read_state(core.state_path())
    assert st["providers"]["openai-codex"]["fetch"]["reset_credits"]["error"] is None
    assert [c["id"] for c in st["providers"]["openai-codex"]["reset_credits"]["credits"]] == ["soon", "late"]


def test_reset_credit_fallback_is_only_for_an_unavailable_credential(tmp_path, monkeypatch):
    """A transient failure in native resolution must not switch to another pool account."""
    plugin, core = _load(tmp_path / ".hermes", monkeypatch)
    calls = []
    _fake_hermes(monkeypatch, 5.0, _handler(calls), calls)
    _no_native_credential(monkeypatch, calls, error=OSError)
    monkeypatch.setattr(plugin, "_codex_pool_credentials",
                        lambda: iter([("https://chatgpt.com/backend-api/codex", "row-tok")]))
    plugin.refresh(now=NOW)
    assert not [c for c in calls if c[0] in ("GET", "POST")]
    err = core.read_state(core.state_path())["providers"]["openai-codex"]["fetch"]["reset_credits"]["error"]
    assert err and "OSError" in err["message"]


def test_reset_credit_fallback_lookup_failure_keeps_the_original_resolution_error(tmp_path, monkeypatch):
    """A broken pool read inside the fallback must not mask why native resolution failed."""
    plugin, core = _load(tmp_path / ".hermes", monkeypatch)
    calls = []
    _fake_hermes(monkeypatch, 5.0, _handler(calls), calls)
    _no_native_credential(monkeypatch, calls)

    def broken_pool():
        raise ValueError("malformed persisted row")
        yield  # pragma: no cover - makes this a generator like the real helper

    monkeypatch.setattr(plugin, "_codex_pool_credentials", broken_pool)
    plugin.refresh(now=NOW)
    assert not [c for c in calls if c[0] in ("GET", "POST")]
    err = core.read_state(core.state_path())["providers"]["openai-codex"]["fetch"]["reset_credits"]["error"]
    assert err and err["message"].startswith("RuntimeError: No available openai-codex credential")
