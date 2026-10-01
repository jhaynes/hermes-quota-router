"""Suite-wide offline guard for the plugin's raw Anthropic usage GET.

``refresh()`` makes its own ``urllib.request.urlopen`` call to ``/api/oauth/usage`` (scoped
limits + core-window percent overrides) whenever the anthropic core outcome is ok — including
when a test injects ``snapshots=``. Unguarded, a test then depends on the operator's live
account: a 200 overrides the injected ``used_percent`` with real numbers (the injected 80%
becomes whatever the account shows), a 429 leaves it alone. Same test, different result.

Blocking ``urlopen`` here makes every such test take the deterministic fetch-failure path
(``scoped_error``), exactly as product code handles any network error. Tests that exercise the
fetch itself already monkeypatch ``_fetch_anthropic_raw`` and are unaffected.
"""
import urllib.error
import urllib.request

import pytest


@pytest.fixture(autouse=True)
def _no_live_urlopen(monkeypatch):
    def _blocked(req, *a, **k):
        url = getattr(req, "full_url", req)
        raise urllib.error.URLError(f"tests are offline: blocked urlopen({url!r})")

    monkeypatch.setattr(urllib.request, "urlopen", _blocked)
