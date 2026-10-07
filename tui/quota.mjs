// quota.mjs — live provider-quota widget for the Hermes TUI.
// Claude-style usage bars:  Session ████░░░  82% (resets in 1h 11m)
// Data source: the quota-router plugin's state file (the plugin's cron keeps
// it fresh; the widget never hits the network). Re-reads every 60s.
//
// /quota        — toggle the docked widget (a second /quota dismisses it)
// /quota <p>    — show only provider <p> (e.g. /quota openai-codex)
// Auto-docks on TUI startup: register() ends with openWidget, the documented
// always-on contract for ambient widgets (re-docks on /widgets-reload).
import { readFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { basename, dirname, normalize } from 'node:path'

export default function register(sdk) {
  const { Box, Dialog, React, Text, defineWidgetApp, h, gauge, openWidget } = sdk

  const HERMES_HOME = process.env.HERMES_HOME?.trim() || `${homedir()}/.hermes`
  // A profile session gets HERMES_HOME=<root>/profiles/<name>, but the shared
  // quota cache deliberately lives at the root home (core.state_path → core.
  // root_home). Normalize a profile home back to its root so profile sessions
  // read the same cache as the root session.
  const homePath = normalize(HERMES_HOME)
  const ROOT_HOME = basename(dirname(homePath)) === 'profiles'
    ? dirname(dirname(homePath))
    : homePath
  const STATE = `${ROOT_HOME}/cache/quota-router/state.json`

  // ESM imports are ready before registration, so the first render has cached data.
  function loadState() {
    try {
      return JSON.parse(readFileSync(STATE, 'utf8'))
    } catch {
      return null
    }
  }

  function fmtReset(resets_at, now) {
    if (!resets_at) return 'resets ?'
    const s = Math.max(0, resets_at - (now ?? Date.now() / 1000))
    if (s < 90) return 'resets now'
    const m = Math.round(s / 60)
    if (m < 90) return `resets in ${m}m`
    const h = Math.floor(m / 60)
    const rem = m % 60
    if (h < 36) return `resets in ${h}h ${rem}m`.replace(' 0m', '')
    return `resets in ${Math.floor(h / 24)}d ${h % 24}h`
  }

  function fmtAge(fetchedAt, now) {
    const s = Math.max(0, (now ?? Date.now() / 1000) - fetchedAt)
    if (s < 60) return `${Math.round(s)}s old`
    const m = Math.floor(s / 60)
    if (m < 120) return `${m}m ${Math.floor(s % 60 / 10)}0s old`
    return `${Math.floor(m / 60)}h ${m % 60}m old`
  }

  function windowSeg(w, now) {
    const pct = Math.round(w.used)
    const bar = gauge(Math.min(1, w.used / 100), 10)
    return `${w.label.replace(/^Current /, '')} ${bar} ${pct}% (${fmtReset(w.resets_at, now)})`
  }

  function QuotaRows({ state, only, t }) {
    const providers = Object.entries(state.providers || {})
      .filter(([p]) => !only || p.includes(only))
    return h(Box, { flexDirection: 'column' },
      ...providers.map(([p, ps]) => {
        // Host windows use "account"/"model"; legacy cache windows have no scope.
        const wins = ((ps.snapshot || {}).windows || [])
          .filter(win => win.scope == null || win.scope === '' || win.scope === 'account')
        const now = state.now ?? (Date.now() / 1000)
        const tierTag = ps.tier === 'ok' ? '' : ` ${ps.tier}`
        const segs = wins.map(w => windowSeg(w, now)).join(' | ')
        // whole line takes the worst-window color so the bar itself shifts red when hot
        const peak = Math.max(0, ...wins.map(w => w.used || 0))
        const fetchedAt = (ps.snapshot || {}).fetched_at || 0
        const stale = !fetchedAt || (now - fetchedAt > 15 * 60)
        const color = stale || peak >= 95 ? t.color.error : peak >= 80 ? (t.color.warning ?? t.color.error) : t.color.text
        const age = fetchedAt ? ` [${fmtAge(fetchedAt, now)}]` : ' [no data]'
        return h(Text, { key: p, color }, `${p}${tierTag}${stale ? ' STALE' : ''} ${segs}${age}`)
      }))
  }

  function Ticker({ only, t }) {
    const [st, setSt] = React.useState(() => loadState())
    React.useEffect(() => {
      const id = setInterval(() => setSt(loadState()), 60_000)
      return () => clearInterval(id)
    }, [])
    if (!st) return h(Text, { color: t.color.muted }, 'quota-router state unavailable')
    return h(QuotaRows, { state: st, only, t })
  }

  const app = defineWidgetApp({
    id: 'quota',
    help: 'live provider quota bars (arg: provider filter)',
    mode: 'ambient',
    zone: 'dock-bottom', // full-width strip directly above the bottom status bar
    usage: 'usage: /quota [provider]   e.g. /quota openai-codex',

    init(arg) {
      return { only: arg.trim().toLowerCase() }
    },

    reduce(state, { ch, key }) {
      return key.escape || ch === 'q' ? null : state
    },

    render({ state, t }) {
      return h(Ticker, { only: state.only, t })
    },
  })

  // Auto-dock at startup. Safe on hot-reload too: openWidget replaces this
  // appId's docked instance in place (refreshing state), it never duplicates.
  openWidget(app, app.init(''))
}
