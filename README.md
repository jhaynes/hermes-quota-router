# quota-router

Host-level quota awareness for `anthropic` and `openai-codex`.

- **Observed blocks:** `api_request_error` records a hard quota error, and the next successful request from that provider clears it (`post_api_request`).
- **Usage tiers:** `/usage` snapshots are sorted into ok, tight or exhausted, with hysteresis. Thresholds are 85/75 for tight and 97/90 for exhausted, with a 15-minute dwell. A window that resets within 30 minutes can't escalate a tier.
- **Mode:** normal, shift (one provider constrained) or internal (both constrained).
- **Alerts:** at 75% and 90%, once per window per reset cycle.
- **Alert queue:** only `hermes quota-router refresh` (the cron path) **drains** and delivers pending alert/transition lines. A manual refresh (`/quota refresh`, `hermes quota-router status --refresh`) folds fresh usage in and enqueues any new lines, but leaves the queue for the next cron run to deliver — it no longer swallows them. Delivery is **at-most-once**: drained lines are removed from state immediately, so if the cron script's delivery step then fails, those lines are lost, not retried.
- **Surfaces:** `hermes quota-router status [--refresh] | refresh | path | throttle <apply|status|hold|disable>`, `/quota [refresh]`.
- **Cache efficiency:** 24h/7d prompt-cache hit rates per provider and source, the top-burner sessions, and low-hit alerts, all from this host's `state.db` (see below).

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

## Cache efficiency

`refresh()` (the 15-minute cron and any manual refresh, never a request hook) also reads this host's `<root HERMES_HOME>/state.db` and summarises how well prompt caching is working. The query is **read-only**: the DB is opened with a `file:…?mode=ro` URI and a 0.5 s busy timeout, and a missing file is never created.

- **Metric:** `hit = cache_read / (input + cache_read + cache_write)`, over trailing **24h** and **7d** windows. `input` is uncached input.
- **Breakdowns:** per provider (`anthropic`, `openai-codex`, and `other` for every other `billing_provider`), then per `source` (desktop, kanban, cron, tui, …) within each provider. It also lists the **top 5 burners**: sessions ranked by uncached input plus cache writes over 24h, with id, title, source, model, hit % and tokens.
- **Attribution caveat:** session token totals in `state.db` are cumulative, and windows filter on `last_activity_at` (falling back to `started_at`). A long session's **whole** total is attributed to the window it was last active in. A week-old session that was active an hour ago counts in full towards 24h.
- **Host-local:** each host reports only its own root `state.db`. The payload's `host` says which host. Profile-local `state.db` files (`profiles/<name>/state.db`) are not read.
- **State:** only the latest aggregates and top-5 list are kept (`state.json` → `cache`), with no per-run history.
- **Failures:** a missing, locked or unreadable DB records a `cache` fetch outcome (`missing` / `locked` / `db_error`) with the incident's first-failure time. The last good aggregates stay on display, and the failure is shown under the `cache:` block. It is display-only and never alerts, and it never changes tier, mode or blocks.

**Alerts** go through the normal queue, so like every other alert they are delivered only on the cron path. Each condition fires at most once per 24h:

| key | default | fires when |
|---|---|---|
| `cache_alert_below` | `80` | a provider's 24h hit % is below this … |
| `cache_alert_min_tokens` | `20000000` | … **and** its 24h input (cached + uncached + writes) is at least this, so low-volume noise stays quiet |
| `cache_write_alert_tokens` | `10000000` | a single session wrote at least this many tokens to cache in 24h … |
| `cache_write_alert_below` | `50` | … at a hit % below this |

Set them under `plugins.entries.quota-router` in `config.yaml`, like the other thresholds:

```yaml
plugins:
  entries:
    quota-router:
      cache_alert_below: 85
      cache_alert_min_tokens: 50000000
```

**Surfaces:** `hermes quota-router status` and `/quota` add a `cache:` block showing 24h/7d hit % per provider and source, plus the top burners. `status --json` adds a `cache` object (see below). The desktop widget shows 24h hit % per provider in the chip's hover card, and 24h/7d hit % plus the top-burner list in the Quota pane.

## `status --json`

`hermes quota-router status --json [--refresh]` prints exactly one `QRJSON <payload>` line to stdout (nothing else on stdout; logging goes to stderr) for the desktop widget or any other machine reader. `--refresh --json` fetches fresh data but does **not** drain the pending-alert queue — only `hermes quota-router refresh` (the cron path) does that.

The payload (`v: 1`) has `host`, `now`, `mode`/`mode_since`, and per-provider `tier`, `fetched_at`, `stale`, `plan`, `windows` (each with `affects_tier: bool` — `false` for scoped windows — and `forecast`), `block`, `fetch.core`/`fetch.scoped` outcomes, and `details`; plus top-level `recent_errors` (last 5) and `pending_count`.

## Forecast

For each window in `forecast_windows` (default: Anthropic `Current session` as `session`, `Current week` and `Opus week` as `weekly`; since 0.4.0 also OpenAI Codex `Session`, its 5 h primary window, as `session` and `Weekly` as `weekly`), `status` and `status --json` show a forecast computed from the current reset cycle's history:

- `rate_pct_h` (`r_long`): `session` uses the least-squares slope over the whole current cycle; `weekly` uses the cycle average (used ÷ elapsed cycle time), so quiet nights count.
- `r_short`: least-squares slope over the last 30 minutes (`session`) or 6 hours (`weekly`).
- `projected_at_reset = used + max(r_long, 0) × hours_to_reset`, clamped to [used, 200]; `projected_short` is the same with `r_short`. `accel = r_short − r_long`. `confidence` is the number of points.
- `status: "unknown"` (with a `reason`) when there is no reset time, fewer than 4 points, a span under 40 minutes, or `used` under 20%.

Scoped (model) windows get `forecast: null`. The forecast doesn't change tiers or mode.

Since 0.3.0 it also carries `cache`: `collected_at`, `stale` (older than `stale_after_s`, or never collected), `alert_below`, `providers.<anthropic|openai-codex|other>.<24h|7d>` (`sessions`, `input`, `cache_read`, `cache_write`, `output`, `hit` — `null` with no input — and `sources.<source>` with the same fields minus `sources`), `top` (up to 5 × `id`, `title`, `source`, `model`, `provider`, `input`, `cache_read`, `cache_write`, `output`, `burn`, `hit`), and `fetch` (`last_ok_at`, `error`). The field is additive, so `v` stays `1`.

## Throttle policy

A separate, host-local decision over an ordered **ladder** of levels. Every provider that gates a level (a **gate provider**, `anthropic` or `openai-codex`) has its own `ok`/`constrained` state, decided from its own `forecast_windows` by the rules below, with its own dwell timer and reasons. The **selected level is the first whose gate providers are all `ok`**. The last level has no gate, so there is always a floor. The throttle doesn't change tiers or mode. Config lives under `plugins.entries.quota-router.throttle`. The thresholds default as below and apply to every gate provider. The level specs (`provider`, `model`, `fallback: [{provider, model}]`, optional `max_in_progress`) have **no code defaults**.

Per gate provider, over that provider's forecast windows:

- **Enter `constrained`** on any window: (a) `projected_at_reset ≥ enter_projected` (95); (b) `used ≥ enter_used` (85); (c) `r_long > 0`, `r_short ≥ 1.5 × r_long` and `projected_at_reset ≥ early_projected` (85). A window resetting within `ignore_reset_within_s` (30 min) can't escalate.
- **Leave** (back to `ok`) when every window has `projected_short ≤ leave_projected` (80; an unknown forecast counts as satisfied) and `used < leave_used` (75), after `min_dwell_s` (30 min) in `constrained`.
- **Stale data** (snapshot older than `stale_after_s`): hold the state. Older than `stale_hold_s` (60 min): `constrained` if any last-known window was at or above `leave_used` (one `stale usage data` alert per incident), otherwise `ok`.
- An observed hard block on that provider makes it `constrained` until the block ends.

**Precedence:** `hold` > `disable` > observed block > policy (including the stale rules). A hold or disable forces a level. Its gate providers become `ok`, and each earlier level is made unselectable by marking its other gate providers `constrained`. The gate providers of the forced level and of every level before it are frozen until the hold or disable ends; the other gate providers keep following the policy. When it ends, each frozen provider resumes from the forced state, with the dwell counted from when it was forced. A hold or disable naming a level the ladder doesn't have is ignored, with one alert.

**Validation** (B1, generalised): a provider that gates **any** level may appear in a level's chain (primary or fallback) **only if it also gates that level**. Otherwise a saturated level could fail over onto a provider that is known to be capped. Also: at least two levels, unique `name`s, the last level has no `gate` and no other level is ungated (it would make every later level unreachable), gate providers are tracked (`anthropic`, `openai-codex`) and have `forecast_windows`, and a level has only the keys `name`, `gate`, `provider`, `model`, `fallback` and `max_in_progress`. An invalid config is refused with one alert, and nothing is written.

**Legacy `levels`:** `levels: {fast, constrained}` with `protected_provider` (default `anthropic`) still works. It is the two-level ladder `[fast gated by protected_provider, constrained]`, and it decides, logs and alerts exactly as 0.3.0 did. Its validation is the 0.3.0 check (the constrained chain must not contain the protected provider), which is the rule above for that ladder. The recorded level stays the source of truth for the protected provider. A 0.3.0 state file migrates on the first decision, keeping its dwell, the hold, the disable and the stale-alert dedupe. Editing `protected_provider` on a live config hands the level and its dwell to the new provider, as in 0.3.0. Set `ladder` or `levels`, not both.

- **Alerts:** with one gate provider (the legacy config), one `quota-router: throttle [(shadow)] <from> → <to> (<why>)` line per level change, as before. With several gate providers, one `quota-router: throttle [(shadow)] <provider> ok → constrained (<why>)` line per provider state change, and one `quota-router: throttle [(shadow)] <from> → <to> (<provider> <state>, ...)` line per level change. A stale-data escalation alert (`stale usage data [for <provider>] ...`) replaces that provider's own line. Every decision is appended to `throttle.log` in state (per-window used, projected, `r_long`, `r_short`, rule; per provider when there are several) for the shadow report.

## Throttle apply

A codex → claude → internal ladder: codex while Codex has room, Claude while Codex is nearly out and Claude isn't, internal when both are:

```yaml
plugins:
  entries:
    quota-router:
      throttle:
        enabled: false            # shadow mode (default): decide + log, never write
        profiles: [worker-a, worker-b]
        ladder:                   # highest preference first
          - name: codex
            gate: [openai-codex]  # selectable while openai-codex is ok
            provider: openai-codex
            model: codex-model
            fallback: [{provider: "custom:internal", model: internal-model}]
          - name: claude
            gate: [anthropic]     # selectable while anthropic is ok
            provider: anthropic
            model: claude-model
            fallback: [{provider: "custom:internal", model: internal-model}]
          - name: internal        # no gate: the floor
            provider: "custom:internal"
            model: internal-model
            fallback: [{provider: backup, model: backup-model}]
```

The validation rule means the `codex` level can't fall back to `anthropic`: `anthropic` gates `claude`, and it may be capped while `codex` is selected. To let the codex level fall back to Claude, gate it on both (`gate: [openai-codex, anthropic]`). It is then selected only while both have room.

The legacy two-level form, unchanged:

```yaml
      throttle:
        enabled: false
        profiles: [worker-a, worker-b]
        levels:
          fast:        {provider: primary-provider, model: fast-model,
                        fallback: [{provider: "custom:internal", model: internal-model}, {provider: backup, model: backup-model}],
                        max_in_progress: 64}          # optional; both levels or neither
          constrained: {provider: "custom:internal", model: internal-model,
                        fallback: [{provider: backup, model: backup-model}],
                        max_in_progress: 24}
```

- `hermes quota-router throttle apply` decides the level (above) and, when `enabled: true`, reconciles **every run** (not only on a level change) each listed profile by running `hermes -p <profile> quota-router throttle apply-one <level> ...`. The child reads its own raw `config.yaml`, compares the **parsed** `model.provider`, `model.default` and `fallback_providers` with every level spec, and makes at most one `hermes_cli.config.atomic_config_write` call:
  - matches the desired level → no-op;
  - matches another level → backup to `<root>/backups/quota-throttle/<profile>-<ts>.yaml`, one atomic write, re-read to verify. If the throttle had already applied this level there, it alerts once: `re-applied <level> to <profile> after external overwrite`;
  - matches no level → a hand edit: skipped, never overwritten, one alert per (profile, level);
  - a `model:` block carrying its own route (`base_url`, `api_mode`, ...) is skipped with an alert.
- When every level sets `max_in_progress`, the root config's `kanban.max_in_progress` is reconciled the same way (`hermes -p default`). Otherwise the key is never touched.
- A config failing validation (above) is refused with one alert, and nothing is written. The child validates the specs it is given too.
- Running workers are never touched; the next spawn reads the new config.
- `throttle status [--json]`: the selected level, source/rule, hold/disable, per-window numbers from the last decision, per-profile results, and the leak count. With several gate providers it also shows the ladder and each provider's state, reasons and windows. The leak count is per gate provider: sessions a throttled profile started on that provider since it became `constrained`, read-only from the profile's `state.db`. No selectable level routes to a constrained gate provider, so each of those sessions leaked. `--json` adds `ladder`, `providers` and per-profile `leaked_by_provider`. Fixed in 0.4.0: the text line used to say `leak N anthropic sessions` whatever `protected_provider` was. It now names the provider it counted. With the default `protected_provider: anthropic` the line is unchanged.
- `throttle hold <level> --for 2h` / `throttle hold --clear`; `throttle disable [--level <level>]` (default: the first level, `fast` for `levels:`) / `throttle disable --clear`. Level names are the ladder's. Each applies at once.
- Rollback: `throttle disable`, check `throttle status` shows every profile at the first level, then downgrade the plugin. Restoring a backup is the last resort.
- Downgrading to 0.3.0 with a `levels:` config: 0.4.0 keeps every throttle field 0.3.0 reads exactly as 0.3.0 writes it. Those are `level`, `since`, `source`, `rule`, `decided_at`, `hold`, `disabled`, the stale-alert dedupe (`stale_alerted_for`), `log`, `applied` and `config_error`. So 0.3.0 carries on with the same level, dwell, hold and disable, and doesn't send a stale alert again. It ignores `throttle.providers`. When 0.4.0 runs again, it re-derives the protected provider's state from what 0.3.0 recorded.
- Downgrading with a `ladder:` config preserves nothing useful. 0.3.0 doesn't know the `ladder` key, so it sees no `levels`, refuses to apply (`throttle.levels.fast needs provider and model`) and decides for `anthropic` only. A stored ladder level reads as `fast`, and a hold or disable naming a ladder level is taken literally. Switch the config back to `levels:` and clear any hold or disable first.

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

### Cron script and cadence

`scripts/quota-router-refresh.py` runs `hermes quota-router refresh`, then `hermes quota-router throttle apply` (set `QR_HERMES_BIN` to use another binary). `throttle apply` runs even if `refresh` failed, timed out (`QR_STEP_TIMEOUT`, default 300 s per step) or could not start, so holds expire and the stale-data rules run. On a host without a `throttle` block it's a silent no-op; with `enabled: false` (shadow) it decides and logs only. Only `⚠️`/`quota-router:` lines reach stdout.

The refresh may run every 5 minutes (`hermes cron edit <id> --schedule "every 5m"`). Run shadow mode at 15 minutes first, then 5 minutes. A built-in backoff keeps a failing endpoint from being hammered: after 3 non-ok core fetches for a provider within an hour (from the `fetch_log` ring) with no success since, that provider is fetched at most every 15 minutes, with one `quota-router: <provider> usage fetch backing off to 15m (...)` alert per incident. The next success clears it.

## Evaluation (read-only)

- `python eval/outcomes.py --state-db <profile>/state.db --kanban-db <board>/kanban.db [...] [--days 7] [--json]` groups a profile's top-level kanban worker sessions by `provider/model`: sessions, cards, card outcomes (task status), review rejects (`changes_requested` events) and turns. Both databases are opened read-only. Model is confounded with task, so it's a first signal, not a benchmark.
- `python eval/backtest.py --state <root>/cache/quota-router/state.json [--throttle-json '{...}'] [--json]` replays recorded history through the throttle policy for the current thresholds and a ±5-point grid (`enter_projected` 85–100, `leave_projected` 70–90, leave < enter). It reports constrained hours, misses (a window reached 97% or a block happened while the throttle was `fast` in the hour before) and false alarms (constrained periods whose window's cycle ended below 85%). The proposal minimises misses, then constrained time. Tightening is proposed automatically; any loosening is labelled Justin's judgement and needs 3 weeks of history. It skips until 7 days of history exist (a full retention window, within an hour), and it says that it's biased by constrained periods and by other hosts' usage. Nothing is applied. With a `ladder` in `--throttle-json`, each gate provider is back-tested on its own (its state follows exactly the policy of a legacy throttle protecting it, so `fast` reads as `ok`); `--json` then maps provider → report. Hold and disable are not replayed.

## Tests

```
~/.hermes/hermes-agent/venv/bin/python -m pytest -q -p no:cacheprovider tests/   # policy
~/.hermes/hermes-agent/venv/bin/python tests/e2e_live.py                          # real hooks + real anthropic call; restores state
```

If the managed venv has no pytest, run it ephemerally with uv. The throttle-apply tests import `hermes_cli` (for the real `atomic_config_write`), so put the agent checkout on `PYTHONPATH`:

```
PYTHONPATH=~/.hermes/hermes-agent uv run --no-project --python ~/.hermes/hermes-agent/venv/bin/python \
  --with pytest pytest -q -p no:cacheprovider tests/
```

Every test uses a temp `HERMES_HOME`. The B3 round-trip test edits a **copy** of a real profile config (`QR_REAL_PROFILE_CONFIG`, default `~/.hermes/profiles/builder/config.yaml`; skipped if absent). The live test runs the real `hermes -p <profile>` CLI (`QR_HERMES_BIN`) against the temp home; `QR_SKIP_LIVE=1` skips it.

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
