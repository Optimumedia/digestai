/* Digest AI client script: theme, live times, "new since your visit", follows, saves, shares,
   and the anonymous reader events that feed the ranking model. No cookies, no accounts. */
(() => {
  const cfg = window.DIGEST || {};
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} },
  };
  const session = {
    get(k) { try { return sessionStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { sessionStorage.setItem(k, v); } catch {} },
  };

  /* ---------- reader events ---------- */
  // Opt-out for the site's own team: visit any page with ?notrack=1 (or use the switch on
  // /admin) and this browser stops sending events; ?notrack=0 turns them back on.
  const nt = new URLSearchParams(location.search).get("notrack");
  if (nt === "1") store.set("notrack", true);
  if (nt === "0") store.set("notrack", false);
  const noTrack = store.get("notrack", false);
  const storyId = Number(document.body.dataset.storyId) || null;
  const articleId = Number(document.body.dataset.articleId) || null;
  let sid = session.get("s");
  if (!sid) { sid = Math.random().toString(36).slice(2, 12); session.set("s", sid); }
  function send(type, value) {
    if (!cfg.supabaseUrl || !cfg.supabaseKey || noTrack) return;
    const body = JSON.stringify({ story_id: storyId, article_id: articleId, type, value: value ?? 1, session: sid, path: location.pathname, created_at: new Date().toISOString() });
    // keepalive lets the request finish after the page is gone (unlike sendBeacon, it can carry
    // the JSON content type and the API headers Supabase requires).
    fetch(`${cfg.supabaseUrl}/rest/v1/events`, { method: "POST", keepalive: true, headers: { "Content-Type": "application/json", apikey: cfg.supabaseKey, Authorization: `Bearer ${cfg.supabaseKey}`, Prefer: "return=minimal" }, body }).catch(() => {});
  }
  if (storyId) {
    send("view", 1);
    // Time on story = time the tab was actually visible. Each time the page is hidden or left,
    // the seconds since it became visible are sent; the server adds them up.
    let visibleSince = document.visibilityState === "visible" ? Date.now() : null;
    const flush = () => {
      if (visibleSince == null) return;
      const secs = Math.round((Date.now() - visibleSince) / 1000);
      visibleSince = null;
      if (secs >= 1) send("dwell", Math.min(secs, 3600));
    };
    addEventListener("visibilitychange", () => {
      if (document.visibilityState === "hidden") flush();
      else if (visibleSince == null) visibleSince = Date.now();
    });
    addEventListener("pagehide", flush);
    document.querySelectorAll("a[data-source-link]").forEach((a) => a.addEventListener("click", () => send("click_source", 1)));
  }

  /* ---------- theme ---------- */
  document.getElementById("theme-toggle")?.addEventListener("click", () => {
    const root = document.documentElement;
    const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    store.set("theme", root.dataset.theme);
    document.querySelectorAll("iframe.giscus-frame").forEach((f) => f.contentWindow?.postMessage({ giscus: { setConfig: { theme: root.dataset.theme } } }, "https://giscus.app"));
  });

  /* ---------- live relative times ---------- */
  function rel(iso) {
    const diff = Math.max(0, Date.now() - Date.parse(iso));
    const m = Math.round(diff / 6e4);
    if (m < 1) return "just now";
    if (m < 60) return `${m}m ago`;
    const h = Math.round(m / 60);
    if (h < 36) return `${h}h ago`;
    const d = Math.round(h / 24);
    if (d < 14) return `${d}d ago`;
    return null;
  }
  function tick() {
    document.querySelectorAll("time[datetime]").forEach((t) => {
      if ("static" in t.dataset) return;
      const r = rel(t.getAttribute("datetime"));
      if (r) t.textContent = r;
    });
  }
  tick();
  setInterval(tick, 60000);

  /* ---------- new since your last visit ---------- */
  const now = Date.now();
  let prev = Number(session.get("visitPrev"));
  if (!prev) { prev = store.get("lastVisit", 0); session.set("visitPrev", String(prev)); }
  store.set("lastVisit", now);
  if (prev && now - prev > 10 * 60 * 1000) {
    let n = 0;
    document.querySelectorAll("[data-published]").forEach((el) => {
      if (Date.parse(el.dataset.published) > prev) {
        el.classList.add("is-new");
        n++;
      }
    });
    const badge = document.getElementById("new-count");
    if (badge && n) { badge.textContent = `${n} new since your last visit`; badge.hidden = false; }
  }

  /* ---------- follows ---------- */
  const follows = () => store.get("follows", []);
  const followKey = (f) => `${f.kind}:${f.slug}`;
  function renderFollowButtons() {
    const keys = new Set(follows().map(followKey));
    document.querySelectorAll("[data-follow]").forEach((btn) => {
      const f = JSON.parse(btn.dataset.follow);
      const on = keys.has(followKey(f));
      btn.classList.toggle("is-on", on);
      btn.setAttribute("aria-pressed", String(on));
      btn.textContent = on ? "Following" : "Follow";
    });
  }
  document.querySelectorAll("[data-follow]").forEach((btn) => btn.addEventListener("click", () => {
    const f = JSON.parse(btn.dataset.follow);
    const list = follows();
    const idx = list.findIndex((x) => followKey(x) === followKey(f));
    if (idx >= 0) list.splice(idx, 1); else { list.push(f); send("follow", 1); }
    store.set("follows", list);
    renderFollowButtons();
  }));
  renderFollowButtons();

  async function renderYourTopics() {
    const box = document.getElementById("your-topics");
    if (!box) return;
    const list = follows();
    if (!list.length) return;
    const seen = new Set();
    const items = [];
    await Promise.all(list.slice(0, 12).map(async (f) => {
      try {
        const r = await fetch(`/feeds/${f.kind}/${f.slug}.json`);
        if (!r.ok) return;
        for (const s of await r.json()) {
          if (seen.has(s.slug)) continue;
          seen.add(s.slug);
          items.push({ ...s, topic: f.name });
        }
      } catch {}
    }));
    if (!items.length) return;
    items.sort((a, b) => Date.parse(b.updatedAt) - Date.parse(a.updatedAt));
    const ul = box.querySelector("ol");
    ul.innerHTML = items.slice(0, 8).map((s) => `
      <li data-published="${s.updatedAt}">
        <div>
          <div class="meta-row"><span class="chip">${escapeHtml(s.topic)}</span><time datetime="${s.publishedAt || s.updatedAt}">${rel(s.publishedAt || s.updatedAt) || ""}</time>${s.articleCount > 1 ? `<span>${s.articleCount} sources</span>` : ""}</div>
          <h3><a href="/story/${s.slug}">${escapeHtml(s.headline)}</a></h3>
        </div>
      </li>`).join("");
    box.querySelector(".topic-names").textContent = list.map((f) => f.name).slice(0, 6).join(" · ") + (list.length > 6 ? ` +${list.length - 6}` : "");
    box.hidden = false;
    if (prev) ul.querySelectorAll("[data-published]").forEach((el) => { if (Date.parse(el.dataset.published) > prev) el.classList.add("is-new"); });
  }
  renderYourTopics();

  /* ---------- saves ---------- */
  const saves = () => store.get("saved", []);
  function renderSaveButtons() {
    const slugs = new Set(saves().map((s) => s.slug));
    document.querySelectorAll("[data-save]").forEach((btn) => {
      const on = slugs.has(btn.dataset.slug);
      btn.classList.toggle("is-on", on);
      btn.setAttribute("aria-pressed", String(on));
      btn.querySelector("span").textContent = on ? "Saved" : "Save";
    });
    const count = document.getElementById("saved-count");
    if (count) { const n = saves().length; count.textContent = n ? String(n) : ""; count.hidden = !n; }
  }
  document.querySelectorAll("[data-save]").forEach((btn) => btn.addEventListener("click", () => {
    const list = saves();
    const idx = list.findIndex((s) => s.slug === btn.dataset.slug);
    if (idx >= 0) list.splice(idx, 1);
    else { list.unshift({ slug: btn.dataset.slug, headline: btn.dataset.headline, category: btn.dataset.category, savedAt: new Date().toISOString() }); send("save", 1); }
    store.set("saved", list);
    renderSaveButtons();
  }));
  renderSaveButtons();

  const savedList = document.getElementById("saved-list");
  if (savedList) {
    const list = saves();
    const empty = document.getElementById("saved-empty");
    if (list.length) {
      savedList.innerHTML = list.map((s) => `
        <li class="no-img">
          <div>
            <div class="meta-row"><span class="chip">${escapeHtml(s.category || "AI")}</span><time datetime="${s.savedAt}" data-static>saved ${rel(s.savedAt) || "earlier"}</time></div>
            <h3><a href="/story/${s.slug}">${escapeHtml(s.headline)}</a></h3>
            <button class="linkish" data-unsave="${s.slug}">Remove</button>
          </div>
        </li>`).join("");
      savedList.querySelectorAll("[data-unsave]").forEach((b) => b.addEventListener("click", () => {
        store.set("saved", saves().filter((s) => s.slug !== b.dataset.unsave));
        location.reload();
      }));
      if (empty) empty.hidden = true;
    }
  }

  /* ---------- share ---------- */
  document.querySelectorAll("[data-share]").forEach((btn) => btn.addEventListener("click", async () => {
    const url = location.href.split("#")[0];
    const title = document.title.replace(/ — Digest AI$/, "");
    const text = encodeURIComponent(title);
    const u = encodeURIComponent(url);
    const targets = {
      x: `https://x.com/intent/post?text=${text}&url=${u}`,
      bluesky: `https://bsky.app/intent/compose?text=${text}%20${u}`,
      linkedin: `https://www.linkedin.com/sharing/share-offsite/?url=${u}`,
      whatsapp: `https://wa.me/?text=${text}%20${u}`,
      hn: `https://news.ycombinator.com/submitlink?u=${u}&t=${text}`,
    };
    const kind = btn.dataset.share;
    if (kind === "copy") {
      try { await navigator.clipboard.writeText(url); btn.textContent = "Copied"; setTimeout(() => (btn.textContent = "Copy link"), 1500); } catch {}
    } else if (kind === "native" && navigator.share) {
      try { await navigator.share({ title, url }); } catch { return; }
    } else if (targets[kind]) {
      open(targets[kind], "_blank", "noopener,width=640,height=560");
    }
    send("share", 1);
  }));

  /* ---------- audio ---------- */
  document.querySelectorAll("audio[data-episode]").forEach((a) => a.addEventListener("play", () => send("listen", 1), { once: true }));

  /* ---------- offline + push alerts ---------- */
  // The service worker keeps recently read pages available offline and shows push alerts.
  if ("serviceWorker" in navigator && (location.protocol === "https:" || /^(localhost|127.0.0.1)$/.test(location.hostname))) {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  }
  const pushOk = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window && cfg.vapidKey && cfg.supabaseUrl && cfg.supabaseKey;
  function b64ToBytes(b64) {
    const pad = "=".repeat((4 - (b64.length % 4)) % 4);
    const raw = atob((b64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
    return Uint8Array.from(raw, (c) => c.charCodeAt(0));
  }
  async function pushState() {
    if (!pushOk) return "unsupported";
    if (Notification.permission === "denied") return "denied";
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.getSubscription();
    return sub ? "on" : "off";
  }
  async function pushOn() {
    const perm = await Notification.requestPermission();
    if (perm !== "granted") return "denied";
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToBytes(cfg.vapidKey) });
    const j = sub.toJSON();
    const body = JSON.stringify({ endpoint: j.endpoint, p256dh: j.keys.p256dh, auth: j.keys.auth, topics: JSON.stringify((store.get("follows", []) || []).map((f) => f.slug).slice(0, 50)) });
    const res = await fetch(`${cfg.supabaseUrl}/rest/v1/push_subscriptions`, { method: "POST", headers: { "Content-Type": "application/json", apikey: cfg.supabaseKey, Authorization: `Bearer ${cfg.supabaseKey}`, Prefer: "return=minimal" }, body });
    if (!res.ok && res.status !== 409) { await sub.unsubscribe().catch(() => {}); return "error"; }
    store.set("push", true);
    send("push_on", 1);
    return "on";
  }
  async function pushOff() {
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.getSubscription();
    if (sub) await sub.unsubscribe().catch(() => {});
    store.set("push", false);
    return "off";
  }
  const LABELS = { on: "Alerts on", off: "Turn on alerts", denied: "Alerts blocked in browser settings", error: "Could not turn on alerts, try again", unsupported: "" };
  async function paintPush(state) {
    document.querySelectorAll("[data-push-box]").forEach((el) => { el.hidden = state === "unsupported"; });
    document.querySelectorAll("[data-push]").forEach((b) => {
      b.textContent = LABELS[state] || LABELS.off;
      b.setAttribute("aria-pressed", state === "on" ? "true" : "false");
      b.disabled = state === "denied";
    });
    document.querySelectorAll("[data-push-status]").forEach((el) => {
      el.textContent = state === "on" ? "This browser will get breaking-news alerts, at most three a day." : state === "denied" ? "Notifications are blocked for this site; allow them in the browser's site settings to turn alerts on." : "";
    });
  }
  if (document.querySelector("[data-push]")) {
    pushState().then(paintPush);
    document.querySelectorAll("[data-push]").forEach((b) => b.addEventListener("click", async () => {
      b.disabled = true;
      const state = await pushState();
      const next = state === "on" ? await pushOff() : await pushOn().catch(() => "error");
      await paintPush(next);
      b.disabled = next === "denied";
    }));
  }
  // Soft ask: after the third story read, offer alerts once in a small bar (dismiss = 30 days).
  if (storyId && pushOk && Notification.permission === "default" && !store.get("push", false)) {
    const reads = (store.get("reads", 0) || 0) + 1;
    store.set("reads", reads);
    const snoozed = store.get("pushSnooze", 0) || 0;
    if (reads >= 3 && Date.now() > snoozed && !document.querySelector("[data-push]")) {
      const bar = document.createElement("aside");
      bar.className = "pushbar";
      bar.innerHTML = `<span>Get an alert when AI news breaks, at most three a day.</span><button class="follow" data-push aria-pressed="false">Turn on alerts</button><button class="icon-btn" data-push-close aria-label="Not now">×</button>`;
      document.querySelector("main")?.prepend(bar);
      bar.querySelector("[data-push-close]").addEventListener("click", () => { store.set("pushSnooze", Date.now() + 30 * 864e5); bar.remove(); });
      bar.querySelector("[data-push]").addEventListener("click", async (e) => {
        e.target.disabled = true;
        const next = await pushOn().catch(() => "error");
        if (next === "on") { bar.innerHTML = "<span>Alerts on. Manage them any time on the <a href='/subscribe'>subscribe page</a>.</span>"; setTimeout(() => bar.remove(), 6000); }
        else { e.target.disabled = false; e.target.textContent = LABELS[next] || LABELS.off; }
      });
    }
  }

  function escapeHtml(s) { return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
})();
