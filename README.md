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

Every `refresh()` records each provider's fetch outcome per slot (`core`: the standard `/usage` fetch; `scoped`: the Anthropic model-scoped `limits[]` fetch). A failure preserves the incident's first-failure timestamp even if the error `kind` changes mid-incident. For the `core` slot only, a failing incident that has lasted the 15-minute cron's 30–60 minute window (`stale_after_s`) queues one `quota-router: <provider> usage unavailable for <mins>m+ (<kind>: <message>)` alert, and a following success queues one `quota-router: <provider> usage fetch recovered` line. The `scoped` slot is display-only and never alerts. Kinds: `fetch_error` (an exception reached the plugin), `unavailable` (core returned an `unavailable_reason`), `no_data` (core returned nothing; core swallows its own fetch exceptions and returns `None`, so most real network/auth failures land here). `hermes quota-router status` shows the current error, if any, under each provider. This never changes tier logic: a stale snapshot still fails open to `ok` regardless of fetch health.

## Anthropic model-scoped weekly limits

Anthropic's `/api/oauth/usage` response includes a `limits[]` list with per-model weekly caps (`kind: "weekly_scoped"`, e.g. a "Fable" window). Core (`agent/account_usage.py`) does not surface these, so the plugin makes its own GET using the same OAuth token and headers core uses, throttled to at most once per 300 seconds per host. This fetch is a **permanent** part of the plugin (there is no plan to upstream it into core).

- **Display and alert only.** A scoped window shows up under a `model limits:` sub-heading in `status` and triggers the normal 75/90% alerts, but it can **never** change a provider's tier, `mode`, or an observed block's expiry. Only Opus/Sonnet weekly windows (reported by core, not this fetch) count toward the anthropic tier, unchanged from before.
- **Fail-open with carry-forward.** If the scoped fetch fails or is throttled, the last good scoped windows are kept in state and shown in `status`/`--json` with a `stale` marker, until they are older than `stale_after_s`, at which point they drop from the snapshot.
- A scoped failure never blocks or delays the core `anthropic` fetch, and is recorded independently under the `scoped` fetch slot (never alerts).

## `status --json`

`hermes quota-router status --json [--refresh]` prints exactly one `QRJSON <payload>` line to stdout (nothing else on stdout; logging goes to stderr) for the desktop widget or any other machine reader. `--refresh --json` fetches fresh data but does **not** drain the pending-alert queue — only `hermes quota-router refresh` (the cron path) does that.

The payload (`v: 1`) has `host`, `now`, `mode`/`mode_since`, and per-provider `tier`, `fetched_at`, `stale`, `plan`, `windows` (each with `affects_tier: bool` — `false` for scoped windows), `block`, `fetch.core`/`fetch.scoped` outcomes, and `details`; plus top-level `recent_errors` (last 5) and `pending_count`.

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
