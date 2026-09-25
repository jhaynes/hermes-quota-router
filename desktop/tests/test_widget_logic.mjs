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
	const exportsList = ["shortName", "remainingPct", "fmtReset", "parseQrjson", "chipSummary", "toneForMode"];
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
	assert.match(summary, /Fable 100%/);
});

test("chipSummary handles empty/missing providers", () => {
	assert.equal(pure.chipSummary({}), "");
	assert.equal(pure.chipSummary(null), "");
	assert.equal(pure.chipSummary({ providers: {} }), "");
});

test("toneForMode maps mode to StatusDot tone", () => {
	assert.equal(pure.toneForMode("normal"), "ok");
	assert.equal(pure.toneForMode("shift"), "warning");
	assert.equal(pure.toneForMode("internal"), "error");
	assert.equal(pure.toneForMode("unknown"), "ok");
});

test("v2 fixture payload is distinguishable by v field (version mismatch path)", () => {
	assert.equal(statusV2.v, 2);
	assert.notEqual(statusV2.v, 1);
});
