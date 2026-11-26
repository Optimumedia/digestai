"""One story per event: which article leads a story, when two stories are the same event, and
merging the newer story into the older one (its old address then redirects there).

Duplicate stories came from the cluster cap: once a story held CLUSTER_MAX_ARTICLES, every further
article on the event started a new story (seventeen pages for one funding round). The cluster step
now attaches such articles as overflow (cluster.py); this module merges the duplicates that already
exist and any that still slip through, a bounded number per run, and lists the pairs just under
the bar for review on the dashboard (quality.py). Everything it compares comes from the runner's
copy (cache.py): the story vectors of the threads window and the story and article texts the
export reads anyway, so a run without duplicates reads nothing extra.
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta
from difflib import SequenceMatcher

import numpy as np
from sqlalchemy import select, update

from . import cache, config, db

log = logging.getLogger("digest.merge")

MERGED = "merged"           # stories.status of a story folded into another (redirect_to points there)
STRONG_MARGIN = 0.05        # this far above the bar, two stories are one event even without a shared name
MERGE_MIN_WORDS = 0.40      # with one shared name, the headlines must also repeat this much of each other
SUSPECT_MARGIN = 0.06       # this far below it, with a shared name, a pair is listed for review
MAX_SUSPECTS = 20
LEAD_IMPORTANCE_MARGIN = 2  # a new lead must beat the current one by this much to change the headline

STOP = set("a an the and or of to in on for with by at from as is are be its it this that new says said after over into how why what".split())


# ---------------------------------------------------------------------------- lead choice

# Commentary describes an event second-hand: it may lead a story only while no news article is in it.
NOT_NEWS_TYPES = {"opinion", "analysis", "newsletter", "podcast", "tutorial", "listicle"}
OPINION_START = re.compile(r"^(?:why|how|what|should|could|can|will|is|are|do|does|opinion|column|i|i'm|i've|we|we're|we've|my|our|let's|here's)\b", re.I)
# "I" only as a word (not the I in "A.I."), plus the contractions and "my" in lower case.
FIRST_PERSON = re.compile(r"(?<![\w.'])(?:I|I'm|I've|I'd)(?![\w.'])|\bmy\b")


# A round-up of the day's or the week's news: "AI News Weekly Summary | GPT-6.2, Gemini 4 Neon, Free
# Tasks", "[IT News] 3rd Week of November 2026 #51 | ...", "4 AI News Items Checked Today | ...". It
# names several events, so its embedding sits between them: in the November sample (25 Nov 2026) such
# pieces led five stories under the wrong headline ("Anthropic raises $81.3 billion" over a story about
# Gemini 4 Neon's Zapier scores) and six of them formed a story of their own. Matched on the title.
ROUNDUP = re.compile(
    r"\b(?:ai|tech|(?-i:IT)|technology|generative ai)\s+(?:news|weekly|trends|brief)\b"
    r"(?!\s+(?:anchors?|sites?|outlets?|publishers?|apps?|summaries|bots?|readers?|startups?|deals?|licensing|industry)\b)"
    r"|\b(?:news|stories|items|announcements)\s+(?:of|from|this|for)\s+(?:the\s+)?(?:week|day|morning|past|today)\b"
    r"|\b(?:this|last)\s+week'?s\s+(?:\w+\s+){0,2}news\b"
    r"|\bweekly\s+(?:summary|report|roundup|round-up|recap|digest|vol)\b|\bweek\s+in\s+(?:ai|review|tech)\b"
    r"|\b(?:news\s+)?(?:roundup|round-up)\b|\bnews\s+(?:summary|recap|digest|flash|commentary)\b"
    r"|\b(?:morning|evening|weekend)\s+edition\b|\b(?:1st|2nd|3rd|4th|5th|first|second|third|fourth|last)\s+week\s+of\b"
    r"|\b\d{4}\s+issue\b|\bvol\.?\s*\d+\s*[|:]|#\d{2,}\s*[|:]"
    r"|[\[(【]\s*\d{1,2}/\d{1,2}\s*[~～–—-]\s*\d{1,2}/\d{1,2}\s*[\])】]"
    r"|\((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\.?\s+\d{1,2}\s*[–—-]\s*(?:\w+\.?\s+)?\d{1,2}\)",
    re.I)


def is_roundup(title: str | None) -> bool:
    """A piece that rounds up several stories (a weekly summary, a morning list): it may never lead a
    story, and never joins or starts one (cluster.py)."""
    return bool(ROUNDUP.search(title or ""))


def is_news(content_type: str | None) -> bool:
    return (content_type or "news") not in NOT_NEWS_TYPES


def opinion_headline(headline: str | None) -> bool:
    """A question, a first-person piece or a why/how explainer: never a story's headline while news exists."""
    h = (headline or "").strip()
    return bool(h) and (h.endswith("?") or bool(OPINION_START.match(h)) or bool(FIRST_PERSON.search(h)))


def news_ok(headline: str | None, content_type: str | None) -> bool:
    return is_news(content_type) and not opinion_headline(headline)


def better_lead(new: dict, cur: dict) -> bool:
    """Whether an article just merged into a story replaces its lead (the lead's headline and digest
    are the story's). News beats commentary and commentary never displaces news; a primary source
    leads when it is at least as important; anything else must be clearly more important.
    Both: headline, content_type, importance, primary."""
    n_ok, c_ok = news_ok(new.get("headline"), new.get("content_type")), news_ok(cur.get("headline"), cur.get("content_type"))
    if n_ok != c_ok:
        return n_ok
    ni, ci = new.get("importance") or 0, cur.get("importance") or 0
    if n_ok and new.get("primary") and not cur.get("primary"):
        return ni >= ci
    return ni >= ci + LEAD_IMPORTANCE_MARGIN


def headline_tokens(text: str | None) -> list[str]:
    return [t for t in re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split() if t not in STOP]


# Words in nearly every AI headline, or verbs of announcement: two headlines sharing them say nothing
# about being the same event. What is left ("california", "sue", "taalas", "wallet") names the event.
GENERIC = set("""ai artificial intelligence model models new launch launches launched releases release released
adds add added unveils unveil unveiled announces announce announced introduces introduce introduced says say
report reports reported reportedly according tool tools company companies firm firms startup startups user users
agent agents app apps update updates plan plans more than up after amid over its their his her will could may can
first big major latest top tech technology data system systems feature features service services""".split())


# Words that report the same act differently: "California sues OpenAI" and "lawsuit by California".
SAME_ACT = {"lawsuit": "sue", "lawsuits": "sue", "sued": "sue", "sues": "sue", "suing": "sue",
            "acquisition": "acquire", "acquires": "acquire", "acquired": "acquire", "buys": "acquire", "buy": "acquire",
            "resigns": "resign", "resigned": "resign", "resignation": "resign", "quits": "resign",
            "raises": "raise", "raised": "raise", "funding": "raise", "bans": "ban", "banned": "ban"}
# Verbs that put something new out: two headlines that both launch the same named model are its launch.
LAUNCH_VERBS = set("""launch launches launched release releases released unveil unveils unveiled announce announces
announced introduce introduces introduced debut debuts ships ship rolls adds add opens open""".split())


def _stem(word: str) -> str:
    if word in SAME_ACT:
        return SAME_ACT[word]
    for suffix in ("ing", "ed", "es", "s"):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def event_words(headline: str | None) -> list[str]:
    """The words of a headline that name its event: no stop words, no words common to all AI news, no
    lone digits; light stemming, so "sues" and "sued" meet."""
    return [_stem(t) for t in headline_tokens(headline) if t not in GENERIC and len(t) > 1]


def coverage_share(tokens: list[str], titles: list[list[str]]) -> float:
    """How many of the titles share two or more words with these tokens, as a fraction."""
    if not titles or len(tokens) < 2:
        return 0.0
    mine = set(tokens)
    return sum(1 for t in titles if len(mine & set(t)) >= 2) / len(titles)


def pick_lead(members: list, texts: dict, primary) -> object | None:
    """The member whose digest describes a story: news over commentary, then importance with a point
    for a primary source and up to a point for wording the other sources share, then the earliest.
    members: mirror article rows; texts: their text rows (cache.article_text); primary(row) -> bool."""
    shown = [m for m in members if m.status == "published" and m.id in texts]
    if not shown:
        return None
    titles = [headline_tokens(texts[m.id].title or texts[m.id].headline) for m in shown]

    def key(m):
        t = texts[m.id]
        head = t.headline or t.title
        return (news_ok(head, m.content_type),
                (m.importance or 0) + (1 if primary(m) else 0) + coverage_share(headline_tokens(head), titles), -m.id)

    return max(shown, key=key)


# ---------------------------------------------------------------------------- same event

def _tokens_similarity(ta: list[str], tb: list[str]) -> float:
    if not ta or not tb:
        return 0.0
    sa, sb = set(ta), set(tb)
    jac = len(sa & sb) / len(sa | sb)
    if jac < 0.4:
        return jac
    return max(jac, SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio())


def headline_similarity(a: str | None, b: str | None) -> float:
    return _tokens_similarity(headline_tokens(a), headline_tokens(b))


def entity_names(entities) -> set[str]:
    names = set()
    if not isinstance(entities, dict):
        return names
    for kind in ("companies", "models", "people"):
        for n in entities.get(kind) or []:
            key = re.sub(r"[^a-z0-9]+", " ", str(n).lower()).strip()
            if key:
                names.add(key)
    return names


def shared_entities(a, b) -> list[str]:
    return sorted(entity_names(a) & entity_names(b))


def facts_of(headline, entities, at, sources: int | None = None) -> dict:
    """What same_event compares, with the headline's tokens and the entity names worked out once:
    find_pairs asks about every pair of stories in the window, so per-pair tokenising was the
    whole cost of the cluster step (fifty seconds over a thousand stories). sources: how many
    articles the story holds, when known."""
    tokens = headline_tokens(headline)
    names = entity_names(entities)
    have = set(tokens)
    models = entity_names({"models": (entities or {}).get("models") if isinstance(entities, dict) else None})
    # A named model the headline is about: every word of its name in the headline, and a name that
    # picks out one release ("gemini 4 neon", "gpt 6 2 astra"), not a family ("claude").
    subjects = {m for m in models if (len(m.split()) >= 2 or any(c.isdigit() for c in m)) and set(m.split()) <= have}
    return {"headline": headline, "entities": entities, "at": at, "sources": sources,
            "tokens": tokens, "sorted": sorted(tokens), "names": names,
            "words": set(event_words(headline)), "name_words": {_stem(w) for n in names for w in n.split()},
            "launch": bool(have & LAUNCH_VERBS), "subjects": subjects}


def _facts(x: dict) -> dict:
    return x if "tokens" in x else facts_of(x.get("headline"), x.get("entities"), x.get("at"), x.get("sources"))


# Who did it, not what happened: "Nvidia interim CEO Kress addresses staff" and "Nvidia interim CEO
# Kress says the board will not reinstate Huang" share four words and are two events.
ROLE_WORDS = {_stem(w) for w in "ceo cfo cto chief executive executives interim president founder founders cofounder chair chairman head boss".split()}
CLOSE_SIM = 0.86   # with three shared event words, one of them saying what happened, inside MERGE_CLOSE_HOURS
NEAR_SIM = 0.92    # this alike, two shared event words are enough inside MERGE_CLOSE_HOURS
LAUNCH_SIM = 0.88  # two launches of the same named model inside MERGE_CLOSE_HOURS
RESTATED_WORDS = 0.6  # a later one-source story whose headline repeats this much of an older one's restates it


def _shared_words(a: dict, b: dict) -> tuple[set, set]:
    """Event words both headlines use, and those of them that are neither a name nor a role: the
    words that say what happened."""
    shared = a["words"] & b["words"]
    return shared, shared - a["name_words"] - b["name_words"] - ROLE_WORDS


def same_event(a: dict, b: dict, sim: float | None, thr: float) -> str | None:
    """Why two stories are one event, or None. a and b: headline, entities, at (when each broke, a
    datetime or None), sources (article count, optional), or the same prepared by facts_of. sim: the
    cosine similarity of their embeddings, or None when unknown (the exported data carries none):
    then the wording and the names have to give it away."""
    gap = abs(a["at"] - b["at"]) if a.get("at") and b.get("at") else None
    if gap is not None and gap > timedelta(days=config.MERGE_PAIR_DAYS):
        return None
    a, b = _facts(a), _facts(b)
    if len(a["tokens"]) >= 3 and a["sorted"] == b["sorted"]:
        return "the same headline"
    shared = sorted(a["names"] & b["names"])
    if not config.MERGE_DUPLICATES:
        # Wording only: two feeds carrying the same piece, or the same headline reworded slightly.
        if shared and _tokens_similarity(a["tokens"], b["tokens"]) >= 0.85:
            return f"nearly the same headline, both about {shared[0].title()}"
        return None
    if sim is not None:
        close = gap is None or gap <= timedelta(hours=config.MERGE_CLOSE_HOURS)
        words, plain = _shared_words(a, b)
        listed = ", ".join(f'"{w}"' for w in sorted(words)[:4])
        # One event is covered within a day or two, under headlines that repeat its words: on
        # 16 Nov "California sues OpenAI" was ten separate stories, 0.90 to 0.96 alike, while the
        # bar below asked 0.97. Two stories that alike, that close in time, sharing the words that
        # say what happened, are one event; sharing only names and roles ("Nvidia interim CEO
        # Kress ...") is not enough.
        if close and sim >= NEAR_SIM and len(words) >= 2:
            return f"{sim:.2f} alike, both headlines say {listed}"
        if close and sim >= CLOSE_SIM and len(words) >= 3 and plain:
            return f"{sim:.2f} alike, both headlines say {listed}"
        # A model launch: both headlines put out the same named release ("Google announces Gemini 4
        # Neon ..." and "Google introduces Gemini 4 Neon ..." were four stories on 19 Nov).
        launched = a["subjects"] & b["subjects"]
        if close and sim >= LAUNCH_SIM and a["launch"] and b["launch"] and launched:
            return f"{sim:.2f} alike, both launch {sorted(launched)[0].title()}"
        # A one-source story days later whose headline repeats an older one ("California sues
        # OpenAI over rogue agents that hacked Hugging Face" on 24 Nov, eight days on) is the old
        # news again: it joins the older story instead of showing as new.
        if not close and sim >= CLOSE_SIM and plain and len(words) >= 3 and 1 in (a.get("sources"), b.get("sources")):
            union = a["words"] | b["words"]
            if union and len(words) / len(union) >= RESTATED_WORDS:
                return f"{sim:.2f} alike, a later headline repeats {listed}"
        # A shared name alone is not evidence: "Anthropic" is in most AI stories, and trusting it at
        # 0.88 merged a dozen separate events in September. At this bar a pair also has to share two
        # names, or repeat some of the same words, or be alike enough to stand on its own. Only for
        # stories that broke close together: two week-long stories about one company are alike at
        # 0.97 whatever happened in them, and merging them built one story out of eight days of a saga.
        if not close:
            return None
        if sim >= thr and shared:
            if len(shared) >= 2:
                return f"{sim:.2f} alike, both about {shared[0].title()} and {shared[1].title()}"
            if _tokens_similarity(a["tokens"], b["tokens"]) >= MERGE_MIN_WORDS:
                return f"{sim:.2f} alike, both about {shared[0].title()}"
        if sim >= thr + STRONG_MARGIN:
            return f"{sim:.2f} alike"
        return None
    if shared and _tokens_similarity(a["tokens"], b["tokens"]) >= 0.6:
        return f"nearly the same headline, both about {shared[0].title()}"
    return None


def find_pairs(stories: list, vecs: dict, thr: float) -> tuple[list[tuple], list[tuple]]:
    """Among stories (id, headline, entities, first_published_at) with their vectors: the pairs that
    are one event, most alike first, and the pairs just under the bar. Each is (sim, reason, a_id, b_id)."""
    facts = {s.id: facts_of(s.headline, s.entities, db.as_utc(s.first_published_at), getattr(s, "article_count", None))
             for s in stories}
    with_vec = [s.id for s in stories if vecs.get(s.id) is not None]
    dims = [vecs[i].shape[0] for i in with_vec]
    dim = max(set(dims), key=dims.count) if dims else 0
    with_vec = [i for i in with_vec if vecs[i].shape[0] == dim]
    order = {s.id: k for k, s in enumerate(stories)}
    ids = [s.id for s in stories]
    pos = {i: k for k, i in enumerate(with_vec)}
    m = np.stack([vecs[i] for i in with_vec]).astype(np.float32) if len(with_vec) >= 2 else None
    matrix = m @ m.T if m is not None else None

    def sim_of(a: int, b: int) -> float | None:
        return float(matrix[pos[a], pos[b]]) if matrix is not None and a in pos and b in pos else None

    # Below this similarity no rule finds two stories one event except the same headline, so only the
    # pairs above it, the pairs with one headline, and the stories without a comparable embedding are
    # examined (every pair of two thousand stories was seconds of every run).
    floor = min(CLOSE_SIM, LAUNCH_SIM, NEAR_SIM, thr - SUSPECT_MARGIN, thr)
    key = lambda a, b: (a, b) if order[a] < order[b] else (b, a)  # noqa: E731
    candidates: set[tuple[int, int]] = set()
    if not config.MERGE_DUPLICATES:
        candidates = {(ids[x], ids[y]) for x in range(len(ids)) for y in range(x + 1, len(ids))}
    else:
        if matrix is not None:
            for x, y in np.argwhere(np.triu(matrix, 1) >= floor):
                candidates.add(key(with_vec[x], with_vec[y]))
        same: dict[tuple, list[int]] = {}
        for i in ids:
            if len(facts[i]["tokens"]) >= 3:
                same.setdefault(tuple(facts[i]["sorted"]), []).append(i)
        for group in same.values():
            candidates.update(key(group[x], group[y]) for x in range(len(group)) for y in range(x + 1, len(group)))
        for i in ids:
            if i not in pos:
                candidates.update(key(i, j) for j in ids if j != i)
    pairs, suspects = [], []
    for a, b in sorted(candidates, key=lambda p: (order[p[0]], order[p[1]])):
        sim = sim_of(a, b)
        reason = same_event(facts[a], facts[b], sim, thr)
        if reason:
            pairs.append((1.0 if sim is None else sim, reason, a, b))
        elif sim is not None and sim >= thr - SUSPECT_MARGIN:
            fa, fb = facts[a], facts[b]
            if fa["at"] and fb["at"] and abs(fa["at"] - fb["at"]) > timedelta(days=config.MERGE_PAIR_DAYS):
                continue
            shared = sorted(fa["names"] & fb["names"])
            if shared:
                suspects.append((sim, f"{sim:.2f} alike, both about {shared[0].title()}", a, b))
    pairs.sort(key=lambda p: (-p[0], p[2], p[3]))
    suspects.sort(key=lambda p: (-p[0], p[2], p[3]))
    return pairs, suspects


# ---------------------------------------------------------------------------- merging

def _mean(vec_a, n_a: int, vec_b, n_b: int):
    parts = [(v, n) for v, n in ((vec_a, n_a), (vec_b, n_b)) if v is not None]
    if not parts or len({v.shape for v, _n in parts}) != 1:
        return None
    m = sum(np.asarray(v, dtype=np.float32) * max(1, n) for v, n in parts) / sum(max(1, n) for _v, n in parts)
    norm = np.linalg.norm(m)
    return m / norm if norm else m


def merge_stories(conn, keep, drop, members: list, texts: dict, primary, vec_keep, vec_drop, now) -> dict:
    """Fold `drop` into `keep`: its articles move over, keep's lead is chosen again among every
    member, and drop stays as a signpost (status merged, redirect_to). keep and drop: story rows with
    their text (cache.merged); members: the articles of both (mirror rows); texts: the text of the
    shown ones (cache.article_text)."""
    conn.execute(update(db.articles).where(db.articles.c.story_id == drop.id).values(story_id=keep.id))
    lead = pick_lead(members, texts, primary)
    firsts = [db.as_utc(x) for x in (keep.first_published_at, drop.first_published_at) if x]
    values = {
        "article_count": len(members), "updated_at": now,
        "first_published_at": min(firsts) if firsts else now,
        "importance": max(keep.importance or 0, drop.importance or 0) or 5,
        "pinned": bool(keep.pinned or drop.pinned),
    }
    mean = _mean(vec_keep, keep.article_count or 1, vec_drop, drop.article_count or 1)
    if mean is not None:
        values["embedding"] = db.pack_vec(mean)
    if drop.thread_id and not keep.thread_id:
        values["thread_id"] = drop.thread_id
    elif drop.thread_id:
        # The thread lost an episode (it was keep's thread too, or another one).
        conn.execute(update(db.threads).where(db.threads.c.id == drop.thread_id)
                     .values(story_count=db.threads.c.story_count - 1))
    if not keep.pulse and drop.pulse:
        values.update(pulse=drop.pulse, pulse_at=now)
    changed_lead = lead is not None and lead.id != keep.lead_article_id
    if changed_lead:
        t = texts[lead.id]
        values.update(lead_article_id=lead.id, headline=t.headline or t.title, summary_md=t.summary_md, key_points=t.key_points,
                      why_it_matters=t.why_it_matters, entities=t.entities, importance=max(values["importance"], lead.importance or 0))
        if lead.category:
            values["category"] = lead.category
    conn.execute(update(db.stories).where(db.stories.c.id == keep.id).values(**values))
    conn.execute(update(db.stories).where(db.stories.c.id == drop.id)
                 .values(status=MERGED, redirect_to=keep.id, updated_at=now, pinned=False))
    return {"lead_changed": changed_lead, "articles": len(members), **{k: v for k, v in values.items() if k in ("headline", "thread_id")}}


def run(eng, thr: float, max_merges: int | None = None) -> dict:
    """Merge duplicate published stories of the last MERGE_LOOKBACK_DAYS, the older one staying,
    at most `max_merges` per run; then remember the pairs just under the bar for the dashboard."""
    max_merges = config.MERGE_MAX_PER_RUN if max_merges is None else max_merges
    stats = {"merged_stories": 0, "articles_moved": 0, "suspects": 0}
    now = db.utcnow()
    since = now - timedelta(days=config.MERGE_LOOKBACK_DAYS)
    with eng.begin() as conn:
        mirror = cache.stories(conn)
        rows = sorted((s for s in mirror.values()
                       if s.status == "published" and (db.as_utc(s.first_published_at) or now) >= since), key=lambda s: s.id)
        if len(rows) < 2:
            cache.SUSPECTS.put(conn, {"pairs": []})
            return stats
        vecs = cache.story_vectors(conn, [s for s in rows if (s.len_embedding or -1) >= 0])
        text = cache.story_text(conn, rows)
        stories = [cache.merged(s, text[s.id]) for s in rows if s.id in text]
        pairs, suspects = find_pairs(stories, vecs, thr)
        by_id = {s.id: s for s in stories}
        moved: dict[int, int] = {}

        def root(i: int) -> int:
            while i in moved:
                i = moved[i]
            return i

        if pairs:
            source_type = dict(conn.execute(select(db.sources.c.id, db.sources.c.source_type)).all())
            primary = lambda m: m.domain in config.PRIMARY_DOMAINS or source_type.get(m.source_id) == "primary"  # noqa: E731
        for sim, reason, a_id, b_id in pairs:
            if stats["merged_stories"] >= max_merges:
                break
            a, b = by_id[root(a_id)], by_id[root(b_id)]
            if a.id == b.id:
                continue
            keep, drop = sorted((a, b), key=lambda s: (db.as_utc(s.first_published_at) or now, s.id))
            members = sorted((m for m in cache.articles(conn).values()
                              if m.story_id in (keep.id, drop.id) and m.status in ("published", "overflow")), key=lambda m: m.id)
            if len(members) > config.CLUSTER_MAX_TOTAL:
                # A merge used to move every member over whatever the size: chained merges grew one
                # story to 161 sources. Two stories that would pass the ceiling together stay two
                # (the cluster step's repair keeps the shown part of a merged story at its cap).
                stats["too_big"] = stats.get("too_big", 0) + 1
                continue
            texts = cache.article_text(conn, [m for m in members if m.status == "published"])
            done = merge_stories(conn, keep, drop, members, texts, primary, vecs.get(keep.id), vecs.get(drop.id), now)
            moved[drop.id] = keep.id
            keep.article_count = len(members)
            keep.thread_id = done.get("thread_id", keep.thread_id)
            if done.get("headline"):
                keep.headline = done["headline"]
            stats["merged_stories"] += 1
            stats["articles_moved"] += drop.article_count or 1
            log.info("story #%s (%s) merged into #%s: %s", drop.id, drop.headline, keep.id, reason)
        live = [(sim, reason, a, b) for sim, reason, a, b in suspects if a not in moved and b not in moved][:MAX_SUSPECTS]
        cache.SUSPECTS.put(conn, {"pairs": [{"a": a, "b": b, "sim": round(sim, 3), "reason": reason} for sim, reason, a, b in live]})
        stats["suspects"] = len(live)
    return stats
