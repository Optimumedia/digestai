"""Step: export published stories, the daily briefing and topic data as JSON for the site build."""
from __future__ import annotations

import json
import logging
from datetime import timedelta
from pathlib import Path

import yaml
from sqlalchemy import select, update

from . import archive, cache, checks, config, db, funding as funding_rules, hold, primary, trackers as tracker_rules, work as work_rules
from . import prices as price_rules, work_learn
from .enrich import headline_hedged
from .textutil import word_count

log = logging.getLogger("digest.export")

BRIEFING_SIZE = 5
BRIEFING_ALSO = 8
CONFIRMED_REACH = 5  # how far below the top five a confirmed story may be and still replace a single-outlet one


def _work_dropped(dropped: dict[str, dict[str, int]], carded: set[int]) -> dict[str, dict]:
    """Cards the rules kept out of AI at Work, per reason: how many stories lost their only card, and
    up to eight of the tools. A story still carded through another of its articles does not count."""
    out = {}
    for why, tools in dropped.items():
        kept = {name: sid for name, sid in tools.items() if sid not in carded}
        if kept:
            out[why] = {"count": len(set(kept.values())), "tools": sorted(kept)[:8]}
    return out


def _coverage(articles: list[dict]) -> dict:
    cov = {"primary": 0, "press": 0, "newsletter": 0, "community": 0}
    for a in articles:
        cov[a["sourceType"]] = cov.get(a["sourceType"], 0) + 1
    return cov


def _source_type(src: dict, domain: str | None) -> str:
    """Community feeds point at other publishers, so their finds count as press; a primary source is
    one the story is *about* (the lab's own post counts even when it arrived via HN)."""
    if domain in config.PRIMARY_DOMAINS:
        return "primary"
    stype = src.get("type") or "press"
    return "press" if stype == "community" or src.get("discovered") else stype


def story_redirects(stories: dict[int, tuple], titles: dict[int, tuple], exported: dict[int, str]) -> list[dict]:
    """Old address -> current address for stories folded into another one (merge.py), following a
    chain of merges, only where the surviving story is on the site. stories: the mirror rows;
    titles: slug and headline of the merged ones; exported: id -> slug of the stories written out."""
    out = []
    for s in stories.values():
        if s.status != "merged" or not s.redirect_to or s.id not in titles:
            continue
        target, hops = s.redirect_to, 0
        while target in stories and stories[target].status == "merged" and stories[target].redirect_to and hops < 10:
            target, hops = stories[target].redirect_to, hops + 1
        if target in exported and titles[s.id].slug != exported[target]:
            out.append({"from": titles[s.id].slug, "to": exported[target]})
    return sorted(out, key=lambda r: r["from"])


def add_entity(index: dict[str, dict], name: str, kind: str, story_id: int) -> None:
    """One topic per name whatever the stories' spelling: "Nvidia" and "NVIDIA" are one hub, named
    the way most stories write it (they used to be two rail entries with one address)."""
    key = str(name).strip()
    if not key:
        return
    ent = index.setdefault(key.lower(), {"name": key, "kind": kind, "storyIds": [], "spellings": {}})
    ent["storyIds"].append(story_id)
    ent["spellings"][key] = ent["spellings"].get(key, 0) + 1


def entity_out(ent: dict) -> dict:
    spellings = ent.get("spellings") or {ent["name"]: 1}
    name = max(spellings, key=lambda k: (spellings[k], k == ent["name"]))
    return {"name": name, "kind": ent["kind"], "storyIds": ent["storyIds"]}


def confirmed(s: dict) -> bool:
    """Reported by two or more publishers, or announced by the lab or company itself. A single
    outlet's story can be in the briefing, but never above a confirmed one (a pin is the owner's call).
    A paper is not an announcement: an arXiv preprint, a journal article, a repository or a government
    PDF is one source like any other (primary.py), so it confirms nothing on its own."""
    publishers = {hold.registrable(a.get("domain") or "") for a in s.get("articles") or []} - {""}  # news.x.com and x.com are one
    return bool(s.get("pinned") or primary.announced(s) or len(publishers) >= 2)


PAPER_LEAD_PLACE = 3  # the highest place (1-based) a research story may take unless it is pinned


def demote_papers(pool: list[dict], size: int = BRIEFING_SIZE, focus: str | None = None) -> list[dict]:
    """A research story (the research category, or a paper as its only primary source) can be in the
    briefing but not lead it: the first PAPER_LEAD_PLACE - 1 places go to the best other stories, in
    their order. They come from the top `size` first; only when those are nearly all papers is one
    brought up from further down, and then the lowest story of the top that is not the focus
    category's gives way."""
    lead_ok = lambda s: s.get("pinned") or not primary.research_story(s)  # noqa: E731
    need = PAPER_LEAD_PLACE - 1
    head = pool[:size]
    if all(lead_ok(s) for s in head[:need]):
        return pool
    front = [s for s in head if lead_ok(s)][:need]
    front += [s for s in pool[size:] if lead_ok(s)][: need - len(front)]
    new_head = front + [s for s in head if s not in front]
    while len(new_head) > size:
        drop = next((s for s in reversed(new_head[need:]) if s.get("category") != focus), new_head[-1])
        new_head.remove(drop)
    return new_head + [s for s in pool if s not in new_head]


def build_briefing(stories: list[dict], now) -> dict:
    """Today's top stories: first new ones, then developing ones only to fill empty places.

    New means first published in the last 24 hours (48 or 96 on a quiet day). A developing
    story is older but had at least two articles published inside the window; it only enters
    when there are not enough new stories, so the briefing always leads with fresh news."""

    def new_story(s: dict, cutoff: str) -> bool:
        return bool(s["pinned"] or (s["firstPublishedAt"] or "") >= cutoff)

    def developing(s: dict, cutoff: str) -> bool:
        return sum(1 for a in s["articles"] if (a["publishedAt"] or "") >= cutoff) >= 2

    rank = lambda s: (not s["pinned"], -s["score"])  # noqa: E731

    for window in (24, 48, 96):
        cutoff = db.iso_z(now - timedelta(hours=window))
        fresh = sorted((s for s in stories if new_story(s, cutoff)), key=rank)
        if len(fresh) >= BRIEFING_SIZE + BRIEFING_ALSO or window == 96:
            break
    older = sorted((s for s in stories if not new_story(s, cutoff) and developing(s, cutoff)), key=rank)
    pool = fresh + older
    # A focus category (config.BRIEFING_FOCUS_CATEGORY) always has a place when it has a fresh story:
    # the best one takes the last place if none made the top on score alone.
    focus = config.BRIEFING_FOCUS_CATEGORY
    if focus and not any(s.get("category") == focus for s in pool[:BRIEFING_SIZE]):
        pick = next((s for s in fresh if s.get("category") == focus), None)
        if pick and len(pool) >= BRIEFING_SIZE:
            pool = [s for s in pool if s is not pick]
            pool.insert(BRIEFING_SIZE - 1, pick)
    # Confirmed stories lead. The five are the five best by score as before; inside them the confirmed
    # ones come first, and a single-outlet story loses its place to a confirmed one ranked just below
    # it (within CONFIRMED_REACH places), so the first thing a visitor reads is never one outlet's word
    # while a story half the press is covering sits underneath.
    reach = pool[: BRIEFING_SIZE + CONFIRMED_REACH]
    solid = [s for s in reach if confirmed(s)]
    if solid:
        rest = [s for s in reach if not confirmed(s)]
        head = (solid + rest)[:BRIEFING_SIZE]
        if focus and any(s.get("category") == focus for s in pool[:BRIEFING_SIZE]) and not any(s.get("category") == focus for s in head):
            keep = next(s for s in pool[:BRIEFING_SIZE] if s.get("category") == focus)
            head = head[: BRIEFING_SIZE - 1] + [keep]  # the focus category keeps its place
        pool = head + [s for s in pool if s not in head]
    # A paper never leads: research stories start at PAPER_LEAD_PLACE (a pin still decides).
    pool = demote_papers(pool, BRIEFING_SIZE, focus)
    top = pool[:BRIEFING_SIZE]
    also = pool[BRIEFING_SIZE : BRIEFING_SIZE + BRIEFING_ALSO]
    words = sum(word_count(s.get("summaryMd") or "") for s in top) + 25 * len(also)
    return {
        "date": now.date().isoformat(),
        "generatedAt": db.iso_z(now),
        "windowHours": window,
        "storyIds": [s["id"] for s in top],
        "alsoIds": [s["id"] for s in also],
        "stats": {
            "stories": len(pool),
            "articles": sum(s["articleCount"] for s in pool),
            "minutes": max(3, round(words / 230)),
        },
    }


def load_moderation() -> dict:
    path = Path(__file__).with_name("moderation.yaml")
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def apply_moderation(eng, rules: dict | None = None) -> dict:
    """moderation.yaml is the unpublish button when there is no database console."""
    rules = load_moderation() if rules is None else rules
    if not rules:
        return {}
    unpublish = [s.strip() for s in rules.get("unpublish") or [] if s]
    pin = [s.strip() for s in rules.get("pin") or [] if s]
    domains = [d.strip().lower() for d in rules.get("block_domains") or [] if d]
    words = [w.strip().lower() for w in rules.get("block_title_words") or [] if w]
    stats = {"unpublished": 0, "pinned": 0}
    with eng.begin() as conn:
        if unpublish:
            res = conn.execute(update(db.stories).where(db.stories.c.slug.in_(unpublish), db.stories.c.status.in_(["published", hold.HELD]))
                               .values(status="unpublished"))
            stats["unpublished"] = res.rowcount
        conn.execute(update(db.stories).where(db.stories.c.pinned.is_(True), ~db.stories.c.slug.in_(pin or ["-"])).values(pinned=False))
        if pin:
            res = conn.execute(update(db.stories).where(db.stories.c.slug.in_(pin)).values(pinned=True))
            stats["pinned"] = res.rowcount
        if domains:
            conn.execute(update(db.articles).where(db.articles.c.domain.in_(domains), db.articles.c.status == "published")
                         .values(status="unpublished", reject_reason="moderation: domain"))
        for w in words:
            conn.execute(update(db.stories).where(db.stories.c.headline.ilike(f"%{w}%"), db.stories.c.status.in_(["published", hold.HELD]))
                         .values(status="unpublished"))
    return stats


def _full_text(conn, story_rows, art_rows) -> dict[int, str]:
    """Article text for the story pages: the lead article's when it may be shown, otherwise the newest
    article that has showable text (what the page's fullTextArticle picks). Reading the text of every
    article, most of which no page shows, was most of the database's monthly transfer allowance; now
    only that one article's text is read, and only when the runner's copy lacks it (cache.py)."""
    leads = {s.id: s.lead_article_id for s in story_rows}
    by_story: dict[int, list] = {}
    for a in art_rows:  # newest first, as the page lists them
        by_story.setdefault(a.story_id, []).append(a)
    has_text = lambda m: (m.len_content_md or 0) > 0  # noqa: E731
    pick = []
    for sid, members in by_story.items():
        lead = next((m for m in members if m.id == leads.get(sid)), members[0])
        if lead.show_fulltext and (lead.word_count or 0) > 0 and has_text(lead):
            pick.append(lead)
            continue
        # The lead has no text to show: the newest other article that has some.
        other = next((m for m in members if m.show_fulltext and m.id != lead.id and has_text(m)), None)
        if other is not None:
            pick.append(other)
    return cache.article_bodies(conn, pick)


def load_rows(conn, since, stories: dict | None = None, articles: dict | None = None) -> tuple[list, list, dict[int, str], list]:
    """Published stories updated since `since` (newest first) and their published articles (newest
    first), as one object per row with the columns the export uses, plus the text each story page
    shows, plus the overflow members (coverage of a full story, cluster.py: counted, never shown,
    so their text is not read). Read through the runner's copy (cache.py): only rows written since
    the previous run come from the database, so a run with a few new stories reads kilobytes, not
    megabytes. `stories` and `articles` are the mirrors when the caller already holds them."""
    stories = cache.stories(conn) if stories is None else stories
    articles = cache.articles(conn) if articles is None else articles
    story_mirror = [s for s in stories.values()
                    if s.status == "published" and (db.as_utc(s.updated_at) or since) >= since]
    # Postgres order for "updated_at desc": newest first; ties by id so the order is stable.
    story_mirror.sort(key=lambda s: (db.as_utc(s.updated_at).timestamp(), s.id), reverse=True)
    ids = {s.id for s in story_mirror}
    members = [a for a in articles.values() if a.status in ("published", "overflow") and a.story_id in ids]
    art_mirror = [a for a in members if a.status == "published"]
    # "published_at desc" as Postgres sorts it: articles without a date first, then newest first.
    art_mirror.sort(key=lambda a: (a.published_at is None, db.as_utc(a.published_at).timestamp() if a.published_at else 0, a.id),
                    reverse=True)
    s_text = cache.story_text(conn, story_mirror)
    a_text = cache.article_text(conn, art_mirror)
    story_rows = [cache.merged(s, s_text.get(s.id)) for s in story_mirror if s.id in s_text]
    art_rows = [cache.merged(a, a_text.get(a.id)) for a in art_mirror if a.id in a_text]
    overflow = sorted((a for a in members if a.status == "overflow"), key=lambda a: a.id)
    return story_rows, art_rows, _full_text(conn, story_mirror, art_mirror), overflow


def learning_summary(learned: dict, stories: list[dict], section: list[dict], briefing: dict, now) -> dict:
    """What reader data did to AI at Work this run, for the admin page and the morning note: what
    it taught (work_learn.taught, over the last week's cards, as the home page shows them) and what
    it changed at the top of the section briefing against the rules order alone."""
    rules = lambda s: s["workCard"].get("rulesUsefulness", s["workCard"]["usefulness"])  # noqa: E731
    cutoff = db.iso_z(now - timedelta(days=work_learn.MOVERS_DAYS))
    week = [s for s in section if (s.get("firstPublishedAt") or "") >= cutoff]
    by_rules = [s["id"] for s in sorted(week, key=lambda s: (-rules(s), s.get("firstPublishedAt") or ""))]
    by_blend = [s["id"] for s in sorted(week, key=lambda s: (-s["workCard"]["usefulness"], s.get("firstPublishedAt") or ""))]
    out = work_learn.taught(learned, week, by_rules, by_blend)
    out["changed"] = None
    if learned["active"]:
        rules_briefing = work_rules.build_briefing(stories, now, score=rules)
        out["changed"] = work_learn.changed(rules_briefing, briefing, stories)
    return out


def run() -> dict:
    eng = db.engine()
    out_dir = config.SITE_DATA_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    now = db.utcnow()
    since = now - timedelta(days=config.EXPORT_DAYS)
    rules = load_moderation()
    moderation = apply_moderation(eng, rules)
    # Risky single-source stories get status "held" (and are released again when a second publisher
    # or an `approve` entry arrives) before the query below, which exports only "published" ones.
    if config.HOLD_RISKY_CLAIMS:
        held, moderation["hold"] = hold.review(eng, since, [s for s in rules.get("approve") or [] if s])
        moderation["hold"].update(hold.write_review(out_dir, held, db.iso_z(now)))
    else:
        # Hold switched off: publish anything still held and drop the review files, so the admin page
        # shows no review card.
        with eng.begin() as conn:
            released = conn.execute(update(db.stories).where(db.stories.c.status == hold.HELD).values(status="published")).rowcount
        for name in ("held.json", "held.enc.json"):
            (out_dir / name).unlink(missing_ok=True)
        moderation["hold"] = {"enabled": False, "released": released or 0}

    with eng.connect() as conn:
        src_rows = conn.execute(select(db.sources.c.id, db.sources.c.key, db.sources.c.name, db.sources.c.url, db.sources.c.kind,
                                       db.sources.c.source_type, db.sources.c.weight, db.sources.c.discovered,
                                       db.sources.c.enabled)).all()
        sources = {
            s.id: {"key": s.key, "name": s.name, "url": s.url, "kind": s.kind, "type": s.source_type,
                   "weight": s.weight, "discovered": s.discovered, "enabled": s.enabled}
            for s in src_rows
        }
        # Supabase's free plan counts every byte read (5 GB a month): embeddings are never exported,
        # article text is read only for the one article per story the page shows, and rows the
        # runner already has are not read again (load_rows). The two mirrors are read once here and
        # handed on: nothing below writes to them, so a second read would only repeat the queries.
        story_index, article_index = cache.stories(conn), cache.articles(conn)
        story_rows, art_rows, full_text, overflow_rows = load_rows(conn, since, story_index, article_index)
        sent = {n.date: {"publicUrl": n.public_url, "subject": n.subject}
                for n in conn.execute(select(db.newsletters.c.date, db.newsletters.c.public_url, db.newsletters.c.subject)).all()}
        # Stories folded into another one keep their address as a redirect (merge.py); their slugs
        # are read once and kept. Pairs that may be one event go to the dashboard (quality.py).
        merged_titles = cache.story_titles(conn, [s for s in story_index.values() if s.status == "merged" and s.redirect_to])
        suspects = list(cache.SUSPECTS.get(conn).get("pairs") or [])
        # Only threads a story points at can get a page, so only those are read.
        thread_ids = {s.thread_id for s in story_rows if s.thread_id}
        thread_meta = sorted((t for t in cache.threads(conn).values() if t.id in thread_ids and t.status == "published"),
                             key=lambda t: t.id)
        t_text = cache.thread_text(conn, thread_meta)
        thread_rows = [cache.merged(t, t_text.get(t.id)) for t in thread_meta if t.id in t_text]
        # Stories that just aged out of the window keep a small page (archive.py).
        archived = archive.update(conn, now, since, sources, [s.strip() for s in rules.get("unpublish") or [] if s],
                                  stories=story_index, articles=article_index)

    by_story: dict[int, list] = {}
    for a in art_rows:
        by_story.setdefault(a.story_id, []).append(a)
    overflow_by_story: dict[int, list] = {}
    for a in overflow_rows:
        overflow_by_story.setdefault(a.story_id, []).append(a)

    stories_out: list[dict] = []
    entity_index: dict[str, dict] = {}
    work_cards: dict[int, dict] = {}  # article id -> the stored card (work.py), for the story's card
    work_dropped: dict[str, dict[str, int]] = {}  # why -> tool name -> story id: cards the rules keep out
    for s in story_rows:
        members = by_story.get(s.id, [])
        if not members:
            continue
        lead = next((m for m in members if m.id == s.lead_article_id), members[0])
        articles = []
        for m in members:
            src = sources.get(m.source_id, {})
            community = (src.get("type") or "press") == "community" or src.get("discovered")
            # Cleaned again on the way out: a row written by an older version of the rules, or by
            # hand, can never put a half-card on a page.
            card, dropped = work_rules.screen_card(m.work_card)
            if card:
                work_cards[m.id] = card
            elif dropped:
                # Kept out by the rules (a developer tool, a course): counted for the admin page.
                work_dropped.setdefault(dropped, {})[str((m.work_card or {}).get("tool") or "?")[:80]] = s.id
            work_card = work_rules.card_out(card) if card else None
            # Community feeds point at other publishers: credit the publisher, keep the community as "via".
            articles.append({
                "id": m.id,
                "slug": m.slug,
                "url": m.url,
                "domain": m.domain,
                "source": m.domain if community else src.get("name"),
                "via": src.get("name") if community else None,
                "sourceKey": src.get("key"),
                "sourceType": _source_type(src, m.domain),
                "title": m.title,
                "headline": checks.discipline_headline(m.headline, m.title, checks.entity_names(m.entities))[0] if m.headline else m.headline,
                "author": m.author,
                "publishedAt": db.iso_z(m.published_at) or db.iso_z(m.fetched_at),
                "description": m.description,
                "contentMd": full_text.get(m.id),
                "wordCount": m.word_count,
                "imageUrl": m.image_url,
                "summaryMd": m.summary_md,
                "keyPoints": m.key_points or [],
                "whyItMatters": m.why_it_matters,
                "category": m.category,
                "entities": m.entities or {},
                "contentType": m.content_type,
                "importance": m.importance,
                "predictedScore": m.predicted_score,
                "isLead": m.id == lead.id,
                "modelRelease": m.model_release,
                "funding": m.funding,
                # AI at Work (/work): the practical card, or None when there is nothing to act on.
                "workCard": work_card,
                # The source hedged (may, could, reportedly, a question) and our headline does not.
                "hedged": headline_hedged(m.title, m.headline),
                "discussion": (
                    {"site": m.discussion_site, "url": m.discussion_url, "points": m.discussion_points}
                    if m.discussion_url else None
                ),
                "trendScore": m.trend_score,
            })
        coverage = _coverage(articles)
        # Members of a full story beyond the page's list (cluster.py): coverage, not shown.
        overflow = overflow_by_story.get(s.id, [])
        for m in overflow:
            kind = _source_type(sources.get(m.source_id, {}), m.domain)
            coverage[kind] = coverage.get(kind, 0) + 1
        discussions = sorted(
            (a["discussion"] for a in articles if a["discussion"]),
            key=lambda d: -(d["points"] or 0),
        )
        # The headline rules (checks.py) again on the way out, for headlines stored before them.
        story_headline = checks.discipline_headline(s.headline, lead.title, checks.entity_names(s.entities))[0] if s.headline else s.headline
        story = {
            "id": s.id,
            "slug": s.slug,
            "headline": story_headline,
            "summaryMd": s.summary_md,
            "keyPoints": s.key_points or [],
            "whyItMatters": s.why_it_matters,
            "category": s.category,
            "categoryName": config.CATEGORIES.get(s.category or "", "AI"),
            "entities": s.entities or {},
            "importance": s.importance,
            "score": s.score,
            "pinned": s.pinned,
            "articleCount": len(articles) + len(overflow),
            "overflowCount": len(overflow),
            "coverage": coverage,
            "hasPrimary": coverage["primary"] > 0,
            # The company's or lab's own announcement among them (primary.py): what confirms a story on
            # its own for the briefing; a paper, a repository or a government PDF does not.
            "hasAnnouncement": any(primary.announcement(a) for a in articles),
            "discussions": discussions,
            "threadId": s.thread_id,
            "pulse": s.pulse or None,
            "sourceNotes": s.source_notes if isinstance(getattr(s, "source_notes", None), dict) else None,
            "firstPublishedAt": db.iso_z(s.first_published_at),
            "updatedAt": db.iso_z(s.updated_at),
            "imageUrl": lead.image_url or next((a["imageUrl"] for a in articles if a["imageUrl"]), None),
            "ogImage": f"/og/{s.slug}.png",
            "leadArticleId": lead.id,
            # The story headline states as fact what the lead article's own title only suggests.
            "hedged": headline_hedged(lead.title, story_headline),
            "articles": articles,
        }
        # AI at Work (/work): one card per story, scored with the story's own context. The section
        # is broader than the "marketing" category: any story with a card belongs to it.
        chosen = work_rules.story_card(story, work_cards)
        story["workCard"] = work_rules.card_out(chosen, story) if chosen else None
        stories_out.append(story)
        for kind in ("companies", "models", "people"):
            for name in (s.entities or {}).get(kind, []) or []:
                add_entity(entity_index, name, kind, s.id)

    briefing = build_briefing(stories_out, now)
    # AI at Work: its own briefing, its own tool directory and its own weekly playbooks, all built
    # from the cards. The main briefing above is untouched; an item can appear in both, because the
    # front page says what happened and the section says what to do about it.
    section = work_rules.section_stories(stories_out)
    # What readers do with the cards moves their order a little, once there is enough of it
    # (work_learn.py): one grouped read every few hours, kept in the runner cache. Any failure
    # leaves the rules order exactly as it was.
    learned, learn_stats = None, {"active": False}
    try:
        with eng.connect() as conn:
            learned, learn_stats = work_learn.learn(stories_out, conn, now, section)
    except Exception as exc:  # noqa: BLE001 - reader data must never cost the export
        log.warning("AI at Work learning failed: %s", str(exc)[:200])
        learn_stats = {"active": False, "error": str(exc)[:120]}
    work_briefing = work_rules.build_briefing(stories_out, now)
    if learned is not None:
        work_briefing["learning"] = learning_summary(learned, stories_out, section, work_briefing, now)
    work_out = {
        "generatedAt": db.iso_z(now),
        "storyIds": [s["id"] for s in sorted(section, key=lambda s: s.get("firstPublishedAt") or "", reverse=True)],
        "tools": work_rules.build_tools(section),
        "weeks": work_rules.weeks(section),
        "jobs": {job: [s["id"] for s in section if job in (s["workCard"]["jobs"] or [])] for job in work_rules.JOBS},
        # What the rules kept out of the section (work.screen_card), for the admin page's health line:
        # {"developer": {"count": stories, "tools": [names]}, "course": {...}}.
        # A story that still has a card from another of its articles is not counted.
        "dropped": _work_dropped(work_dropped, {s["id"] for s in section}),
    }
    # What the tools cost, as a history rather than one week's figure (prices.py). Every price here
    # was already read as part of a card, so this adds no database read; the store is a file in the
    # runner cache and the site gets its own copy beside work.json.
    price_stats = {}
    try:
        price_store = price_rules.load()
        price_stats = price_rules.record_from_stories(price_store, stories_out)
        price_rules.save(price_store, now)
        price_stats["tools"] = price_rules.write_export(price_store, now, out_dir / "work-prices.json")
    except Exception as exc:  # noqa: BLE001 - the price history must never cost the export
        log.warning("AI at Work prices not recorded: %s", str(exc)[:200])
        price_stats = {"error": str(exc)[:120]}
    exported = {st["id"]: st["slug"] for st in stories_out}
    redirects = story_redirects(story_index, merged_titles, exported)
    duplicates = [p for p in suspects if p.get("a") in exported and p.get("b") in exported]

    # Threads: only those with 2+ stories are worth a page; singletons stay invisible.
    by_thread: dict[int, list[dict]] = {}
    for st in stories_out:
        if st["threadId"]:
            by_thread.setdefault(st["threadId"], []).append(st)
    threads_out = []
    for t in thread_rows:
        members = sorted(by_thread.get(t.id, []), key=lambda x: x["firstPublishedAt"] or "")
        if len(members) < 2:
            continue
        lead_story = max(members, key=lambda x: (x["importance"], x["score"]))
        named = (t.named_count or 0) >= 2
        threads_out.append({
            "id": t.id,
            "slug": t.slug,
            "title": t.title if named else lead_story["headline"],
            "summary": t.summary if named else (lead_story.get("whyItMatters") or t.summary),
            "named": named,
            "ogImage": f"/og/thread-{t.slug}.png",
            "category": t.category,
            "categoryName": config.CATEGORIES.get(t.category or "", "AI"),
            "entities": t.entities or {},
            "storyCount": len(members),
            "firstAt": members[0]["firstPublishedAt"],
            "updatedAt": members[-1]["updatedAt"],
            "storyIds": [m["id"] for m in members],
        })
    threads_out.sort(key=lambda x: x["updatedAt"] or "", reverse=True)

    # Trackers: one row per model / funding event, deduplicated across articles.
    models: dict[str, dict] = {}
    for st in stories_out:
        for a in st["articles"]:
            r = a.get("modelRelease")
            # Only real launches of usable models, once each however the name is spelled (trackers.py).
            if r and r.get("name") and tracker_rules.is_release(r, f"{st['headline']} | {a.get('title') or ''}"):
                k = tracker_rules.model_key(r["name"], r.get("lab"))
                row = models.setdefault(k, {**r, "date": a["publishedAt"], "storySlug": st["slug"], "storyHeadline": st["headline"], "sources": 0})
                row["sources"] += 1
                if (a["publishedAt"] or "") < (row["date"] or ""):
                    row["date"] = a["publishedAt"]
                for f in ("license", "context", "link", "lab"):
                    if not row.get(f) and r.get(f):
                        row[f] = r[f]
    models = tracker_rules.fold_versions(models)
    # Funding: one row per deal, with figures the coverage agrees on and that the articles' own text
    # states (funding.py); the page's total adds these rows only.
    funding_rows, funding_dropped = funding_rules.build(stories_out, now)
    trackers = {
        "models": sorted(models.values(), key=lambda r: r["date"] or "", reverse=True),
        "funding": funding_rows,
        "fundingTotalUsd": funding_rules.total(funding_rows),
    }
    # Hub pages: every entity with two or more stories, plus any model or company that has a
    # tracker row (a fact box makes a page worthwhile even with one story).
    tracked = {r["name"].strip().lower() for r in trackers["models"]} | {r["company"].strip().lower() for r in trackers["funding"]}
    entities_out = sorted(
        (entity_out(e) for e in entity_index.values() if len(e["storyIds"]) >= 2 or e["name"].strip().lower() in tracked),
        key=lambda e: len(e["storyIds"]), reverse=True,
    )

    (out_dir / "threads.json").write_text(json.dumps(threads_out, ensure_ascii=False), encoding="utf-8")
    (out_dir / "trackers.json").write_text(json.dumps(trackers, ensure_ascii=False), encoding="utf-8")
    (out_dir / "stories.json").write_text(json.dumps(stories_out, ensure_ascii=False), encoding="utf-8")
    (out_dir / "entities.json").write_text(json.dumps(entities_out, ensure_ascii=False), encoding="utf-8")
    (out_dir / "briefing.json").write_text(json.dumps(briefing, ensure_ascii=False), encoding="utf-8")
    (out_dir / "work.json").write_text(json.dumps(work_out, ensure_ascii=False), encoding="utf-8")
    (out_dir / "work-briefing.json").write_text(json.dumps(work_briefing, ensure_ascii=False), encoding="utf-8")
    (out_dir / "redirects.json").write_text(json.dumps(redirects, ensure_ascii=False), encoding="utf-8")
    (out_dir / "duplicates.json").write_text(json.dumps(duplicates, ensure_ascii=False), encoding="utf-8")
    (out_dir / "newsletters.json").write_text(json.dumps(sent, ensure_ascii=False), encoding="utf-8")
    archived["pages"] = archive.export(out_dir, {s["slug"] for s in stories_out})
    (out_dir / "sources.json").write_text(
        json.dumps([v for v in sources.values() if v["enabled"] and not v["discovered"]], ensure_ascii=False), encoding="utf-8")
    (out_dir / "meta.json").write_text(json.dumps({
        "generatedAt": db.iso_z(now),
        "siteUrl": config.SITE_URL,
        "categories": config.CATEGORIES,
        "storyCount": len(stories_out),
        "articleCount": sum(s["articleCount"] for s in stories_out),
    }), encoding="utf-8")
    return {"stories": len(stories_out), "entities": len(entities_out), "briefing": len(briefing["storyIds"]),
            "work": len(section), "workTools": len(work_out["tools"]), "workBriefing": len(work_briefing["storyIds"]),
            "workDropped": {why: d["count"] for why, d in work_out["dropped"].items()},
            "workLearn": learn_stats, "workPrices": price_stats,
            "threads": len(threads_out), "models": len(trackers["models"]), "funding": len(trackers["funding"]),
            "fundingDropped": funding_dropped, "hedged": sum(1 for s in stories_out if s["hedged"]),
            "redirects": len(redirects), "moderation": moderation, "archive": archived, "dir": str(out_dir)}
