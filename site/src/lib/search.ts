/* Derived numbers for the admin Search tab ("how are we ranking on Google?") from gsc.json.
   Positions are always averaged weighted by impressions, never as a mean of daily averages, and
   a day without impressions has no position (not 0). */

export type Day = { day: string; clicks: number; impressions: number; position: number | null; queries?: number };

/** Impressions below this per side make a week-on-week move in position noise, not news. */
export const MIN_IMPRESSIONS = 50;
/** Below this over 28 days the page says positions are not meaningful yet. */
export const MEANINGFUL_IMPRESSIONS = 100;

const addDay = (d: string, n: number) => new Date(Date.parse(d + "T00:00:00Z") + n * 864e5).toISOString().slice(0, 10);

function normal(r: any): Day {
  const impressions = Number(r?.impressions) || 0;
  const p = Number(r?.position);
  return { day: String(r.day), clicks: Number(r?.clicks) || 0, impressions, position: impressions > 0 && p > 0 ? p : null, queries: r?.queries };
}

/** Every day from `from` to `to`; days the file does not list count as no impressions. */
export function fillDays(rows: any[], from: string, to: string): Day[] {
  const by = new Map(rows.map((r) => [String(r.day), normal(r)]));
  const out: Day[] = [];
  for (let d = from; d <= to; d = addDay(d, 1)) out.push(by.get(d) ?? { day: d, clicks: 0, impressions: 0, position: null });
  return out;
}

export function weightedPosition(rows: { impressions: number; position: number | null }[]): number | null {
  let s = 0, n = 0;
  for (const r of rows) if (r.position != null && r.impressions > 0) { s += r.position * r.impressions; n += r.impressions; }
  return n ? s / n : null;
}

/** "page 2 of Google's results" style reading of an average position. */
export function explainPosition(p: number | null): string {
  if (p == null) return "Google did not show the site in these days";
  if (p <= 3) return "On average the site appeared near the top of Google's first page";
  const page = Math.ceil(p / 10);
  return page === 1 ? "On average the site appeared on the first page of Google's results" : `On average the site appeared on page ${page} of Google's results`;
}

/** Change in position between two periods, positive when the site moved up (a lower number). */
export function positionChange(now: number | null | undefined, before: number | null | undefined): number | null {
  return now == null || before == null ? null : before - now;
}

const esc = (s: unknown) => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/"/g, "&quot;");

/** The change line of a tile and its tone ("good", "bad" or "" when too small to judge).
    position: lower is better, shown in places; count: percent once the base is 20 or more;
    rate: percentage points, judged only with enough impressions. */
export function tileChange(now: number | null, before: number | null, kind: "position" | "count" | "rate", enough: boolean): { html: string; tone: string } {
  if (now == null || before == null) return { html: "", tone: "" };
  const arrow = (up: boolean) => `<span class="cmp-arrow" aria-hidden="true">${up ? "▲" : "▼"}</span>`;
  if (kind === "position") {
    const d = before - now;
    if (Math.abs(d) < 0.05) return { html: `<span class="cmp-change">＝ no change</span>`, tone: "" };
    const tone = enough ? (d > 0 ? "good" : "bad") : "";
    return { html: `<span class="cmp-change">${arrow(d > 0)}${Math.abs(d).toFixed(1)} places ${d > 0 ? "higher" : "lower"} <span class="cmp-from">(from ${before.toFixed(1)})</span></span>`, tone };
  }
  if (kind === "rate") {
    const d = (now - before) * 100;
    if (Math.abs(d) < 0.05) return { html: `<span class="cmp-change">＝ no change</span>`, tone: "" };
    return { html: `<span class="cmp-change">${arrow(d > 0)}${d > 0 ? "+" : "−"}${Math.abs(d).toFixed(1)} pts <span class="cmp-from">(from ${(before * 100).toFixed(1)}%)</span></span>`, tone: enough ? (d > 0 ? "good" : "bad") : "" };
  }
  const d = now - before;
  if (d === 0) return { html: `<span class="cmp-change">＝ no change</span>`, tone: "" };
  if (before >= 20) {
    const p = Math.round((d / before) * 100);
    return { html: `<span class="cmp-change">${arrow(d > 0)}${d > 0 ? "+" : "−"}${Math.abs(p)}% <span class="cmp-from">(from ${before})</span></span>`, tone: d > 0 ? "good" : "bad" };
  }
  return { html: `<span class="cmp-change">${arrow(d > 0)}${d > 0 ? "+" : "−"}${Math.abs(d)} <span class="cmp-from">(from ${before})</span></span>`, tone: "" };
}

/** Table cell for a change in position: green arrow up when the site moved up. */
export const plural = (n: number, one: string, many = `${one}s`) => `${n.toLocaleString("en-US")} ${n === 1 ? one : many}`;

/** Hover text for one day of the position chart. */
export function dayTip(label: (v: any) => string) {
  return (r: any) => `${label(r.day)}: ${r.position == null ? "Google did not show the site (no impressions)" : `average position ${r.position.toFixed(1)} · ${plural(r.impressions, "impression")} · ${plural(r.clicks, "click")}`}`;
}

/** Hover text for a query's sparkline. */
export function sparkTitle(q: { query: string; spark: (number | null)[] | null }): string {
  const got = (q.spark || []).filter((v): v is number => v != null);
  if (!got.length) return "";
  return got.length === 1 ? `${q.query}: shown on one day, at position ${got[0].toFixed(1)}`
    : `${q.query}: shown on ${got.length} days, best position ${Math.min(...got).toFixed(1)}, worst ${Math.max(...got).toFixed(1)}`;
}

export function rowChange(r: { position: number; prevPosition?: number; change: number | null }): string {
  if (r.change == null) return `<span class="muted" title="Google did not show it for this in the 28 days before">new</span>`;
  if (Math.abs(r.change) < 0.05) return `<span class="muted" title="Same average position as the 28 days before">＝</span>`;
  const up = r.change > 0;
  const title = `${up ? "Up" : "Down"} ${Math.abs(r.change).toFixed(1)} places: from ${r.prevPosition?.toFixed(1)} to ${r.position.toFixed(1)}`;
  return `<span class="st ${up ? "good" : "bad"}" title="${esc(title)}"><span aria-hidden="true">${up ? "▲" : "▼"}</span> ${Math.abs(r.change).toFixed(1)}<span class="cmp-sr"> places ${up ? "higher" : "lower"}</span></span>`;
}

export function searchView(gsc: any) {
  const end: string = gsc.end;
  const perDay = fillDays(gsc.perDay || [], gsc.start, end);
  const sum = (rows: Day[], k: "clicks" | "impressions") => rows.reduce((n, r) => n + r[k], 0);
  const week = perDay.slice(-7), before = perDay.slice(-14, -7);
  const w = {
    from: week[0]?.day, to: end,
    position: weightedPosition(week), prevPosition: weightedPosition(before),
    impressions: sum(week, "impressions"), prevImpressions: sum(before, "impressions"),
    clicks: sum(week, "clicks"), prevClicks: sum(before, "clicks"),
  };
  const ctr = w.impressions ? w.clicks / w.impressions : null;
  const prevCtr = w.prevImpressions ? w.prevClicks / w.prevImpressions : null;
  const enough = Math.min(w.impressions, w.prevImpressions) >= MIN_IMPRESSIONS;

  // The daily chart: up to 90 days of the longer history when it carries positions (older files
  // do not), starting at the first day Google showed the site.
  const hist: any[] = gsc.history || [];
  const withPos = hist.length && hist.some((r) => "position" in r);
  const from = addDay(end, -89);
  let chart = withPos ? fillDays(hist.filter((r) => r.day >= from), hist.find((r) => r.day >= from)?.day ?? gsc.start, end) : perDay;
  const firstShown = chart.findIndex((r) => r.impressions > 0);
  chart = firstShown < 0 ? chart.slice(-14) : chart.slice(Math.max(0, Math.min(firstShown, chart.length - 14)));

  const days = fillDays([], gsc.start, end).map((d) => d.day);
  const queries = (gsc.queries || []).map((q: any, i: number) => {
    const at = new Map((q.days || []).map((x: any[]) => [x[0], x[2]]));
    return {
      ...q, change: positionChange(q.position, q.prevPosition),
      spark: i < 10 && q.days ? days.map((d) => (at.get(d) as number | undefined) ?? null) : null,
    };
  });
  const pages = (gsc.pages || []).map((p: any) => ({ ...p, change: positionChange(p.position, p.prevPosition) }));
  const total = { impressions: sum(perDay, "impressions"), clicks: sum(perDay, "clicks"), position: weightedPosition(perDay) };
  const checks: any[] = gsc.inspections || [];
  const queryCount = gsc.queryCount ?? queries.length, pageCount = gsc.pageCount ?? pages.length;
  const change = {
    position: tileChange(w.position, w.prevPosition, "position", enough),
    impressions: tileChange(w.impressions, w.prevImpressions, "count", true),
    clicks: tileChange(w.clicks, w.prevClicks, "count", true),
    ctr: tileChange(ctr, prevCtr, "rate", Math.min(w.impressions, w.prevImpressions) >= MEANINGFUL_IMPRESSIONS),
    queries: tileChange(queryCount, gsc.prevQueryCount ?? null, "count", true),
    pages: tileChange(pageCount, gsc.prevPageCount ?? null, "count", true),
  };
  return {
    week: w, ctr, prevCtr, enough, chart, queries, pages, total, change,
    queryCount, pageCount,
    indexed: checks.filter((c) => /^(submitted and indexed|indexed)/i.test(String(c.state || ""))).length, inspected: checks.length,
    sparse: total.impressions < MEANINGFUL_IMPRESSIONS,
  };
}
