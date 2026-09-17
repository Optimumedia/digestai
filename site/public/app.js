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
  // Search engines and link previewers render pages with JavaScript too (Googlebot, Bingbot,
  // card fetchers, automated browsers). They are not readers, so they send nothing.
  const isBot = navigator.webdriver === true || /bot\b|bot\/|crawl|spider|slurp|headless|lighthouse|pagespeed|google-inspectiontool|googleother|mediapartners|bingpreview|facebookexternalhit|embedly|whatsapp|pinterest|vkshare|w3c_validator|yandex|baidu|petalsearch|semrush|ahrefs|mj12|dataforseo|bytespider|ccbot|amazonbot/i.test(navigator.userAgent || "");
  const noTrack = store.get("notrack", false) || isBot;
  const storyId = Number(document.body.dataset.storyId) || null;
  const articleId = Number(document.body.dataset.articleId) || null;
  let sid = session.get("s");
  if (!sid) { sid = Math.random().toString(36).slice(2, 12); session.set("s", sid); }
  // Where this visit came from, once per tab: a utm_source tag, else the referring site, else "direct".
  let visitSource = session.get("src");
  if (!visitSource) {
    let host = "";
    try { host = document.referrer ? new URL(document.referrer).hostname.replace(/^www[.]/, "") : ""; } catch {}
    visitSource = new URLSearchParams(location.search).get("utm_source") || (host && host !== location.hostname ? host : "direct");
    session.set("src", visitSource);
  }
  // One anonymous visitor number per browser per UTC day, so a reader with several tabs counts once.
  // It is random, never leaves this browser except with the events, and is replaced every day.
  const visitDay = new Date().toISOString().slice(0, 10);
  // The time zone the device is set to (Europe/Warsaw): enough to tell the country, never the city.
  let visitZone = null;
  try { visitZone = String(Intl.DateTimeFormat().resolvedOptions().timeZone || "").slice(0, 40) || null; } catch {}
  let visitor = store.get("visitor", null);
  if (!visitor || visitor.day !== visitDay || typeof visitor.id !== "string") {
    visitor = { id: Math.random().toString(36).slice(2, 12) + Math.random().toString(36).slice(2, 8), day: visitDay };
    store.set("visitor", visitor);
  }
  // extra: { detail } for searches (the query) and read depth (how far), { path } to file it under another page.
  function send(type, value, extra) {
    if (!cfg.supabaseUrl || !cfg.supabaseKey || noTrack) return;
    const ev = { story_id: storyId, article_id: articleId, type, value: value ?? 1, session: sid, visitor: visitor.id, source: String(visitSource).slice(0, 60), tz: visitZone, path: (extra && extra.path) || location.pathname, created_at: new Date().toISOString() };
    if (extra && extra.detail) ev.detail = String(extra.detail).slice(0, 100);
    const body = JSON.stringify(ev);
    // keepalive lets the request finish after the page is gone (unlike sendBeacon, it can carry
    // the JSON content type and the API headers Supabase requires).
    fetch(`${cfg.supabaseUrl}/rest/v1/events`, { method: "POST", keepalive: true, headers: { "Content-Type": "application/json", apikey: cfg.supabaseKey, Authorization: `Bearer ${cfg.supabaseKey}`, Prefer: "return=minimal" }, body }).catch(() => {});
  }
  // Every page counts as a view (the admin page does not); time on page and source clicks are story-only.
  // Not the admin page, and not "page not found": old addresses from the previous site are mostly
  // crawlers checking links that no longer exist.
  if (!location.pathname.startsWith("/admin") && !document.title.startsWith("Page not found")) send("view", 1);
  if (storyId) {
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

    // How far the story was read: the deepest point that reached the bottom of the screen, as a
    // percent of the story (headline to the end of the full text, or of the summary when there is
    // none) and as a stage: top, summary (reached the source box below the key points and digest),
    // full_text (read into the publisher's text) or end. A glance does not count: nothing is
    // credited before the page has been visible for 4 seconds, and a page that fits the screen
    // counts as read to the end only after 15 seconds or a scroll. Sent when the page is hidden or
    // left, and again only if the reader later gets to a further stage (at most four per page view).
    const STAGES = ["top", "summary", "full_text", "end"];
    const sumEl = document.querySelector('[data-read="summary"]');
    const fullEl = document.querySelector('[data-read="full_text"]');
    const endEl = fullEl || sumEl;
    const art = endEl && endEl.closest("article");
    if (art && endEl) {
      let shownMs = 0, shownSince = document.visibilityState === "visible" ? Date.now() : null;
      let scrolled = false, rank = 0, pct = 0, sentRank = -1, queued = false;
      const visibleMs = () => shownMs + (shownSince != null ? Date.now() - shownSince : 0);
      const docY = (el, edge) => el.getBoundingClientRect()[edge] + scrollY;
      const sample = () => {
        queued = false;
        const seen = visibleMs();
        if (seen < 4000) return;
        const bottom = scrollY + innerHeight;
        const start = docY(art, "top"), end = docY(endEl, "bottom");
        let r = 0;
        if (bottom >= docY(sumEl || endEl, "top")) r = 1;
        if (fullEl) {
          const top = docY(fullEl, "top");
          if (bottom >= top + Math.min(600, (end - top) / 4)) r = 2;
        }
        if (bottom >= end - 40) r = 3;
        let p = end > start ? ((bottom - start) / (end - start)) * 100 : 100;
        if (!scrolled && seen < 15000) { r = Math.min(r, 2); p = Math.min(p, 90); }
        rank = Math.max(rank, r);
        pct = Math.max(pct, Math.min(100, Math.max(0, p)));
      };
      addEventListener("scroll", () => {
        scrolled = true;
        if (!queued) { queued = true; requestAnimationFrame(sample); }
      }, { passive: true });
      setInterval(() => { if (document.visibilityState === "visible") sample(); }, 2000);
      const report = () => {
        sample();
        if (shownSince != null) { shownMs += Date.now() - shownSince; shownSince = null; }
        if (rank <= sentRank) return;
        sentRank = rank;
        send("depth", Math.round(pct), { detail: STAGES[rank] });
      };
      addEventListener("visibilitychange", () => {
        if (document.visibilityState === "hidden") report();
        else if (shownSince == null) shownSince = Date.now();
      });
      addEventListener("pagehide", report);
    }
  }

  /* ---------- site searches ---------- */
  // The search page and the not-found page announce each term Pagefind looks up ("digest:search").
  // Once the reader stops typing for 1.5 seconds, or leaves, the query is sent once with its number
  // of results: what people look for, and what the site has no story on. Queries are lowercased and
  // lose e-mail addresses and long numbers; the not-found page's own guess from the old address
  // (auto) is not sent.
  {
    const cleanQuery = (t) => String(t || "").toLowerCase().replace(/\S+@\S+/g, " ").replace(/\d{5,}/g, " ").replace(/\s+/g, " ").trim().slice(0, 100);
    let pending = null, timer = null, lastSent = "", pagefind = null;
    const flushSearch = () => {
      const p = pending;
      if (!p) return;
      if (p.results == null) { p.due = true; return; }  // sent as soon as the count arrives
      clearTimeout(timer); pending = null;
      if (p.query === lastSent) return;
      lastSent = p.query;
      send("search", Math.min(p.results, 3600), { detail: p.query, path: location.pathname.startsWith("/search") ? "/search" : "/404" });
      // Google Analytics exists only after the reader accepted it (components/Analytics.astro).
      if (typeof window.gtag === "function") window.gtag("event", "search", { search_term: p.query });
    };
    addEventListener("digest:search", (e) => {
      const { term, auto } = e.detail || {};
      const query = cleanQuery(term);
      clearTimeout(timer);
      pending = null;
      if (auto || query.length < 2) return;
      const p = (pending = { query, results: null, due: false });
      timer = setTimeout(flushSearch, 1500);
      // The same module Pagefind UI loaded, so this reuses its index instead of downloading it again.
      (pagefind ||= import("/pagefind/pagefind.js"))
        .then((pf) => pf.search(term))
        .then((r) => { p.results = r && r.results ? r.results.length : 0; if (p.due && pending === p) flushSearch(); })
        .catch(() => {});
    });
    addEventListener("pagehide", flushSearch);
    addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") flushSearch(); });
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
    // Share the canonical address: no ".html", no tracking parameters from the visit.
    const url = (document.querySelector('link[rel="canonical"]')?.href || location.href).split("#")[0];
    const title = document.title.replace(/ — Digest AI$/, "");
    const text = encodeURIComponent(title);
    const u = encodeURIComponent(url);
    const targets = {
      x: `https://x.com/intent/post?text=${text}&url=${u}&via=DigestAINews`,
      bluesky: `https://bsky.app/intent/compose?text=${text}%20${u}%20via%20%40digestai.bsky.social`,
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

  /* ---------- live presence: who is on the site right now (read by the admin page) ---------- */
  // Anonymous and not stored: the page, its title, the traffic source and whether it is a phone.
  // Connected only while the tab is visible, so the free plan's concurrent-connection cap is respected.
  (() => {
    if (!cfg.supabaseUrl || !cfg.supabaseKey || noTrack || !("WebSocket" in window) || location.pathname.startsWith("/admin")) return;
    const src = visitSource;
    const topic = "realtime:live";
    const meta = { path: location.pathname, title: document.title.replace(" — Digest AI", "").slice(0, 90), src, mobile: matchMedia("(max-width: 640px)").matches, at: Date.now() };
    let ws = null, hb = null, ref = 0, hideTimer = null, retries = 0;
    const send = (t, event, payload) => { if (ws && ws.readyState === 1) ws.send(JSON.stringify({ topic: t, event, payload, ref: String(++ref) })); };
    const connect = () => {
      if (ws || document.visibilityState !== "visible") return;
      const sock = new WebSocket(`${cfg.supabaseUrl.replace(/^http/, "ws")}/realtime/v1/websocket?apikey=${encodeURIComponent(cfg.supabaseKey)}&vsn=1.0.0`);
      ws = sock;
      sock.onopen = () => {
        retries = 0;
        send(topic, "phx_join", { config: { broadcast: { self: false }, presence: { key: sid }, postgres_changes: [] }, access_token: cfg.supabaseKey });
        send(topic, "presence", { type: "presence", event: "track", payload: meta });
        hb = setInterval(() => send("phoenix", "heartbeat", {}), 25000);
      };
      sock.onclose = () => {
        if (ws !== sock) return;  // closed on purpose
        clearInterval(hb); ws = null;
        if (document.visibilityState === "visible" && retries++ < 5) setTimeout(connect, 3000 * retries);
      };
      sock.onerror = () => {};
    };
    const disconnect = () => {
      if (!ws) return;
      const sock = ws; ws = null; clearInterval(hb);
      try { sock.close(); } catch {}
    };
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") { clearTimeout(hideTimer); connect(); }
      else hideTimer = setTimeout(disconnect, 60000);
    });
    addEventListener("pagehide", disconnect);
    connect();
  })();

  /* ---------- audio briefing player ([data-listen]) ---------- */
  const clock = (sec) => `${Math.floor(sec / 60)}:${String(Math.floor(sec % 60)).padStart(2, "0")}`;
  const SPEEDS = [1, 1.25, 1.5, 1.75, 0.75];
  document.querySelectorAll("[data-listen]").forEach((box) => {
    const audio = box.querySelector("audio");
    const btn = box.querySelector(".listen-play");
    const bar = box.querySelector(".listen-bar");
    const fill = box.querySelector(".listen-fill");
    const cur = box.querySelector("[data-current]");
    const speedBtn = box.querySelector(".listen-speed");
    const total = Number(box.dataset.duration) || 0;
    if (!audio || !btn) return;
    let speed = Number(store.get("listenSpeed", 1)) || 1;
    const length = () => (Number.isFinite(audio.duration) && audio.duration > 0 ? audio.duration : total);
    const paint = () => {
      const d = length(), t = audio.currentTime || 0;
      if (fill) fill.style.width = d ? `${Math.min(100, (t / d) * 100)}%` : "0";
      if (cur) cur.textContent = clock(t);
      bar?.setAttribute("aria-valuenow", String(Math.round(t)));
    };
    const setSpeed = (v) => {
      speed = v; audio.playbackRate = v; store.set("listenSpeed", v);
      if (speedBtn) speedBtn.textContent = `${v}×`;
    };
    setSpeed(speed);
    const seekTo = (seconds) => {
      const apply = () => { audio.currentTime = Math.max(0, Math.min(length(), seconds)); paint(); };
      if (audio.readyState >= 1) apply();
      else { audio.addEventListener("loadedmetadata", apply, { once: true }); audio.preload = "metadata"; audio.load(); }
    };
    btn.addEventListener("click", () => {
      if (audio.paused) {
        document.querySelectorAll("[data-listen] audio").forEach((other) => { if (other !== audio) other.pause(); });
        audio.play().catch(() => {});
      } else audio.pause();
    });
    audio.addEventListener("play", () => {
      box.classList.add("is-playing");
      btn.setAttribute("aria-pressed", "true");
      btn.setAttribute("aria-label", "Pause the audio briefing");
      audio.playbackRate = speed;
      if ("mediaSession" in navigator) {
        try {
          navigator.mediaSession.metadata = new MediaMetadata({ title: box.dataset.title || "Digest AI briefing", artist: "Digest AI", album: "Daily AI briefing", artwork: [{ src: "/logo-512.png", sizes: "512x512", type: "image/png" }] });
          navigator.mediaSession.setActionHandler("play", () => audio.play());
          navigator.mediaSession.setActionHandler("pause", () => audio.pause());
          navigator.mediaSession.setActionHandler("seekbackward", () => seekTo(audio.currentTime - 15));
          navigator.mediaSession.setActionHandler("seekforward", () => seekTo(audio.currentTime + 15));
        } catch {}
      }
    });
    audio.addEventListener("pause", () => {
      box.classList.remove("is-playing");
      btn.setAttribute("aria-pressed", "false");
      btn.setAttribute("aria-label", "Play the audio briefing");
    });
    audio.addEventListener("timeupdate", paint);
    audio.addEventListener("ended", () => { audio.currentTime = 0; paint(); });
    bar?.addEventListener("click", (e) => {
      const r = bar.getBoundingClientRect();
      seekTo(((e.clientX - r.left) / r.width) * length());
    });
    bar?.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
      e.preventDefault();
      seekTo((audio.currentTime || 0) + (e.key === "ArrowRight" ? 10 : -10));
    });
    speedBtn?.addEventListener("click", () => setSpeed(SPEEDS[(SPEEDS.indexOf(speed) + 1) % SPEEDS.length]));
  });

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
