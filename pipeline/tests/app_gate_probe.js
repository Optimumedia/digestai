/* Runs site/public/app.js in Node against a small stand-in for the browser, with its own clock, and
   prints what the page would have sent in each case: {"case name": [{at, type, value}, ...]}.
   Used by test_units.py (test_app_js_counts_a_view_only_for_a_reader). Nothing leaves the process:
   fetch is the recorder. Usage: node app_gate_probe.js <path to app.js> */
const fs = require("node:fs");
const vm = require("node:vm");

const code = fs.readFileSync(process.argv[2], "utf-8");
const SWIFTSHADER = "ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (Subzero) (0x0000C0DE)), SwiftShader driver)";
const RADEON = "ANGLE (AMD, AMD Radeon(TM) 860M Graphics (0x00001114) Direct3D11 vs_5_0 ps_5_0, D3D11)";

/* One page load. `o` overrides: visible, outer, screen, languages, webdriver, renderer (null = WebGL off),
   firefox (the renderer is named without the extension), path, title. */
function load(o = {}) {
  let clock = 1_790_000_000_000;
  const t0 = clock;
  let timers = [], seq = 0;
  const sent = [];
  const listeners = { window: {}, document: {} };
  const on = (where) => (type, fn) => { (listeners[where][type] ||= []).push(fn); };
  const off = (where) => (type, fn) => { listeners[where][type] = (listeners[where][type] || []).filter((f) => f !== fn); };
  const storage = () => { const m = new Map(); return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)) }; };
  class FakeDate extends Date {
    constructor(...a) { if (a.length) super(...a); else super(clock); }
    static now() { return clock; }
  }
  const gl = o.renderer === null ? null : {
    RENDERER: 1,
    getParameter: (p) => (p === 1 ? (o.firefox ? o.renderer || RADEON : "WebKit WebGL") : o.renderer || RADEON),
    getExtension: (name) => (name === "WEBGL_debug_renderer_info" && !o.firefox ? { UNMASKED_RENDERER_WEBGL: 2 } : null),
  };
  const state = { visible: o.visible !== false };
  const document = {
    title: o.title || "A story — Digest AI", referrer: "", documentElement: { dataset: {} },
    body: { dataset: { storyId: "9", articleId: "419" } },
    get visibilityState() { return state.visible ? "visible" : "hidden"; },
    querySelector: () => null, querySelectorAll: () => [], getElementById: () => null,
    createElement: () => ({ getContext: (kind) => (kind === "webgl" ? gl : null) }),
    addEventListener: on("document"), removeEventListener: off("document"),
  };
  const w = {
    DIGEST: { supabaseUrl: "http://stub.invalid", supabaseKey: "stub" },
    document, Date: FakeDate, URL, URLSearchParams,
    navigator: { webdriver: o.webdriver === true, userAgent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/152.0", languages: o.languages || ["en-GB", "en"] },
    location: { search: "", pathname: o.path || "/story/a-story", hostname: "digestai.news", protocol: "https:", href: "https://digestai.news/story/a-story" },
    localStorage: storage(), sessionStorage: storage(),
    outerWidth: (o.outer || [1280, 900])[0], outerHeight: (o.outer || [1280, 900])[1],
    screen: { width: (o.screen || [1920, 1080])[0], height: (o.screen || [1920, 1080])[1] },
    innerHeight: 800, scrollY: 0,
    matchMedia: () => ({ matches: false }),
    addEventListener: on("window"), removeEventListener: off("window"),
    setTimeout: (fn, ms) => { timers.push({ id: ++seq, at: clock + (ms || 0), fn }); return seq; },
    clearTimeout: (id) => { timers = timers.filter((t) => t.id !== id); },
    setInterval: () => 0, clearInterval: () => {},
    requestIdleCallback: (fn) => w.setTimeout(fn, 50),
    fetch: (url, opts) => {
      if (String(url).includes("/rest/v1/events")) { const b = JSON.parse(opts.body); sent.push({ at: (clock - t0) / 1000, type: b.type, value: b.value }); }
      return Promise.resolve({ ok: true, json: async () => [] });
    },
  };
  w.window = w;
  vm.runInNewContext(code, w, { filename: "app.js" });
  const page = {
    sent,
    /** Let `ms` pass, running whatever falls due, in order. */
    wait(ms) {
      const end = clock + ms;
      for (;;) {
        const due = timers.filter((t) => t.at <= end).sort((a, b) => a.at - b.at || a.id - b.id)[0];
        if (!due) break;
        timers = timers.filter((t) => t !== due);
        clock = Math.max(clock, due.at);
        due.fn();
      }
      clock = end;
      return page;
    },
    /** An event on the window (pointermove, scroll, keydown, pagehide...): trusted unless said otherwise. */
    fire(type, trusted = true) { (listeners.window[type] || []).slice().forEach((fn) => fn({ type, isTrusted: trusted, target: null })); return page; },
    show(visible) { state.visible = visible; (listeners.document.visibilitychange || []).concat(listeners.window.visibilitychange || []).forEach((fn) => fn({ type: "visibilitychange" })); return page; },
  };
  return page;
}

const out = {};
// A reader who opens a story and reads without touching anything: counted at ten seconds, not before.
let p = load().wait(9900);
out.quietBefore = p.sent.slice();
out.quietAfter = p.wait(200).sent.slice();
out.quietLeft = p.wait(5000).show(false).fire("pagehide").sent.slice();
// The first sign of a person counts at once, whichever it is; a made-up (untrusted) one does not.
for (const type of ["pointermove", "scroll", "touchstart", "keydown", "pointerdown"]) out[type] = load().wait(2000).fire(type).sent;
out.untrusted = load().wait(2000).fire("pointermove", false).fire("scroll", false).sent;
// Gone within ten seconds without a sign: nothing at all, not even the time on page.
out.leftEarly = load().wait(4000).show(false).fire("pagehide").wait(60000).sent;
// What was measured before the count is sent after the view, never before it.
out.heldThenCounted = load().wait(3000).show(false).wait(1000).show(true).wait(1000).fire("pointermove").sent;
// Hidden time does not count toward the ten seconds; a background tab counts from when it is shown.
out.hiddenDoesNotCount = load().wait(6000).show(false).wait(60000).sent.filter((e) => e.type === "view");
out.backgroundTab = load({ visible: false, outer: [0, 0] }).wait(30000).show(true).wait(1000).fire("scroll").sent.filter((e) => e.type === "view");
// The marks of an automated browser: each alone means nothing is ever sent, whatever happens next.
const bot = (o) => load(o).wait(1000).fire("pointermove").wait(15000).show(false).fire("pagehide").sent;
out.swiftshader = bot({ renderer: SWIFTSHADER });
out.llvmpipe = bot({ renderer: "llvmpipe (LLVM 15.0.7, 256 bits)", firefox: true });
out.noLanguages = bot({ languages: [] });
out.noOuterSize = bot({ outer: [0, 0] });
out.noScreen = bot({ screen: [0, 0] });
out.webdriver = bot({ webdriver: true });
// Not marks: WebGL switched off, and a real graphics card named the way Firefox names it.
out.webglOff = load({ renderer: null }).wait(1000).fire("pointermove").sent;
out.firefox = load({ firefox: true }).wait(1000).fire("pointermove").sent;
// The admin page and "page not found" count no view.
out.admin = load({ path: "/admin" }).wait(1000).fire("pointermove").wait(20000).sent.filter((e) => e.type === "view");
out.notFound = load({ title: "Page not found — Digest AI" }).wait(1000).fire("pointermove").sent.filter((e) => e.type === "view");
console.log(JSON.stringify(out));
