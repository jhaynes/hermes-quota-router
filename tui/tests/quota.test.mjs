// Exercise the real widget register/render path against an owned on-disk state file.
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { test } from 'node:test'
import register from '../quota.mjs'

const theme = { color: { text: 'text', muted: 'muted', error: 'error', warning: 'warning' } }

function setup(snapshot, arg = '') {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'quota-widget-'))
  const oldHome = process.env.HERMES_HOME
  process.env.HERMES_HOME = home
  if (snapshot) {
    const dir = path.join(home, 'cache/quota-router')
    fs.mkdirSync(dir, { recursive: true })
    fs.writeFileSync(path.join(dir, 'state.json'), JSON.stringify(snapshot))
  }
  let app
  const sdk = {
    Box: 'Box', Text: 'Text', Dialog: 'Dialog',
    React: { useState: init => [typeof init === 'function' ? init() : init, () => {}], useEffect: () => {} },
    h: (type, props, ...children) => typeof type === 'function'
      ? type({ ...props, children }) : { type, props, children },
    gauge: (ratio, width) => '█'.repeat(Math.round(ratio * width)).padEnd(width, '░'),
    defineWidgetApp: definition => (app = definition),
    openWidget: () => {},
  }
  register(sdk)
  return {
    render: () => app.render({ state: app.init(arg), t: theme }),
    cleanup: () => {
      if (oldHome == null) delete process.env.HERMES_HOME
      else process.env.HERMES_HOME = oldHome
      fs.rmSync(home, { recursive: true, force: true })
    },
  }
}

function text(node) {
  if (typeof node === 'string') return node
  return (node?.children ?? []).map(text).join('\n')
}

function state(scope = 'account') {
  const now = Date.now() / 1000
  return { providers: {
    anthropic: { tier: 'exhausted', snapshot: { fetched_at: now, windows: [
      { label: 'Current week', used: 100, resets_at: now + 7200, scope },
      { label: 'Fable week (model)', used: 99, resets_at: now + 3600, scope: 'Fable' },
    ] } },
    'openai-codex': { tier: 'ok', snapshot: { fetched_at: now, windows: [
      { label: 'Weekly', used: 27, resets_at: now + 86400, scope },
    ] } },
  } }
}

test('account usage bars and reset countdowns survive explicit account scope', async () => {
  // Regression: !win.scope discarded every real account window, leaving only provider names.
  const fixture = setup(state())
  try {
    await new Promise(resolve => setImmediate(resolve)) // Let legacy async fs import finish.
    const rendered = text(fixture.render())
    assert.match(rendered, /anthropic exhausted week █{10} 100% \(resets in 2h\)/)
    assert.match(rendered, /openai-codex Weekly [█░]+ 27% \(resets in 24h\)/)
    assert.doesNotMatch(rendered, /Fable/)
  } finally { fixture.cleanup() }
})

test('legacy scopes and provider filtering remain compatible', async () => {
  // Old cached null/empty scopes should display without exposing other providers or model caps.
  for (const scope of [null, '']) {
    const fixture = setup(state(scope), 'openai-codex')
    try {
      await new Promise(resolve => setImmediate(resolve))
      const rendered = text(fixture.render())
      assert.match(rendered, /Weekly [█░]+ 27%/)
      assert.doesNotMatch(rendered, /anthropic|Fable/)
    } finally { fixture.cleanup() }
  }
})

test('first render reads existing cached state without a 60-second blank period', () => {
  // Async fs initialization previously left the real React useState initializer null until polling.
  const fixture = setup(state(null))
  try { assert.match(text(fixture.render()), /Weekly [█░]+ 27%/) }
  finally { fixture.cleanup() }
})

test('missing cache reports unavailable instead of inventing usage', () => {
  // Empty-state compatibility matters while startup refresh has not created the shared cache.
  const fixture = setup(null)
  try { assert.equal(text(fixture.render()), 'quota-router state unavailable') }
  finally { fixture.cleanup() }
})

test('register works when HERMES_HOME is absent', () => {
  // The installed fallback referenced os without importing it and threw before the widget registered.
  const oldHome = process.env.HERMES_HOME
  delete process.env.HERMES_HOME
  try { assert.doesNotThrow(() => register({ defineWidgetApp: x => x, openWidget: () => {} })) }
  finally {
    if (oldHome != null) process.env.HERMES_HOME = oldHome
  }
})
