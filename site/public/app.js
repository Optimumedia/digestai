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
  const storyId = Number(document.body.dataset.storyId) || null;
  const articleId = Number(document.body.dataset.articleId) || null;
  let sid = session.get("s");
  if (!sid) { sid = Math.random().toString(36).slice(2, 12); session.set("s", sid); }
  function send(type, value, useBeacon) {
    if (!cfg.supabaseUrl || !cfg.supabaseKey) return;
    const body = JSON.stringify({ story_id: storyId, article_id: articleId, type, value: value ?? 1, session: sid, path: location.pathname, created_at: new Date().toISOString() });
    const url = `${cfg.supabaseUrl}/rest/v1/events`;
    if (useBeacon && navigator.sendBeacon) {
      navigator.sendBeacon(`${url}?apikey=${encodeURIComponent(cfg.supabaseKey)}`, new Blob([body], { type: "application/json" }));
      return;
    }
    fetch(url, { method: "POST", keepalive: true, headers: { "Content-Type": "application/json", apikey: cfg.supabaseKey, Authorization: `Bearer ${cfg.supabaseKey}`, Prefer: "return=minimal" }, body }).catch(() => {});
  }
  if (storyId) {
    send("view", 1, false);
    const start = Date.now();
    let dwellSent = false;
    const dwell = () => { if (dwellSent) return; dwellSent = true; send("dwell", Math.round((Date.now() - start) / 1000), true); };
    addEventListener("pagehide", dwell);
    addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") dwell(); });
    document.querySelectorAll("a[data-source-link]").forEach((a) => a.addEventListener("click", () => send("click_source", 1, true)));
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
    if (idx >= 0) list.splice(idx, 1); else { list.push(f); send("follow", 1, false); }
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
    else { list.unshift({ slug: btn.dataset.slug, headline: btn.dataset.headline, category: btn.dataset.category, savedAt: new Date().toISOString() }); send("save", 1, false); }
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
    send("share", 1, false);
  }));

  function escapeHtml(s) { return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
})();
