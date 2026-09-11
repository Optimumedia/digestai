/* Tiny SVG chart builders for the admin dashboard. Built at build time, no library.
   Marks follow the data-viz spec: columns <= 24px with 4px rounded caps, 2px surface gaps
   between stacked segments, 2px lines with >= 8px end markers, hairline solid grid, text in
   text tokens. Every chart also has a table view on the page. */

export interface Series { key: string; label: string; color: string }

const esc = (s: unknown) => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/"/g, "&quot;");

function niceMax(v: number): number {
  if (v <= 0) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  const n = v / p;
  const m = n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10;
  return m * p;
}

function ticks(max: number, n = 4): number[] {
  const step = max / n;
  return Array.from({ length: n + 1 }, (_, i) => Math.round(i * step));
}

const fmt = (n: number) => (n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : String(Math.round(n)));

/** Stacked (or single-series) columns, one column per row. */
export function columns(rows: Record<string, any>[], xKey: string, series: Series[], opts: { w?: number; h?: number; labelEvery?: number; xFormat?: (v: any) => string } = {}): string {
  const w = opts.w ?? 640, h = opts.h ?? 200;
  const padL = 34, padR = 8, padT = 10, padB = 24;
  const pw = w - padL - padR, ph = h - padT - padB;
  const totals = rows.map((r) => series.reduce((s, k) => s + (Number(r[k.key]) || 0), 0));
  const max = niceMax(Math.max(1, ...totals));
  const band = pw / Math.max(1, rows.length);
  const bw = Math.min(24, Math.max(4, band * 0.62));
  const y = (v: number) => padT + ph - (v / max) * ph;
  const xf = opts.xFormat ?? ((v) => String(v).slice(5));
  const every = opts.labelEvery ?? Math.max(1, Math.ceil(rows.length / 7));
  let out = `<svg class="chart" viewBox="0 0 ${w} ${h}" role="img" aria-label="column chart">`;
  for (const t of ticks(max)) {
    out += `<line class="grid" x1="${padL}" x2="${w - padR}" y1="${y(t)}" y2="${y(t)}"/><text class="tick" x="${padL - 6}" y="${y(t) + 3}" text-anchor="end">${fmt(t)}</text>`;
  }
  rows.forEach((r, i) => {
    const cx = padL + band * i + band / 2;
    let acc = 0;
    const tip = `${esc(r[xKey])}: ` + series.map((s) => `${s.label} ${r[s.key] ?? 0}`).join(", ");
    series.forEach((s, si) => {
      const v = Number(r[s.key]) || 0;
      if (v <= 0) return;
      const y1 = y(acc + v), y0 = y(acc);
      const top = si === series.length - 1 || series.slice(si + 1).every((k) => !(Number(r[k.key]) || 0));
      const hh = Math.max(0, y0 - y1 - (acc > 0 ? 2 : 0));
      const yy = y1 + (acc > 0 ? 2 : 0);
      const rad = top ? 4 : 0;
      out += `<path class="mark" fill="${s.color}" d="M${cx - bw / 2},${yy + hh} v${-(hh - rad)} a${rad},${rad} 0 0 1 ${rad},${-rad} h${bw - 2 * rad} a${rad},${rad} 0 0 1 ${rad},${rad} v${hh - rad} z"><title>${tip}</title></path>`;
      acc += v;
    });
    if (i % every === 0) out += `<text class="tick" x="${cx}" y="${h - 6}" text-anchor="middle">${esc(xf(r[xKey]))}</text>`;
  });
  out += `<line class="axis" x1="${padL}" x2="${w - padR}" y1="${padT + ph}" y2="${padT + ph}"/></svg>`;
  return out;
}

/** One or more lines over the same x, with end markers and end labels. */
export function lines(rows: Record<string, any>[], xKey: string, series: Series[], opts: { w?: number; h?: number; xFormat?: (v: any) => string } = {}): string {
  const w = opts.w ?? 640, h = opts.h ?? 200;
  const padL = 34, padR = 56, padT = 10, padB = 24;
  const pw = w - padL - padR, ph = h - padT - padB;
  const max = niceMax(Math.max(1, ...rows.flatMap((r) => series.map((s) => Number(r[s.key]) || 0))));
  const x = (i: number) => padL + (rows.length > 1 ? (i / (rows.length - 1)) * pw : pw / 2);
  const y = (v: number) => padT + ph - (v / max) * ph;
  const xf = opts.xFormat ?? ((v) => String(v).slice(5));
  let out = `<svg class="chart" viewBox="0 0 ${w} ${h}" role="img" aria-label="line chart">`;
  for (const t of ticks(max)) out += `<line class="grid" x1="${padL}" x2="${w - padR}" y1="${y(t)}" y2="${y(t)}"/><text class="tick" x="${padL - 6}" y="${y(t) + 3}" text-anchor="end">${fmt(t)}</text>`;
  const every = Math.max(1, Math.ceil(rows.length / 7));
  rows.forEach((r, i) => { if (i % every === 0) out += `<text class="tick" x="${x(i)}" y="${h - 6}" text-anchor="middle">${esc(xf(r[xKey]))}</text>`; });
  for (const s of series) {
    const pts = rows.map((r, i) => [x(i), y(Number(r[s.key]) || 0)] as const);
    out += `<path class="line" stroke="${s.color}" d="${pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" ")}"/>`;
    const last = pts[pts.length - 1];
    if (last) {
      out += `<circle class="dot" cx="${last[0]}" cy="${last[1]}" r="4" fill="${s.color}"><title>${esc(s.label)}: ${rows[rows.length - 1][s.key] ?? 0}</title></circle>`;
      out += `<text class="endlabel" x="${last[0] + 8}" y="${last[1] + 4}">${esc(s.label)} ${fmt(Number(rows[rows.length - 1][s.key]) || 0)}</text>`;
    }
    rows.forEach((r, i) => { out += `<circle class="hit" cx="${pts[i][0]}" cy="${pts[i][1]}" r="9"><title>${esc(r[xKey])} · ${esc(s.label)}: ${r[s.key] ?? 0}</title></circle>`; });
  }
  out += `<line class="axis" x1="${padL}" x2="${w - padR}" y1="${padT + ph}" y2="${padT + ph}"/></svg>`;
  return out;
}

/** Horizontal bars, single series, labelled at the tip. */
export function bars(rows: { label: string; value: number; color?: string; title?: string }[], opts: { w?: number; color: string; rowH?: number }): string {
  const w = opts.w ?? 640, rowH = opts.rowH ?? 26;
  const labelW = 190, padR = 48;
  const h = rows.length * rowH + 4;
  const max = Math.max(1, ...rows.map((r) => r.value));
  const pw = w - labelW - padR;
  let out = `<svg class="chart" viewBox="0 0 ${w} ${h}" role="img" aria-label="bar chart">`;
  rows.forEach((r, i) => {
    const y0 = i * rowH + 3, bh = Math.min(18, rowH - 8);
    const bw = Math.max(2, (r.value / max) * pw);
    out += `<text class="ylabel" x="${labelW - 10}" y="${y0 + bh / 2 + 4}" text-anchor="end">${esc(r.label.length > 26 ? r.label.slice(0, 25) + "…" : r.label)}</text>`;
    out += `<path class="mark" fill="${r.color ?? opts.color}" d="M${labelW},${y0} h${bw - 4} a4,4 0 0 1 4,4 v${bh - 8} a4,4 0 0 1 -4,4 h${-(bw - 4)} z"><title>${esc(r.title ?? `${r.label}: ${r.value}`)}</title></path>`;
    out += `<text class="value" x="${labelW + bw + 6}" y="${y0 + bh / 2 + 4}">${fmt(r.value)}</text>`;
  });
  out += `</svg>`;
  return out;
}
