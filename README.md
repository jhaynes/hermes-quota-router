# quota-router

Host-level quota awareness for `anthropic` and `openai-codex`.

- **Observed blocks:** `api_request_error` records a hard quota error, and the next successful request from that provider clears it (`post_api_request`).
- **Usage tiers:** `/usage` snapshots are sorted into ok, tight or exhausted, with hysteresis. Thresholds are 85/75 for tight and 97/90 for exhausted, with a 15-minute dwell. A window that resets within 30 minutes can't escalate a tier.
- **Mode:** normal, shift (one provider constrained) or internal (both constrained).
- **Alerts:** at 75% and 90%, once per window per reset cycle.
- **Alert queue:** only `hermes quota-router refresh` (the cron path) **drains** and delivers pending alert/transition lines. A manual refresh (`/quota refresh`, `hermes quota-router status --refresh`) folds fresh usage in and enqueues any new lines, but leaves the queue for the next cron run to deliver — it no longer swallows them. Delivery is **at-most-once**: drained lines are removed from state immediately, so if the cron script's delivery step then fails, those lines are lost, not retried.
- **Surfaces:** `hermes quota-router status [--refresh] | refresh | path`, `/quota [refresh]`.

State lives at `<root HERMES_HOME>/cache/quota-router/state.json` and is shared by every profile on the host.

## Fetch failures

Every `refresh()` records each provider's fetch outcome per slot (`core`: the standard `/usage` fetch; `scoped`: reserved for a future model-scoped fetch, not yet wired up). A failure preserves the incident's first-failure timestamp even if the error `kind` changes mid-incident. For the `core` slot only, a failing incident that has lasted the 15-minute cron's 30–60 minute window (`stale_after_s`) queues one `quota-router: <provider> usage unavailable for <mins>m+ (<kind>: <message>)` alert, and a following success queues one `quota-router: <provider> usage fetch recovered` line. The `scoped` slot is display-only and never alerts. `hermes quota-router status` shows the current error, if any, under each provider. This never changes tier logic: a stale snapshot still fails open to `ok` regardless of fetch health.

## Deliberately out of scope

- **Error classification override.** Hermes already switches to fallback immediately on rate-limit and billing errors when the credential pool can't rotate (`agent/turn_recovery.py::route_classified_error`).
- **Routing before a session starts.** There is no hook for it yet. Actual routing comes from the `fallback_providers` chains in root and worker profiles. Routing before start needs an upstream `resolve_session_route`-style hook (not yet proposed).

## Install on a host

```
hermes plugins install jhaynes/hermes-quota-router --enable
# optional: share with named profiles (one host-level state file either way)
for p in <profiles>; do mkdir -p ~/.hermes/profiles/$p/plugins; ln -sfn ~/.hermes/plugins/<installed-dir> ~/.hermes/profiles/$p/plugins/quota-router; hermes -p $p plugins enable quota-router; done
cp scripts/quota-router-refresh.py ~/.hermes/scripts/
hermes cron create "every 15m" --name "Quota router: usage refresh + alerts" --script quota-router-refresh.py --no-agent --deliver local
```

## Tests

```
~/.hermes/hermes-agent/venv/bin/python -m pytest -q -p no:cacheprovider tests/   # policy
~/.hermes/hermes-agent/venv/bin/python tests/e2e_live.py                          # real hooks + real anthropic call; restores state
```

## Rollback

`hermes plugins disable quota-router` (root and each profile), `hermes cron remove <id>`, then delete `cache/quota-router/`.
