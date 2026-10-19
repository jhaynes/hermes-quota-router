// Pure-logic tests for desktop/plugin.js. The full file imports the desktop
// plugin SDK (@hermes/plugin-sdk) which does not resolve under plain Node, so
// this test extracts ONLY the code between the `// BEGIN PURE` / `// END PURE`
// markers and evaluates it in a sandbox, then exercises behaviour against
// fixtures. It never asserts on source text — only on runtime behaviour.

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import test from "node:test";
import assert from "node:assert/strict";
import vm from "node:vm";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const pluginSrc = readFileSync(path.join(__dirname, "..", "plugin.js"), "utf8");

function extractPureBlock(src) {
	const beginMarker = "// BEGIN PURE";
	const endMarker = "// END PURE";
	const start = src.indexOf(beginMarker);
	const end = src.indexOf(endMarker);
	assert.ok(start !== -1, "BEGIN PURE marker not found");
	assert.ok(end !== -1 && end > start, "END PURE marker not found");
	return src.slice(start + beginMarker.length, end);
}

function loadPure() {
	const body = extractPureBlock(pluginSrc);
	const exportsList = ["shortName", "remainingPct", "fmtReset", "parseQrjson", "chipSummary", "toneForMode", "errorChipLabel", "retryDelayMs", "shouldRetry", "loadingChipLabel"];
	const wrapped = `${body}\n({ ${exportsList.join(", ")} })`;
	const context = { console };
	vm.createContext(context);
	return vm.runInContext(wrapped, context, { filename: "plugin-pure-block.js" });
}

const pure = loadPure();

const fixturesDir = path.join(__dirname, "fixtures");
const statusV1 = JSON.parse(readFileSync(path.join(fixturesDir, "status_v1.json"), "utf8"));
const statusV2 = JSON.parse(readFileSync(path.join(fixturesDir, "status_v2_mismatch.json"), "utf8"));

test("shortName maps known providers and falls back", () => {
	assert.equal(pure.shortName("anthropic"), "A");
	assert.equal(pure.shortName("openai-codex"), "C");
	assert.equal(pure.shortName("mystery"), "M");
	assert.equal(pure.shortName(""), "?");
});

test("remainingPct converts used% to remaining%, clamped", () => {
	assert.equal(pure.remainingPct({ used: 45 }), 55);
	assert.equal(pure.remainingPct({ used: 0 }), 100);
	assert.equal(pure.remainingPct({ used: 100 }), 0);
	assert.equal(pure.remainingPct({ used: -10 }), 100);
	assert.equal(pure.remainingPct({ used: 150 }), 0);
	assert.equal(pure.remainingPct({ used: null }), null);
	assert.equal(pure.remainingPct({}), null);
	assert.equal(pure.remainingPct(null), null);
});

test("fmtReset produces human deltas and handles null/past", () => {
	const now = 1758830000 * 1000;
	assert.equal(pure.fmtReset(null, now), "");
	assert.equal(pure.fmtReset(1758830000 - 10, now), "resetting…");
	// +1h exactly
	assert.equal(pure.fmtReset(1758830000 + 3600, now), "1h 0m");
	// +25h -> 1d 1h
	assert.equal(pure.fmtReset(1758830000 + 25 * 3600, now), "1d 1h");
	// +30s -> <1m
	assert.equal(pure.fmtReset(1758830000 + 30, now), "<1m");
});

test("parseQrjson finds the LAST QRJSON line amid noise", () => {
	const noisy = [
		"some diagnostic line",
		"QRJSON " + JSON.stringify({ v: 1, marker: "first" }),
		"warning: something happened",
		"QRJSON " + JSON.stringify({ v: 1, marker: "second" }),
	].join("\n");
	const parsed = pure.parseQrjson(noisy);
	assert.equal(parsed.marker, "second");
});

test("parseQrjson throws when no QRJSON line present", () => {
	assert.throws(() => pure.parseQrjson("just some noise\nno json here"));
});

test("parseQrjson throws on invalid JSON after the marker", () => {
	assert.throws(() => pure.parseQrjson("QRJSON {not valid json"));
});

test("chipSummary builds provider + scoped summary from v1 fixture", () => {
	const summary = pure.chipSummary(statusV1);
	// anthropic lowest-remaining affects_tier window is 5h session at 45% used -> 55% left
	assert.match(summary, /A 55% left/);
	// codex weekly at 100% used -> 0% left
	assert.match(summary, /C 0% left/);
	// scoped Fable window at 0% used -> 100% remaining
	assert.match(summary, /Fable 100% left/);
	assert.doesNotMatch(summary, /week \(model\)/);
});

test("chipSummary handles empty/missing providers", () => {
	assert.equal(pure.chipSummary({}), "");
	assert.equal(pure.chipSummary(null), "");
	assert.equal(pure.chipSummary({ providers: {} }), "");
});

test("toneForMode maps mode to StatusDot tone", () => {
	assert.equal(pure.toneForMode("normal"), "good");
	assert.equal(pure.toneForMode("shift"), "warn");
	assert.equal(pure.toneForMode("internal"), "bad");
	assert.equal(pure.toneForMode("unknown"), "good");
});

test("v2 fixture payload is distinguishable by v field (version mismatch path)", () => {
	assert.equal(statusV2.v, 2);
	assert.notEqual(statusV2.v, 1);
});

// ---- tip model tests (stacked, coloured hover) -------------------------------

const tipPure = (() => {
	const body = extractPureBlock(pluginSrc);
	const context = { console };
	vm.createContext(context);
	return vm.runInContext(`${body}\n({ tipModel, leftTone, fmtAgo })`, context, { filename: "plugin-pure-tip.js" });
})();

test("leftTone buckets remaining % into good / warn / bad / muted", () => {
	assert.equal(tipPure.leftTone(null), "muted");
	assert.equal(tipPure.leftTone(0), "bad");
	assert.equal(tipPure.leftTone(10), "bad");
	assert.equal(tipPure.leftTone(11), "warn");
	assert.equal(tipPure.leftTone(30), "warn");
	assert.equal(tipPure.leftTone(31), "good");
	assert.equal(tipPure.leftTone(100), "good");
});

test("fmtAgo renders compact relative ages", () => {
	assert.equal(tipPure.fmtAgo(5), "just now");
	assert.equal(tipPure.fmtAgo(90), "1m ago");
	assert.equal(tipPure.fmtAgo(3 * 3600 + 120), "3h ago");
	assert.equal(tipPure.fmtAgo(null), "");
});

test("tipModel stacks providers with core rows before model-limit rows, each toned", () => {
	const nowMs = statusV1.now * 1000;
	const m = tipPure.tipModel(statusV1, nowMs);
	assert.equal(m.mode, statusV1.mode);
	assert.ok(["good", "warn", "bad"].includes(m.modeTone));
	assert.equal(m.providers.length, Object.keys(statusV1.providers).length);
	for (const p of m.providers) {
		assert.ok(p.name && p.name.length > 1, "friendly provider name");
		assert.ok(["good", "warn", "bad", "muted"].includes(p.tierTone));
		const firstScoped = p.rows.findIndex((r) => r.scoped);
		if (firstScoped !== -1) {
			assert.ok(p.rows.slice(firstScoped).every((r) => r.scoped), "model-limit rows come last");
		}
		for (const r of p.rows) {
			assert.ok(typeof r.label === "string" && r.label.length > 0);
			assert.ok(!/\(model\)/.test(r.label), "scoped label drops the '(model)' suffix");
			assert.ok(["good", "warn", "bad", "muted"].includes(r.leftTone));
		}
	}
	const anthropic = m.providers.find((p) => p.name === "Anthropic");
	assert.ok(anthropic, "anthropic rendered as 'Anthropic'");
	const fable = anthropic.rows.find((r) => r.scoped);
	assert.equal(fable.label, "Fable week");
	assert.equal(fable.left, 100);
	assert.equal(fable.leftTone, "good");
	const codex = m.providers.find((p) => p.name === "Codex");
	assert.ok(codex, "openai-codex rendered as 'Codex'");
	assert.equal(codex.tierTone, "bad");
	assert.ok(codex.rows.some((r) => r.left === 0 && r.leftTone === "bad"));
	assert.ok(m.footer.includes(statusV1.host));
});

test("tipModel surfaces block and fetch error, tolerates empty payload", () => {
	const payload = JSON.parse(JSON.stringify(statusV1));
	const pid = Object.keys(payload.providers)[0];
	payload.providers[pid].block = { until: payload.now + 600, message: "rate limited" };
	payload.providers[pid].fetch = { core: { last_ok_at: null, error: { kind: "http", message: "HTTP 500" } }, scoped: { last_ok_at: null, error: null } };
	payload.pending_count = 2;
	const m = tipPure.tipModel(payload, payload.now * 1000);
	const p = m.providers[0];
	assert.match(p.block, /rate limited/);
	assert.match(p.fetchError, /HTTP 500/);
	assert.match(m.footer, /2 alerts awaiting cron/);
	const empty = tipPure.tipModel({ v: 1, providers: {} }, Date.now());
	assert.equal(empty.providers.length, 0);
});

// ---- pane footer (the payload has no top-level fetched_at; only providers do) ----

const footerPure = (() => {
	const body = extractPureBlock(pluginSrc);
	const context = { console };
	vm.createContext(context);
	return vm.runInContext(`${body}\n({ paneFooter })`, context, { filename: "plugin-pure-footer.js" });
})();

function hhmmOf(epochS) {
	const d = new Date(epochS * 1000);
	return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

test("paneFooter reports the newest provider fetch, not a missing top-level fetched_at", () => {
	const payload = JSON.parse(JSON.stringify(statusV1));
	delete payload.fetched_at; // the real status payload never carries it
	const pids = Object.keys(payload.providers);
	assert.ok(pids.length >= 2, "fixture has two providers");
	const newest = payload.now - 75;
	payload.providers[pids[0]].fetched_at = payload.now - 53 * 3600;
	payload.providers[pids[1]].fetched_at = newest;
	const footer = footerPure.paneFooter(payload, payload.now * 1000);
	assert.equal(footer, `${payload.host} · fetched ${hhmmOf(newest)} · 75s old · poll 120s`);
	assert.doesNotMatch(footer, /—|\?/);
});

test("paneFooter ignores non-finite provider timestamps and degrades without any", () => {
	const payload = JSON.parse(JSON.stringify(statusV1));
	const pids = Object.keys(payload.providers);
	payload.providers[pids[0]].fetched_at = null;
	payload.providers[pids[1]].fetched_at = payload.now - 5;
	assert.match(footerPure.paneFooter(payload, payload.now * 1000), / · 5s old · /);
	for (const pid of pids) payload.providers[pid].fetched_at = "garbage";
	assert.equal(footerPure.paneFooter(payload, payload.now * 1000), `${payload.host} · fetched — · ?s old · poll 120s`);
	// A future timestamp (clock skew) never renders a negative age.
	payload.providers[pids[0]].fetched_at = payload.now + 30;
	assert.match(footerPure.paneFooter(payload, payload.now * 1000), / · 0s old · /);
	assert.equal(footerPure.paneFooter({ providers: {} }, Date.now()), "? · fetched — · ?s old · poll 120s");
});

// ---- cache efficiency (fixture = REAL `status --json --refresh` output of this branch) ----
// Regenerate: scripts/make_widget_fixture.py <captured cli output> > fixtures/status_v1_cli_output.txt

const cachePure = (() => {
	const body = extractPureBlock(pluginSrc);
	const context = { console };
	vm.createContext(context);
	return vm.runInContext(`${body}\n({ parseQrjson, chipSummary, tipModel, cacheModel, hitTone, fmtHit, fmtTokens })`, context, {
		filename: "plugin-pure-cache.js",
	});
})();
const realOutput = readFileSync(path.join(fixturesDir, "status_v1_cli_output.txt"), "utf8");

test("real CLI output: noise around one QRJSON line parses to a v1 payload with a cache object", () => {
	assert.ok(realOutput.split("\n").some((l) => l && !l.startsWith("QRJSON ")), "fixture carries noise lines");
	const payload = cachePure.parseQrjson(realOutput);
	assert.equal(payload.v, 1);
	assert.ok(payload.providers.anthropic && payload.providers["openai-codex"]);
	const c = payload.cache;
	assert.ok(c && typeof c.collected_at === "number");
	assert.deepEqual(Object.keys(c.providers).sort(), ["anthropic", "openai-codex", "other"]);
	for (const pid of Object.keys(c.providers)) {
		for (const w of ["24h", "7d"]) {
			const agg = c.providers[pid][w];
			for (const k of ["sessions", "input", "cache_read", "cache_write", "output", "hit", "sources"]) {
				assert.ok(k in agg, `${pid}.${w}.${k}`);
			}
		}
	}
	for (const t of c.top) {
		for (const k of ["id", "title", "source", "model", "provider", "input", "cache_read", "cache_write", "burn", "hit"]) {
			assert.ok(k in t, `top.${k}`);
		}
	}
	// existing helpers still work on the real payload
	assert.match(cachePure.chipSummary(payload), /A \d+% left/);
	assert.ok(cachePure.tipModel(payload, payload.now * 1000).providers.length >= 2);
});

test("cacheModel mirrors the real payload's 24h/7d hit and top burners", () => {
	const payload = cachePure.parseQrjson(realOutput);
	const m = cachePure.cacheModel(payload);
	assert.equal(m.available, true);
	assert.equal(m.error, "");
	const a = m.providers.find((p) => p.id === "anthropic");
	assert.equal(a.name, "Anthropic");
	assert.equal(a.hit24Text, cachePure.fmtHit(payload.cache.providers.anthropic["24h"].hit));
	assert.equal(a.hit7Text, cachePure.fmtHit(payload.cache.providers.anthropic["7d"].hit));
	assert.ok(["good", "warn", "bad", "muted"].includes(a.hit24Tone));
	assert.equal(m.top.length, payload.cache.top.length);
	assert.equal(m.top[0].burnText, cachePure.fmtTokens(payload.cache.top[0].burn));
	assert.ok(m.top.every((t) => t.label && t.meta.includes(" · ")));
});

test("cacheModel skips providers with no sessions, tolerates a missing cache object, surfaces errors", () => {
	assert.deepEqual(
		JSON.parse(JSON.stringify(cachePure.cacheModel({ v: 1, providers: {} }))),
		{ available: false, stale: false, providers: [], top: [], error: "" },
	);
	const payload = cachePure.parseQrjson(realOutput);
	payload.cache.providers.other = { "24h": { sessions: 0, hit: null }, "7d": { sessions: 0, hit: null } };
	payload.cache.fetch = { last_ok_at: null, error: { kind: "locked", message: "database is locked" } };
	const m = cachePure.cacheModel(payload);
	assert.ok(!m.providers.some((p) => p.id === "other"));
	assert.equal(m.error, "locked: database is locked");
});

test("hitTone / fmtHit / fmtTokens", () => {
	assert.equal(cachePure.hitTone(null, 80), "muted");
	assert.equal(cachePure.hitTone(49.9, 80), "bad");
	assert.equal(cachePure.hitTone(79.9, 80), "warn");
	assert.equal(cachePure.hitTone(80, 80), "good");
	assert.equal(cachePure.hitTone(85, 90), "warn");
	assert.equal(cachePure.hitTone(85, undefined), "good");
	assert.equal(cachePure.fmtHit(91.66), "91.7%");
	assert.equal(cachePure.fmtHit(null), "—");
	assert.equal(cachePure.fmtTokens(999), "999");
	assert.equal(cachePure.fmtTokens(12300), "12.3K");
	assert.equal(cachePure.fmtTokens(19_000_000), "19.0M");
	assert.equal(cachePure.fmtTokens(1_250_000_000), "1.25B");
});

test("errorChipLabel says what failed, the fix hint, and that a click syncs", () => {
	const err = { message: "quota-router backend missing or outdated on this host", hint: "hermes plugins enable quota-router" };
	const label = pure.errorChipLabel(err, false);
	assert.match(label, /backend missing or outdated/);
	assert.match(label, /hermes plugins enable quota-router/);
	assert.match(label, /click to sync now$/);
	assert.equal(pure.errorChipLabel({ message: "cli.exec timeout" }, false), "cli.exec timeout — click to sync now");
	assert.equal(pure.errorChipLabel(null, false), "quota-router error — click to sync now");
	assert.equal(pure.errorChipLabel(err, true), "Quota Router — syncing…");
});

// Drive the schedule the way @tanstack/query-core's retryer does: on each
// failure it asks retry(n) / retryDelay(n) with n = failures so far minus one
// (0 for the first retry), then increments n.
function simulateRetries(retry, retryDelay) {
	const delays = [];
	for (let n = 0; n < 100; n++) {
		if (!retry(n)) break;
		delays.push(retryDelay(n));
	}
	return delays;
}

test("retry schedule backs off, grows, caps at 30s, and gives up in about a minute", () => {
	const delays = simulateRetries(pure.shouldRetry, pure.retryDelayMs);
	assert.ok(delays.length >= 3 && delays.length <= 8, `retry count ${delays.length}`);
	for (let i = 1; i < delays.length; i++) assert.ok(delays[i] > delays[i - 1], `delay ${i} grows`);
	assert.ok(delays[0] >= 1_000 && delays[0] <= 5_000, "first retry soon, not instant");
	assert.ok(Math.max(...delays) <= 30_000, "capped at 30s");
	const total = delays.reduce((a, b) => a + b, 0);
	// Covers a backend restart (~6-10s observed), surfaces a real outage within ~2 min.
	assert.ok(total >= 20_000 && total <= 120_000, `total backoff ${total}ms`);
	// Out-of-range counts clamp instead of returning undefined.
	assert.equal(pure.retryDelayMs(999), Math.max(...delays));
	assert.equal(pure.retryDelayMs(-1), delays[0]);
	assert.equal(pure.retryDelayMs(undefined), delays[0]);
});

test("loadingChipLabel shows plain loading, then which retry is in flight", () => {
	const retries = simulateRetries(pure.shouldRetry, pure.retryDelayMs).length;
	assert.equal(pure.loadingChipLabel(0), "Loading quota…");
	assert.equal(pure.loadingChipLabel(undefined), "Loading quota…");
	assert.equal(pure.loadingChipLabel(1), `Quota Router — retrying (1 of ${retries})…`);
	assert.equal(pure.loadingChipLabel(retries + 3), `Quota Router — retrying (${retries} of ${retries})…`);
});
