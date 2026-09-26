# quota-router

Host-level quota awareness for `anthropic` and `openai-codex`.

- **Observed blocks:** `api_request_error` records a hard quota error, and the next successful request from that provider clears it (`post_api_request`).
- **Usage tiers:** `/usage` snapshots are sorted into ok, tight or exhausted, with hysteresis. Thresholds are 85/75 for tight and 97/90 for exhausted, with a 15-minute dwell. A window that resets within 30 minutes can't escalate a tier.
- **Mode:** normal, shift (one provider constrained) or internal (both constrained).
- **Alerts:** at 75% and 90%, once per window per reset cycle.
- **Alert queue:** only `hermes quota-router refresh` (the cron path) **drains** and delivers pending alert/transition lines. A manual refresh (`/quota refresh`, `hermes quota-router status --refresh`) folds fresh usage in and enqueues any new lines, but leaves the queue for the next cron run to deliver — it no longer swallows them. Delivery is **at-most-once**: drained lines are removed from state immediately, so if the cron script's delivery step then fails, those lines are lost, not retried.
- **Surfaces:** `hermes quota-router status [--refresh] | refresh | path | throttle <apply|status|hold|disable>`, `/quota [refresh]`.

State lives at `<root HERMES_HOME>/cache/quota-router/state.json` and is shared by every profile on the host.

- **Usage history:** every refresh appends `[fetched_at, used_pct, resets_at]` per core (non-scoped) window to `history[provider][label]`. Earlier reset cycles are kept (a `resets_at` move of more than 10 minutes, or null ↔ value, starts a new cycle); points are pruned by age (7 days) and count (2,500 per window). Malformed history is dropped, never fatal.
- **Fetch-outcome ring:** every core fetch appends `[t, provider, outcome]` (`ok`, `fetch_error`, `unavailable`, `no_data`) to `fetch_log`, capped at 24 h / 1,000 entries.
- Both keys are additive under `version: 1`; older plugin versions ignore them.

## Fetch failures

Every `refresh()` records each provider's fetch outcome per slot (`core`: the standard `/usage` fetch; `scoped`: the Anthropic model-scoped `limits[]` fetch). A failure preserves the incident's first-failure timestamp even if the error `kind` changes mid-incident. For the `core` slot only, a failing incident that has lasted the 15-minute cron's 30–60 minute window (`stale_after_s`) queues one `quota-router: <provider> usage unavailable for <mins>m+ (<kind>: <message>)` alert, and a following success queues one `quota-router: <provider> usage fetch recovered` line. The `scoped` slot is display-only and never alerts. Kinds: `fetch_error` (an exception reached the plugin), `unavailable` (core returned an `unavailable_reason`), `no_data` (core returned nothing; core swallows its own fetch exceptions and returns `None`, so most real network/auth failures land here). `hermes quota-router status` shows the current error, if any, under each provider. This never changes tier logic: a stale snapshot still fails open to `ok` regardless of fetch health.

## Anthropic model-scoped weekly limits

Anthropic's `/api/oauth/usage` response includes a `limits[]` list with per-model weekly caps (`kind: "weekly_scoped"`, e.g. a "Fable" window). Core (`agent/account_usage.py`) does not surface these, so the plugin makes its own GET using the same OAuth token and headers core uses, throttled to at most once per 300 seconds per host. This fetch is a **permanent** part of the plugin (there is no plan to upstream it into core).

- **Display and alert only.** A scoped window shows up under a `model limits:` sub-heading in `status` and triggers the normal 75/90% alerts, but it can **never** change a provider's tier, `mode`, or an observed block's expiry. Only Opus/Sonnet weekly windows (reported by core, not this fetch) count toward the anthropic tier, unchanged from before.
- **Fail-open with carry-forward.** If the scoped fetch fails or is throttled, the last good scoped windows are kept in state and shown in `status`/`--json` with a `stale` marker, until they are older than `stale_after_s`, at which point they drop from the snapshot.
- A scoped failure never blocks or delays the core `anthropic` fetch, and is recorded independently under the `scoped` fetch slot (never alerts).

## `status --json`

`hermes quota-router status --json [--refresh]` prints exactly one `QRJSON <payload>` line to stdout (nothing else on stdout; logging goes to stderr) for the desktop widget or any other machine reader. `--refresh --json` fetches fresh data but does **not** drain the pending-alert queue — only `hermes quota-router refresh` (the cron path) does that.

The payload (`v: 1`) has `host`, `now`, `mode`/`mode_since`, and per-provider `tier`, `fetched_at`, `stale`, `plan`, `windows` (each with `affects_tier: bool` — `false` for scoped windows — and `forecast`), `block`, `fetch.core`/`fetch.scoped` outcomes, and `details`; plus top-level `recent_errors` (last 5) and `pending_count`.

## Forecast

For each window in `forecast_windows` (default: Anthropic `Current session` as `session`, `Current week` and `Opus week` as `weekly`), `status` and `status --json` show a forecast computed from the current reset cycle's history:

- `rate_pct_h` (`r_long`): `session` uses the least-squares slope over the whole current cycle; `weekly` uses the cycle average (used ÷ elapsed cycle time), so quiet nights count.
- `r_short`: least-squares slope over the last 30 minutes (`session`) or 6 hours (`weekly`).
- `projected_at_reset = used + max(r_long, 0) × hours_to_reset`, clamped to [used, 200]; `projected_short` is the same with `r_short`. `accel = r_short − r_long`. `confidence` is the number of points.
- `status: "unknown"` (with a `reason`) when there is no reset time, fewer than 4 points, a span under 40 minutes, or `used` under 20%.

Scoped (model) windows get `forecast: null`. The forecast doesn't change tiers or mode.

## Throttle policy

A separate, host-local decision between two levels, `fast` and `constrained`, over the protected provider's (`anthropic`) forecast windows. It doesn't change tiers or mode. Config lives under `plugins.entries.quota-router.throttle`; thresholds default as below, and the level specs (`levels.fast`, `levels.constrained`: `provider`, `model`, `fallback: [{provider, model}]`) have **no code defaults**. The constrained chain (primary and fallback) must not contain `anthropic`, so a saturated constrained primary fails over away from the capped provider rather than back to it.

- **Enter `constrained`** on any window: (a) `projected_at_reset ≥ enter_projected` (95); (b) `used ≥ enter_used` (85); (c) `r_long > 0`, `r_short ≥ 1.5 × r_long` and `projected_at_reset ≥ early_projected` (85). A window resetting within `ignore_reset_within_s` (30 min) can't escalate.
- **Leave** when every window has `projected_short ≤ leave_projected` (80; an unknown forecast counts as satisfied) and `used < leave_used` (75), after `min_dwell_s` (30 min).
- **Stale data** (snapshot older than `stale_after_s`): hold the level. Older than `stale_hold_s` (60 min): `constrained` if any last-known window was at or above `leave_used` (one `stale usage data` alert per incident), otherwise `fast`.
- **Precedence:** `hold` > `disable` > observed Anthropic block > policy (including stale rules).
- **Alerts:** one `quota-router: throttle [(shadow)] <from> → <to> (<why>)` line per transition. Every decision is appended to `throttle.log` in state (per-window used, projected, `r_long`, `r_short`, rule) for the shadow report.

## Throttle apply

```yaml
plugins:
  entries:
    quota-router:
      throttle:
        enabled: false            # shadow mode (default): decide + log, never write
        profiles: [worker-a, worker-b]
        levels:
          fast:        {provider: primary-provider, model: fast-model,
                        fallback: [{provider: "custom:internal", model: internal-model}, {provider: backup, model: backup-model}],
                        max_in_progress: 64}          # optional; both levels or neither
          constrained: {provider: "custom:internal", model: internal-model,
                        fallback: [{provider: backup, model: backup-model}],
                        max_in_progress: 24}
```

- `hermes quota-router throttle apply` decides the level (above) and, when `enabled: true`, reconciles **every run** (not only on a level change) each listed profile by running `hermes -p <profile> quota-router throttle apply-one <level> ...`. The child reads its own raw `config.yaml`, compares the **parsed** `model.provider`, `model.default` and `fallback_providers` with both level specs, and makes at most one `hermes_cli.config.atomic_config_write` call:
  - matches the desired level → no-op;
  - matches the other level → backup to `<root>/backups/quota-throttle/<profile>-<ts>.yaml`, one atomic write, re-read to verify. If the throttle had already applied this level there, it alerts once: `re-applied <level> to <profile> after external overwrite`;
  - matches neither → a hand edit: skipped, never overwritten, one alert per (profile, level);
  - a `model:` block carrying its own route (`base_url`, `api_mode`, ...) is skipped with an alert.
- When both levels set `max_in_progress`, the root config's `kanban.max_in_progress` is reconciled the same way (`hermes -p default`). Otherwise the key is never touched.
- A constrained chain containing `anthropic` is refused with one alert, and nothing is written.
- Running workers are never touched; the next spawn reads the new config.
- `throttle status [--json]`: level, source/rule, hold/disable, per-window numbers from the last decision, per-profile results, and the leak count (sessions started on `anthropic` by a throttled profile since entering `constrained`, read-only from its `state.db`).
- `throttle hold <fast|constrained> --for 2h` / `throttle hold --clear`; `throttle disable [--level fast]` / `throttle disable --clear`. Each applies at once.
- Rollback: `throttle disable`, check `throttle status` shows every profile at `fast`, then downgrade the plugin. Restoring a backup is the last resort.

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

## Desktop widget

The desktop half of this package (`desktop/plugin.js`) ships alongside the
Python plugin but stays **off by default** — enable it in **Capabilities → Plugins** by turning on the **Desktop** switch on the
`quota-router` row (the **Agent** switch is the separate Python half). It loads
immediately; run **⌘K → Reload desktop plugins** only if it doesn't appear.

Hovering the chip shows a stacked card per provider: each window's remaining %
(green > 30%, amber 11–30%, red ≤ 10%) and time to reset, model limits under
their own heading, blocks and fetch errors, and when the snapshot was fetched.

Once enabled it adds:

- **Status-bar chip** — lowest remaining % for each configured provider
  (`A 55% left · C 0% left`), plus the lowest remaining scoped/model window
  if one exists (`· Fable 100%`). The dot tone follows the router's mode
  (normal/shift/internal). Hover for a full breakdown of every window, any
  active block, and fetch errors; click to open the Quota pane/route.
- **Quota pane** (right dock) and **`/quota` route** — one card per
  provider: tier badge, a bar per usage window with remaining % and reset
  countdown, a "Model limits" section for scoped windows, a block banner
  and fetch-error line when present, and a footer showing host, fetch
  time, snapshot age, and the 120s poll cadence, with a manual Refresh
  button.

Requires the backend `hermes quota-router status --json` (≥0.2.0) on the
same host; an outdated or missing backend surfaces as an in-widget error
with a `hermes plugins enable quota-router` hint rather than failing
silently.

## Rollback

`hermes plugins disable quota-router` (root and each profile), `hermes cron remove <id>`, then delete `cache/quota-router/`.
