/* Tiny SVG chart builders for the admin dashboard. Built at build time, no library.
   Marks follow the data-viz spec: columns <= 28px with 4px rounded caps, 2px surface gaps
   between stacked segments, 2px lines with >= 8px end markers, hairline solid grid, text in
   text tokens. Every chart also has a table view on the page. */

export interface Series { key: string; label: string; color: string }

const esc = (s: unknown) => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/"/g, "&quot;");

/** A round step (1, 2 or 5 times a power of ten) that splits the data into about `n` bands,
    and the axis maximum as a whole number of those steps. Ticks are always round numbers. */
function scale(v: number, n = 4): { max: number; ticks: number[] } {
  const raw = Math.max(1, v) / n;
  const p = Math.pow(10, Math.floor(Math.log10(raw)));
  const r = raw / p;
  const step = Math.max(1, (r <= 1 ? 1 : r <= 2 ? 2 : r <= 5 ? 5 : 10) * p);
  const max = step * Math.max(1, Math.ceil(Math.max(1, v) / step));
  return { max, ticks: Array.from({ length: Math.round(max / step) + 1 }, (_, i) => i * step) };
}

const fmt = (n: number) => (n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1).replace(/\.0$/, "")}k` : String(Math.round(n)));

/** Drop the leading days before any series has data, so a young site's chart is not mostly
    empty. Keeps at least `min` days; with no data at all, keeps the last week. */
export function sinceFirst<T extends Record<string, any>>(rows: T[], keys: string[], min = 3): T[] {
  const i = rows.findIndex((r) => keys.some((k) => Number(r[k]) > 0));
  if (i < 0) return rows.slice(-7);
  return rows.slice(Math.max(0, Math.min(i, rows.length - min)));
}

const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
/** x-axis formatter for ISO days: "Sep 12", and "today" for the build day (UTC). */
export function dayLabel(today = new Date().toISOString().slice(0, 10)) {
  return (v: any) => {
    const s = String(v).slice(0, 10);
    if (s === today) return "today";
    const [, m, d] = s.split("-").map(Number);
    return m ? `${MON[m - 1]} ${d}` : s;
  };
}

function capPath(x: number, base: number, top: number, bw: number): string {
  const hh = base - top;
  const rad = Math.max(0, Math.min(4, hh, bw / 2));
  return `M${x},${base} V${top + rad} a${rad},${rad} 0 0 1 ${rad},${-rad} H${x + bw - rad} a${rad},${rad} 0 0 1 ${rad},${rad} V${base} Z`;
}

/** Columns, one group per row. Stacked by default; `grouped` puts the series side by side on
    one shared axis (for related counts that are not parts of a whole). With ten rows or fewer,
    grouped columns carry their value on top. */
export function columns(rows: Record<string, any>[], xKey: string, series: Series[], opts: { w?: number; h?: number; labelEvery?: number; xFormat?: (v: any) => string; grouped?: boolean } = {}): string {
  const w = opts.w ?? 640, h = opts.h ?? 200;
  const padL = 34, padR = 8, padT = opts.grouped ? 18 : 10, padB = 24;
  const pw = w - padL - padR, ph = h - padT - padB;
  const base = padT + ph;
  const values = opts.grouped
    ? rows.flatMap((r) => series.map((s) => Number(r[s.key]) || 0))
    : rows.map((r) => series.reduce((s, k) => s + (Number(r[k.key]) || 0), 0));
  const { max, ticks: yTicks } = scale(Math.max(0, ...values));
  const band = pw / Math.max(1, rows.length);
  const y = (v: number) => padT + ph - (v / max) * ph;
  const xf = opts.xFormat ?? ((v) => String(v).slice(5));
  const every = opts.labelEvery ?? Math.max(1, Math.ceil(rows.length / 7));
  // Numbers over the bars when they fit (about 6.5 px a character at the chart's size), not by a
  // count of days: past ten days they used to vanish. Too narrow for every bar: the first series'
  // number over the group; too narrow for that too: none, and the hover title has them all.
  const textW = (v: number) => fmt(v).length * 6.5;
  let out = `<svg class="chart" viewBox="0 0 ${w} ${h}" role="img" aria-label="column chart">`;
  for (const t of yTicks) {
    out += `<line class="grid" x1="${padL}" x2="${w - padR}" y1="${y(t)}" y2="${y(t)}"/><text class="tick" x="${padL - 6}" y="${y(t) + 3}" text-anchor="end">${fmt(t)}</text>`;
  }
  rows.forEach((r, i) => {
    const cx = padL + band * i + band / 2;
    const tip = `${esc(xf(r[xKey]))}: ` + series.map((s) => `${s.label} ${r[s.key] ?? 0}`).join(", ");
    if (opts.grouped) {
      const gap = 3, n = series.length;
      const bw = Math.min(28, Math.max(4, (band * 0.7 - gap * (n - 1)) / n));
      const x0 = cx - (bw * n + gap * (n - 1)) / 2;
      const vals = series.map((s) => Number(r[s.key]) || 0);
      const anyValue = vals.some((v) => v > 0);
      const eachFits = vals.every((v) => textW(v) <= bw + gap);
      series.forEach((s, si) => {
        const v = vals[si];
        const x = x0 + si * (bw + gap);
        if (v > 0) out += `<path class="mark" fill="${s.color}" d="${capPath(x, base, y(v), bw)}"><title>${tip}</title></path>`;
        if (anyValue && eachFits) out += `<text class="value" x="${x + bw / 2}" y="${(v > 0 ? y(v) : base) - 5}" text-anchor="middle">${fmt(v)}</text>`;
      });
      if (anyValue && !eachFits && textW(vals[0]) <= band - 4) {
        out += `<text class="value" x="${cx}" y="${y(Math.max(...vals)) - 5}" text-anchor="middle">${fmt(vals[0])}</text>`;
      }
      out += `<rect class="hit" x="${cx - band / 2}" y="${padT}" width="${band}" height="${ph}"><title>${tip}</title></rect>`;
    } else {
      const bw = Math.min(24, Math.max(4, band * 0.62));
      let acc = 0;
      series.forEach((s, si) => {
        const v = Number(r[s.key]) || 0;
        if (v <= 0) return;
        const y1 = y(acc + v), y0 = y(acc);
        const top = si === series.length - 1 || series.slice(si + 1).every((k) => !(Number(r[k.key]) || 0));
        const hh = Math.max(0, y0 - y1 - (acc > 0 ? 2 : 0));
        const yy = y1 + (acc > 0 ? 2 : 0);
        const rad = top ? Math.min(4, hh) : 0;
        out += `<path class="mark" fill="${s.color}" d="M${cx - bw / 2},${yy + hh} v${-(hh - rad)} a${rad},${rad} 0 0 1 ${rad},${-rad} h${bw - 2 * rad} a${rad},${rad} 0 0 1 ${rad},${rad} v${hh - rad} z"><title>${tip}</title></path>`;
        acc += v;
      });
    }
    if (i % every === 0) out += `<text class="tick" x="${cx}" y="${h - 6}" text-anchor="middle">${esc(xf(r[xKey]))}</text>`;
  });
  out += `<line class="axis" x1="${padL}" x2="${w - padR}" y1="${base}" y2="${base}"/></svg>`;
  return out;
}

/** One or more lines over the same x, with end markers and end labels that never overlap. */
export function lines(rows: Record<string, any>[], xKey: string, series: Series[], opts: { w?: number; h?: number; xFormat?: (v: any) => string } = {}): string {
  const w = opts.w ?? 640, h = opts.h ?? 200;
  const padL = 34, padR = 76, padT = 10, padB = 24;
  const pw = w - padL - padR, ph = h - padT - padB;
  const { max, ticks: yTicks } = scale(Math.max(0, ...rows.flatMap((r) => series.map((s) => Number(r[s.key]) || 0))));
  const x = (i: number) => padL + (rows.length > 1 ? (i / (rows.length - 1)) * pw : pw / 2);
  const y = (v: number) => padT + ph - (v / max) * ph;
  const xf = opts.xFormat ?? ((v) => String(v).slice(5));
  let out = `<svg class="chart" viewBox="0 0 ${w} ${h}" role="img" aria-label="line chart">`;
  for (const t of yTicks) out += `<line class="grid" x1="${padL}" x2="${w - padR}" y1="${y(t)}" y2="${y(t)}"/><text class="tick" x="${padL - 6}" y="${y(t) + 3}" text-anchor="end">${fmt(t)}</text>`;
  const every = Math.max(1, Math.ceil(rows.length / 7));
  rows.forEach((r, i) => { if (i % every === 0) out += `<text class="tick" x="${x(i)}" y="${h - 6}" text-anchor="middle">${esc(xf(r[xKey]))}</text>`; });
  const lastRow = rows[rows.length - 1];
  const ends: { label: string; ly: number; dy: number }[] = [];
  for (const s of series) {
    const pts = rows.map((r, i) => [x(i), y(Number(r[s.key]) || 0)] as const);
    out += `<path class="line" stroke="${s.color}" d="${pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" ")}"/>`;
    const last = pts[pts.length - 1];
    if (last) {
      const v = Number(lastRow[s.key]) || 0;
      out += `<circle class="dot" cx="${last[0]}" cy="${last[1]}" r="4" fill="${s.color}"><title>${esc(s.label)}: ${v}</title></circle>`;
      ends.push({ label: `${s.label} ${fmt(v)}`, ly: last[1], dy: last[1] });
    }
    rows.forEach((r, i) => { out += `<circle class="hit" cx="${pts[i][0]}" cy="${pts[i][1]}" r="9"><title>${esc(xf(r[xKey]))} · ${esc(s.label)}: ${r[s.key] ?? 0}</title></circle>`; });
  }
  // Spread end labels at least one line apart, then pull the stack back inside the plot.
  const gapY = 13;
  ends.sort((a, b) => a.ly - b.ly);
  for (let i = 1; i < ends.length; i++) ends[i].dy = Math.max(ends[i].ly, ends[i - 1].dy + gapY);
  const overflow = ends.length ? ends[ends.length - 1].dy - (padT + ph) : 0;
  if (overflow > 0) ends.forEach((e) => (e.dy -= overflow));
  for (const e of ends) out += `<text class="endlabel" x="${w - padR + 10}" y="${e.dy + 4}">${esc(e.label)}</text>`;
  out += `<line class="axis" x1="${padL}" x2="${w - padR}" y1="${padT + ph}" y2="${padT + ph}"/></svg>`;
  return out;
}

/** Rank axis for Google positions: 1 (the top result) at the top, a round bottom (10, 20, 50,
    100 or a round step above) at the bottom, and ticks at 1 plus round steps. Linear, so the
    distance between page 1 (positions 1-10) and page 2 reads true. */
export function rankScale(worst: number): { max: number; ticks: number[] } {
  const w = Math.max(1, worst);
  if (w <= 10) return { max: 10, ticks: [1, 5, 10] };
  if (w <= 20) return { max: 20, ticks: [1, 5, 10, 15, 20] };
  if (w <= 50) return { max: 50, ticks: [1, 10, 20, 30, 40, 50] };
  const { max, ticks } = w <= 100 ? { max: 100, ticks: [0, 10, 20, 40, 60, 80, 100] } : scale(w, 5);
  return { max, ticks: [1, ...ticks.filter((t) => t > 1)] };
}

/** Daily Google position as a line on an inverted axis (better ranking is higher). Days with no
    value (Google showed the site nowhere) stay gaps: the line breaks and nothing is drawn at 0.
    A day without a neighbour on either side still shows as a dot. Uses the same horizontal layout
    as columns(), so a columns chart of the same rows and width lines up underneath. */
export function rankLine(rows: Record<string, any>[], xKey: string, key: string, opts: { w?: number; h?: number; color: string; xFormat?: (v: any) => string; tip?: (r: Record<string, any>) => string; label?: string }): string {
  const w = opts.w ?? 640, h = opts.h ?? 200;
  const padL = 34, padR = 8, padT = 10, padB = 24;
  const pw = w - padL - padR, ph = h - padT - padB;
  const val = (r: Record<string, any>) => (r[key] == null || !isFinite(Number(r[key])) || Number(r[key]) <= 0 ? null : Number(r[key]));
  const vals = rows.map(val);
  const { max, ticks } = rankScale(Math.max(1, ...vals.filter((v): v is number => v != null)));
  const band = pw / Math.max(1, rows.length);
  const x = (i: number) => padL + band * i + band / 2;
  const y = (v: number) => padT + ((Math.min(v, max) - 1) / Math.max(1, max - 1)) * ph;
  const xf = opts.xFormat ?? ((v) => String(v).slice(5));
  let out = `<svg class="chart" viewBox="0 0 ${w} ${h}" role="img" aria-label="${esc(opts.label ?? "line chart of position, 1 at the top")}">`;
  // Page 1 of Google's results (positions 1 to 10) as a faint band, when the axis goes beyond it.
  if (max > 10) out += `<rect class="band" x="${padL}" y="${padT}" width="${pw}" height="${(y(10.5) - padT).toFixed(1)}"><title>Positions 1 to 10: the first page of Google's results</title></rect>`;
  for (const t of ticks) out += `<line class="grid" x1="${padL}" x2="${w - padR}" y1="${y(t)}" y2="${y(t)}"/><text class="tick" x="${padL - 6}" y="${y(t) + 3}" text-anchor="end">${t}</text>`;
  const every = Math.max(1, Math.ceil(rows.length / 7));
  rows.forEach((r, i) => { if (i % every === 0) out += `<text class="tick" x="${x(i)}" y="${h - 6}" text-anchor="middle">${esc(xf(r[xKey]))}</text>`; });
  let d = "";
  vals.forEach((v, i) => { if (v != null) d += `${i && vals[i - 1] != null ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)} `; });
  if (d) out += `<path class="line" stroke="${opts.color}" d="${d.trim()}"/>`;
  const last = vals.reduce((n: number, v, i) => (v != null ? i : n), -1);
  vals.forEach((v, i) => {
    if (v == null) return;
    const alone = vals[i - 1] == null && vals[i + 1] == null;
    if (alone || i === last || rows.length <= 120) out += `<circle class="dot" cx="${x(i).toFixed(1)}" cy="${y(v).toFixed(1)}" r="${i === last ? 4.5 : alone ? 3.5 : 2.5}" fill="${opts.color}"/>`;
  });
  rows.forEach((r, i) => {
    const v = vals[i];
    const tip = opts.tip ? opts.tip(r) : `${xf(r[xKey])}: ${v == null ? "not shown on Google" : `position ${v.toFixed(1)}`}`;
    out += `<rect class="hit" x="${(padL + band * i).toFixed(1)}" y="${padT}" width="${band.toFixed(1)}" height="${ph}"><title>${esc(tip)}</title></rect>`;
  });
  out += `<line class="axis" x1="${padL}" x2="${w - padR}" y1="${padT + ph}" y2="${padT + ph}"/></svg>`;
  return out;
}

/** A word-sized inverted line of daily positions for a table row: higher is better, gaps stay
    gaps. Its own vertical range (at least 5 positions tall, so small moves look small). */
export function rankSpark(points: (number | null)[], opts: { w?: number; h?: number; color: string; title?: string }): string {
  const w = opts.w ?? 96, h = opts.h ?? 22, pad = 3;
  const got = points.filter((v): v is number => v != null && v > 0);
  if (!got.length) return "";
  let lo = Math.min(...got), hi = Math.max(...got);
  if (hi - lo < 5) { const mid = (hi + lo) / 2; lo = Math.max(1, mid - 2.5); hi = lo + 5; }
  const x = (i: number) => pad + (points.length > 1 ? (i / (points.length - 1)) * (w - 2 * pad) : (w - 2 * pad) / 2);
  const y = (v: number) => pad + ((v - lo) / (hi - lo)) * (h - 2 * pad);
  let d = "", dots = "";
  points.forEach((v, i) => {
    if (v == null || v <= 0) return;
    d += `${i && points[i - 1] != null ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)} `;
    if (points[i - 1] == null && points[i + 1] == null) dots += `<circle cx="${x(i).toFixed(1)}" cy="${y(v).toFixed(1)}" r="2.2" fill="${opts.color}"/>`;
  });
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" role="img" aria-label="${esc(opts.title ?? "daily position")}"><title>${esc(opts.title ?? "")}</title>${d ? `<path class="line" stroke="${opts.color}" d="${d.trim()}"/>` : ""}${dots}</svg>`;
}

/** Horizontal stacked bars: one row per entity, segments per series (shares of a whole). */
export function stackedBars(rows: Record<string, any>[], labelKey: string, series: Series[], opts: { w?: number; rowH?: number } = {}): string {
  const w = opts.w ?? 640, rowH = opts.rowH ?? 24;
  const labelW = 190, padR = 44;
  const h = rows.length * rowH + 4;
  const pw = w - labelW - padR;
  let out = `<svg class="chart" viewBox="0 0 ${w} ${h}" role="img" aria-label="stacked bar chart">`;
  rows.forEach((r, i) => {
    const total = series.reduce((s, k) => s + (Number(r[k.key]) || 0), 0) || 1;
    const y0 = i * rowH + 3, bh = Math.min(16, rowH - 8);
    const label = String(r[labelKey]);
    out += `<text class="ylabel" x="${labelW - 10}" y="${y0 + bh / 2 + 4}" text-anchor="end">${esc(label.length > 26 ? label.slice(0, 25) + "…" : label)}</text>`;
    let x = labelW;
    const tip = `${esc(label)}: ` + series.map((s) => `${s.label} ${r[s.key] ?? 0}`).join(", ");
    series.forEach((s) => {
      const v = Number(r[s.key]) || 0;
      if (!v) return;
      const bw = Math.max(0, (v / total) * pw - 2);
      out += `<rect class="mark" fill="${s.color}" x="${x}" y="${y0}" width="${bw}" height="${bh}" rx="2"><title>${tip}</title></rect>`;
      x += (v / total) * pw;
    });
    out += `<text class="value" x="${labelW + pw + 6}" y="${y0 + bh / 2 + 4}">${fmt(total)}</text>`;
  });
  out += `</svg>`;
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
