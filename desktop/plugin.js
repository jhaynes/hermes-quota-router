/**
 * Hermes desktop widget for the `quota-router` plugin.
 *
 * Unified package door: $HERMES_HOME/plugins/quota-router/desktop/plugin.js
 * OFF by default (opt-in desktop half of a unified agent-plugin package).
 *
 * Data contract (v1): host.request('cli.exec', {argv:['quota-router','status','--json']})
 * -> {blocked, code, output} where output is stdout+stderr JOINED (max 48KB).
 * Find the LAST line starting with 'QRJSON ', JSON.parse the rest.
 *
 * Plain ESM, loaded uncompiled — UI is jsx() calls, not JSX syntax. Only
 * these imports resolve: @hermes/plugin-sdk, react, react/jsx-runtime.
 */

import {
	Badge,
	Button,
	cn,
	EmptyState,
	ErrorState,
	GlyphSpinner,
	host,
	PANES_AREA,
	queryClient,
	ROUTES_AREA,
	Skeleton,
	StatusDot,
	Tip,
	useMutation,
	useQuery,
	useValue,
} from "@hermes/plugin-sdk";
import { useState } from "react";
import { jsx, jsxs } from "react/jsx-runtime";

const ID = "quota-router";
const QUERY_KEY = ["quota-router", "status"];
const REFETCH_MS = 120_000;
const STALE_MS = 60_000;

// BEGIN PURE

/** Provider short-name for chip display. */
function shortName(pid) {
	if (pid === "anthropic") return "A";
	if (pid === "openai-codex") return "C";
	return String(pid || "?").slice(0, 1).toUpperCase();
}

/** Remaining % from a window's `used` (0-100 used%). Clamped 0-100. */
function remainingPct(w) {
	if (!w || w.used == null) return null;
	const n = Number(w.used);
	if (!Number.isFinite(n)) return null;
	return Math.max(0, Math.min(100, Math.round(100 - n)));
}

/** Format a reset timestamp (epoch seconds) relative to `now` (epoch ms or s). */
function fmtReset(epochS, now) {
	if (epochS == null) return "";
	const nowMs = now > 1e12 ? now : now * 1000;
	const targetMs = epochS * 1000;
	const diff = targetMs - nowMs;
	if (diff <= 0) return "resetting…";
	const totalMin = Math.floor(diff / 60_000);
	const days = Math.floor(totalMin / 1440);
	const hours = Math.floor((totalMin % 1440) / 60);
	const mins = totalMin % 60;
	if (days > 0) return `${days}d ${hours}h`;
	if (hours > 0) return `${hours}h ${mins}m`;
	if (mins > 0) return `${mins}m`;
	return "<1m";
}

/**
 * Find the last "QRJSON <json>" line in joined stdout+stderr and parse it.
 * Throws on no match / bad JSON.
 */
function parseQrjson(output) {
	const text = String(output ?? "");
	const lines = text.split("\n");
	let last = null;
	for (const line of lines) {
		if (line.startsWith("QRJSON ")) last = line.slice("QRJSON ".length);
	}
	if (last == null) throw new Error("no QRJSON line found in output");
	return JSON.parse(last);
}

/**
 * Build the status-bar chip summary string from a v1 payload.
 * e.g. "A 55% left · C 0% left · Fable 100%"
 */
function chipSummary(payload) {
	if (!payload || !payload.providers) return "";
	const parts = [];
	for (const pid of Object.keys(payload.providers)) {
		const p = payload.providers[pid];
		if (!p || !Array.isArray(p.windows)) continue;
		let worst = null;
		for (const w of p.windows) {
			if (!w.affects_tier) continue;
			const r = remainingPct(w);
			if (r == null) continue;
			if (worst == null || r < worst) worst = r;
		}
		if (worst != null) parts.push(`${shortName(pid)} ${worst}% left`);
	}
	let scopedBest = null;
	let scopedLabel = null;
	for (const pid of Object.keys(payload.providers)) {
		const p = payload.providers[pid];
		if (!p || !Array.isArray(p.windows)) continue;
		for (const w of p.windows) {
			if (w.affects_tier) continue;
			if (!w.scope) continue;
			const r = remainingPct(w);
			if (r == null) continue;
			if (scopedBest == null || r < scopedBest) {
				scopedBest = r;
				scopedLabel = w.label || w.scope;
			}
		}
	}
	if (scopedBest != null) parts.push(`${scopedLabel} ${scopedBest}%`);
	return parts.join(" · ");
}

/** Overall StatusDot tone for the payload's mode. */
function toneForMode(mode) {
	if (mode === "shift") return "warning";
	if (mode === "internal") return "error";
	return "ok";
}

// END PURE

/** One CLI call, timeout-guarded. Returns the raw cli.exec result. */
async function cliExec(argv, timeoutMs) {
	let timer = null;
	try {
		return await Promise.race([
			host.request("cli.exec", { argv, timeout: timeoutMs }),
			new Promise((_resolve, reject) => {
				timer = setTimeout(() => reject(new Error("cli.exec timeout")), (timeoutMs + 5) * 1000);
			}),
		]);
	} finally {
		if (timer) clearTimeout(timer);
	}
}

class BackendError extends Error {
	constructor(message, hint) {
		super(message);
		this.hint = hint || null;
	}
}

/** Fetch + parse the quota-router status payload; throws BackendError. */
async function fetchStatus(argv) {
	const result = await cliExec(argv, argv.includes("--refresh") ? 60 : 30);
	if (result?.blocked || result?.code !== 0) {
		throw new BackendError(
			"quota-router backend missing or outdated on this host",
			"hermes plugins enable quota-router",
		);
	}
	let payload;
	try {
		payload = parseQrjson(result.output || "");
	} catch {
		throw new BackendError(
			"quota-router backend missing or outdated on this host",
			"hermes plugins enable quota-router",
		);
	}
	if (payload.v !== 1) {
		throw new BackendError(
			`widget/backend version mismatch — update quota-router on ${payload.host || "this host"}`,
		);
	}
	return payload;
}

function useStatus() {
	return useQuery({
		queryKey: QUERY_KEY,
		queryFn: () => fetchStatus(["quota-router", "status", "--json"]),
		refetchInterval: REFETCH_MS,
		staleTime: STALE_MS,
		retry: 1,
	});
}

function useRefresh() {
	return useMutation({
		mutationFn: () => fetchStatus(["quota-router", "status", "--refresh", "--json"]),
		onSuccess: (data) => queryClient.setQueryData(QUERY_KEY, data),
	});
}

// ---- statusbar chip ---------------------------------------------------------

function chipTip(payload) {
	const lines = [];
	for (const pid of Object.keys(payload.providers || {})) {
		const p = payload.providers[pid];
		lines.push(`${pid} (${p.tier}${p.stale ? ", stale" : ""})`);
		for (const w of p.windows || []) {
			const r = remainingPct(w);
			const reset = w.resets_at ? ` · resets ${fmtReset(w.resets_at, Date.now())}` : "";
			lines.push(`  ${w.label}: ${r == null ? "—" : `${r}% left`}${reset}`);
		}
		if (p.block) lines.push(`  BLOCKED: ${p.block.message || "blocked"}`);
		const fetchErr = p.fetch?.core?.error || p.fetch?.scoped?.error;
		if (fetchErr) lines.push(`  fetch error: ${fetchErr.message || fetchErr.kind}`);
	}
	return lines.join("\n") || "Quota Router";
}

function QuotaChip() {
	const { data, isError, error } = useStatus();
	if (isError) {
		return jsx(Tip, {
			label: error?.message || "quota-router error",
			children: jsx(StatusDot, { tone: "error" }),
		});
	}
	if (!data || !data.providers) {
		return jsx(Tip, { label: "Loading quota…", children: jsx(StatusDot, { tone: "muted" }) });
	}
	const summary = chipSummary(data) || "no data";
	const tone = toneForMode(data.mode);
	return jsx(Tip, {
		label: chipTip(data),
		children: jsxs("button", {
			type: "button",
			className: cn(
				"inline-flex h-full items-center gap-1.5 px-1.5 text-[0.6875rem] transition-colors",
				"text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground",
			),
			onClick: () => host.navigate("/quota"),
			children: [jsx(StatusDot, { tone }), jsx("span", { children: summary })],
		}),
	});
}

// ---- pane / route content ---------------------------------------------------

function WindowBar({ w, now }) {
	const r = remainingPct(w);
	const pct = r == null ? 0 : r;
	return jsxs("div", {
		className: cn("flex flex-col gap-0.5", w.stale && "opacity-50"),
		children: [
			jsxs("div", {
				className: "flex items-center justify-between gap-2 text-xs",
				children: [
					jsx("span", { className: "text-(--ui-text-secondary)", children: w.label || "window" }),
					jsx("span", {
						className: "tabular-nums text-(--ui-text-primary)",
						children: r == null ? "—" : `${r}% left`,
					}),
				],
			}),
			jsx("div", {
				className: "h-1.5 w-full overflow-hidden rounded-full bg-(--ui-stroke-secondary)",
				children: jsx("div", {
					className: "h-full rounded-full",
					style: { width: `${pct}%`, background: "var(--ui-accent)" },
				}),
			}),
			w.resets_at
				? jsx("div", {
						className: "text-[0.6875rem] text-(--ui-text-secondary)",
						children: `resets ${fmtReset(w.resets_at, now)}`,
					})
				: null,
		],
	});
}

function ProviderCard({ pid, provider, now }) {
	const affects = (provider.windows || []).filter((w) => w.affects_tier);
	const scoped = (provider.windows || []).filter((w) => !w.affects_tier);
	const fetchErr = provider.fetch?.core?.error || provider.fetch?.scoped?.error;
	return jsxs("div", {
		className: "flex flex-col gap-2 rounded-lg border border-(--ui-stroke-secondary) px-3 py-2.5",
		children: [
			jsxs("div", {
				className: "flex items-center justify-between gap-2",
				children: [
					jsxs("span", {
						className: "flex items-center gap-2 text-sm font-semibold text-(--ui-text-primary)",
						children: [pid, provider.plan ? jsx(Badge, { children: provider.plan }) : null],
					}),
					jsx(Badge, { children: provider.tier || "unknown" }),
				],
			}),
			...affects.map((w, i) => jsx(WindowBar, { w, now, key: `${pid}-w-${i}` })),
			scoped.length > 0
				? jsxs("div", {
						className: "flex flex-col gap-2",
						children: [
							jsx("div", {
								className: "text-[0.6875rem] font-medium text-(--ui-text-secondary)",
								children: "Model limits",
							}),
							...scoped.map((w, i) => jsx(WindowBar, { w, now, key: `${pid}-s-${i}` })),
						],
					})
				: null,
			provider.block
				? jsx("div", {
						className: "rounded bg-(--ui-stroke-secondary) px-2 py-1 text-[0.6875rem] text-(--ui-text-primary)",
						children: provider.block.message || "blocked",
					})
				: null,
			fetchErr
				? jsx("div", {
						className: "text-[0.6875rem] text-(--ui-text-secondary)",
						children: `fetch error: ${fetchErr.message || fetchErr.kind}`,
					})
				: null,
			provider.stale
				? jsx("div", { className: "text-[0.6875rem] text-(--ui-text-secondary)", children: "stale" })
				: null,
		],
	});
}

function QuotaView() {
	const { data, isLoading, isError, error, refetch } = useStatus();
	const refresh = useRefresh();
	const now = Date.now();

	if (isLoading) {
		return jsxs("div", {
			className: "flex flex-col gap-2 p-3",
			children: [jsx(Skeleton, { className: "h-16 w-full" }), jsx(Skeleton, { className: "h-16 w-full" })],
		});
	}
	if (isError) {
		return jsx(ErrorState, {
			title: error?.message || "quota-router error",
			description: error?.hint || undefined,
			onRetry: () => refetch(),
		});
	}
	const providers = data && data.providers ? Object.keys(data.providers) : [];
	if (providers.length === 0) {
		return jsx(EmptyState, {
			title: "No snapshot yet — Refresh",
			action: jsx(Button, {
				onClick: () => refresh.mutate(),
				disabled: refresh.isPending,
				children: refresh.isPending ? jsx(GlyphSpinner, {}) : "Refresh",
			}),
		});
	}
	const fetchedAt = data.fetched_at ? new Date(data.fetched_at * 1000) : null;
	const ageS = fetchedAt ? Math.max(0, Math.round((now - fetchedAt.getTime()) / 1000)) : null;
	const hhmm = fetchedAt
		? `${String(fetchedAt.getHours()).padStart(2, "0")}:${String(fetchedAt.getMinutes()).padStart(2, "0")}`
		: "—";
	return jsxs("div", {
		className: "flex h-full flex-col gap-2 p-3",
		children: [
			jsx("div", { className: "text-sm font-medium", children: "Quota" }),
			jsx("div", {
				className: "flex flex-1 flex-col gap-2 overflow-y-auto",
				children: providers.map((pid) =>
					jsx(ProviderCard, { pid, provider: data.providers[pid], now, key: pid }),
				),
			}),
			data.pending_count > 0
				? jsx("div", {
						className: "text-[0.6875rem] text-(--ui-text-secondary)",
						children: `${data.pending_count} alerts awaiting cron`,
					})
				: null,
			jsxs("div", {
				className: "flex items-center justify-between gap-2 pt-1",
				children: [
					jsx("div", {
						className: "text-[0.6875rem] text-(--ui-text-secondary)",
						children: `${data.host || "?"} · fetched ${hhmm} · ${ageS == null ? "?" : ageS}s old · poll 120s`,
					}),
					jsx(Button, {
						onClick: () => refresh.mutate(),
						disabled: refresh.isPending,
						children: refresh.isPending ? jsx(GlyphSpinner, {}) : "Refresh",
					}),
				],
			}),
		],
	});
}

export default {
	id: ID,
	name: "Quota Router",
	defaultEnabled: false,
	register(ctx) {
		ctx.register({
			id: "chip",
			area: "statusBar.right",
			order: 105,
			render: () => jsx(QuotaChip, {}),
		});

		ctx.register({
			id: "pane",
			area: PANES_AREA ?? "panes",
			title: "Quota",
			data: { placement: "right", width: "300px" },
			render: () => jsx(QuotaView, {}),
		});

		ctx.register({
			id: "page",
			area: ROUTES_AREA,
			data: { path: "/quota" },
			render: () => jsx(QuotaView, {}),
		});
	},
};
