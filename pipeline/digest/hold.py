"""Risky-claim hold: single-source stories that accuse named people or companies wait for approval.

A story is held when both are true:
- it has one independent source: one article, or every article from the same publisher domain;
- its headline (or, twice over, its key points and summary) describes crime, violence, weapons or
  military use, surveillance or spying, hacking or data breaches, fraud, lawsuits, arrests or
  charges, abuse or harassment, or another allegation of wrongdoing, and a named person or company
  is involved.

Held stories stay in the database with status "held". The export step publishes only "published"
stories, so a held story has no page and never reaches the front page, feeds, Bluesky or browser
alerts. It leaves the hold when a second publisher covers it, or when its slug is in the `approve`
list of moderation.yaml. The owner reviews the list on the admin page: the pipeline writes it
encrypted with ADMIN_REVIEW_KEY, because the admin page and the repository are public.

Detection is keyword and pattern based on purpose: no model quota, and every rule is testable.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
from pathlib import Path

from sqlalchemy import select, update

from . import db

log = logging.getLogger("digest.hold")

HELD = "held"
KDF_ITERATIONS = 600_000

# Each category is a list of whole-word alternatives, case-insensitive. Verb forms are spelled out so
# "sues", "sued" and "suing" all match while "suite" or "hackathon" do not.
CATEGORIES: list[tuple[str, str]] = [
    ("weapons or military use",
     r"weapons?|weaponi[sz](?:e|es|ed|ing|ation)|bioweapons?|missiles?|ballistic|warheads?|munitions?|bombs?|bombing|"
     r"explosives?|drone strikes?|air ?strikes?|warfare|military (?:use|uses|targeting|operations?|applications?|purposes)|"
     r"lethal|militants?|terror(?:ism|ist|ists)|houthis?|hamas|hezbollah"),
    ("surveillance or spying",
     r"surveil(?:s|led|ling|lance)?|spy|spies|spied|spying|spyware|stalkerware|eavesdrop(?:s|ped|ping)?|wiretap(?:s|ped|ping)?|"
     r"(?:monitor|track)(?:s|ed|ing)? (?:its |their |ai )?(?:critics|journalists|activists|dissidents|protesters|employees)"),
    ("hacking or data breach",
     r"hack(?:s|ed)?|hacking|hackers?|breach(?:es|ed)?|data leaks?|"
     r"leaked (?:data|documents?|emails?|memos?|messages|records|files|chats?|conversations)|"
     r"cyber-? ?attacks?|ransomware|malware|phishing|stole|stolen|steal(?:s|ing)?|theft|exfiltrat(?:e|es|ed|ing|ion)"),
    ("fraud",
     r"fraud(?:s|ulent|ulently|ster|sters)?|scam(?:s|med|mer|mers|ming)?|embezzl(?:e|es|ed|ing|ement)|brib(?:e|es|ed|ery|ing)|"
     r"launder(?:s|ed|ing)?|ponzi|insider trading|defraud(?:s|ed|ing)?|misled investors"),
    ("lawsuit, arrest or charges",
     r"lawsuits?|sue|sues|sued|suing|class[- ]action|indict(?:s|ed|ment|ments)?|charged with|criminal charges|charges against|"
     r"arrest(?:s|ed|ing)?|convicted|conviction|sentenced|pleads? guilty|pleaded guilty|found guilty|"
     r"prosecut(?:e|es|ed|ing|ion|ors?)|subpoena(?:s|ed)?|fined|raided|"
     r"(?:antitrust|criminal|federal|regulatory|ftc|sec|doj|fbi) (?:probe|probes|investigation|investigations)"),
    ("violence, abuse or harassment",
     r"murder(?:s|ed|ing)?|killed|killings?|assault(?:s|ed|ing)?|violence|violent|shootings?|abuse|abuses|abused|abusive|"
     r"harass(?:es|ed|ing|ment)|sexual misconduct|stalk(?:s|ed|ing)|child sexual|csam|non-?consensual|sextortion|"
     r"suicide|self-harm"),
    ("allegation of wrongdoing",
     r"accus(?:e|es|ed|ing|ation|ations)|alleg(?:e|es|ed|edly|ing|ation|ations)|whistle-?blowers?|cover-?ups?|covered up|"
     r"misconduct|scandals?|illegal|illegally|unlawful(?:ly)?|violat(?:e|es|ed|ing|ion|ions)|"
     r"without (?:consent|permission|telling)|lied|lying|deceiv(?:e|es|ed|ing)|deceptive|manipulated|cheated|wrongdoing|"
     r"exploit(?:s|ed|ing)? (?:users|workers|children|minors)"),
]
_PATTERNS = [(label, re.compile(rf"\b(?:{alts})\b", re.I)) for label, alts in CATEGORIES]

# Phrases that contain a risky word but describe routine things; removed before matching.
_BENIGN = re.compile(r"\bhacker news\b|\breward hack(?:s|ing)?\b|\bhackathons?\b|\bgrowth hack(?:s|ing)?\b|\blife ?hacks?\b|"
                     r"\bspy ?glass\b|\bbreach(?:es)? of contract\b", re.I)

# Capitalised words that do not name anyone: sentence starters and generic terms.
_NOT_NAMES = {
    "a", "an", "the", "ai", "new", "how", "why", "what", "when", "who", "this", "these", "that", "study", "report",
    "reports", "research", "researchers", "scientists", "experts", "police", "hackers", "users", "startups", "startup",
    "companies", "governments", "government", "officials", "lawmakers", "regulators", "court", "judge", "us", "uk", "eu",
    "un", "llm", "llms", "gpu", "gpus", "chatbot", "chatbots", "is", "are", "in", "on", "for", "with", "after", "as",
    "inside", "exclusive", "update", "breaking", "opinion", "analysis", "watch", "i", "we", "they", "it",
}
_WORD = re.compile(r"(?<![\w$€£])[A-Za-z][A-Za-z0-9'’.&-]*")


def _clean(text: str) -> str:
    return _BENIGN.sub(" ", text or "")


def find_risks(text: str) -> list[tuple[str, str]]:
    """Every (category, matched words) found in `text`, one entry per distinct matched term."""
    text = _clean(text)
    seen: dict[str, str] = {}
    for label, pat in _PATTERNS:
        for m in pat.finditer(text):
            seen.setdefault(m.group(0).lower(), label)
    return [(label, term) for term, label in seen.items()]


def names_someone(headline: str, entities: dict | None = None) -> bool:
    """True when a person or organisation is named: from the model's entity list, else a capitalised
    word in the headline that is not a generic sentence starter."""
    ents = entities or {}
    if any(ents.get(k) for k in ("companies", "people")):
        return True
    # The first word of a sentence-case headline is capitalised anyway; it still counts unless it is a
    # common starter ("Study finds ..."), so "Anthropic builds ..." names Anthropic. Mixed case (xAI) counts.
    for w in _WORD.findall(headline or ""):
        core = re.split(r"['’]", w.strip("'’.-"))[0]
        if core and core.lower() not in _NOT_NAMES and any(c.isupper() for c in core):
            return True
    return False


def registrable(domain: str) -> str:
    """Publisher identity from a host name: news.example.co.uk and example.co.uk are one source."""
    host = (domain or "").lower().strip().strip(".")
    host = host.split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    parts = [p for p in host.split(".") if p]
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in {"co", "com", "org", "net", "ac", "gov", "edu", "ne", "or"}:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def independent_sources(domains: list[str]) -> int:
    return len({registrable(d) for d in domains if d}) or (1 if domains else 0)


def assess(headline: str, key_points: list | None, summary: str | None, entities: dict | None,
           domains: list[str]) -> str | None:
    """The reason a story must wait for approval, or None when it can publish."""
    if independent_sources(domains) > 1:
        return None
    if not names_someone(headline, entities):
        return None
    head = find_risks(headline)
    # Beyond the headline only the lede counts (first key point and first summary sentence), and it
    # takes two different risky words there: background mentions further down ("the new board member
    # once handled a breach") held routine news when the whole summary was scanned.
    points = [str(p) for p in (key_points or []) if p]
    first_sentence = re.split(r"(?<=[.!?])\s+", (summary or "").strip(), maxsplit=1)[0]
    body_text = " ".join([points[0] if points else "", first_sentence])
    body = [r for r in find_risks(body_text) if r[1] not in {t for _, t in head}]
    if not head and len(body) < 2:
        return None
    hits = head or body
    where = "headline" if head else "summary"
    labels = list(dict.fromkeys(label for label, _ in hits))
    terms = ", ".join(f'"{t}"' for _, t in hits[:4])
    return f"Single source; {where} mentions {' and '.join(labels[:2])} ({terms})"


# ---------------------------------------------------------------- database pass (export step)

def review(eng, since, approve: list[str]) -> tuple[list[dict], dict]:
    """Re-check every recent published or held story; flip status between the two as needed.

    Returns the held stories (for the owner's review list) and counts."""
    approved = {s.strip() for s in approve or [] if s}
    stats = {"held": 0, "newlyHeld": 0, "released": 0}
    with eng.connect() as conn:
        rows = conn.execute(
            select(db.stories.c.id, db.stories.c.slug, db.stories.c.headline, db.stories.c.key_points, db.stories.c.summary_md,
                   db.stories.c.entities, db.stories.c.status, db.stories.c.lead_article_id, db.stories.c.first_published_at)
            .where(db.stories.c.status.in_(["published", HELD]), db.stories.c.updated_at >= since)
        ).all()
        sources = {s.id: s for s in conn.execute(select(db.sources.c.id, db.sources.c.name, db.sources.c.source_type,
                                                        db.sources.c.discovered)).all()}
        ids = [r.id for r in rows]
        arts: dict[int, list] = {}
        for i in range(0, len(ids), 500):
            for a in conn.execute(
                select(db.articles.c.id, db.articles.c.story_id, db.articles.c.domain, db.articles.c.url, db.articles.c.source_id)
                .where(db.articles.c.story_id.in_(ids[i : i + 500]), db.articles.c.status == "published")
            ).all():
                arts.setdefault(a.story_id, []).append(a)

    held: list[dict] = []
    to_hold: list[int] = []
    to_release: list[int] = []
    for r in rows:
        members = arts.get(r.id, [])
        if not members:
            continue
        points = r.key_points
        if isinstance(points, str):
            try:
                points = json.loads(points)
            except ValueError:
                points = [points]
        entities = r.entities if isinstance(r.entities, dict) else {}
        reason = None if r.slug in approved else assess(r.headline, points, r.summary_md, entities, [m.domain for m in members])
        if reason:
            lead = next((m for m in members if m.id == r.lead_article_id), members[0])
            src = sources.get(lead.source_id)
            community = src is not None and (src.source_type == "community" or src.discovered)
            held.append({
                "slug": r.slug,
                "headline": r.headline,
                "source": lead.domain if (community or src is None) else src.name,
                "url": lead.url,
                "firstPublishedAt": _iso(r.first_published_at),
                "reason": reason,
            })
            if r.status != HELD:
                to_hold.append(r.id)
        elif r.status == HELD:
            to_release.append(r.id)
    if to_hold or to_release:
        with eng.begin() as conn:
            if to_hold:
                conn.execute(update(db.stories).where(db.stories.c.id.in_(to_hold), db.stories.c.status == "published").values(status=HELD))
            if to_release:
                conn.execute(update(db.stories).where(db.stories.c.id.in_(to_release), db.stories.c.status == HELD).values(status="published"))
    held.sort(key=lambda h: h["firstPublishedAt"] or "", reverse=True)
    stats.update(held=len(held), newlyHeld=len(to_hold), released=len(to_release))
    if to_hold:
        log.info("held %d new single-source stories for review", len(to_hold))
    return held, stats


def _iso(dt):
    dt = db.as_utc(dt)
    return dt.isoformat().replace("+00:00", "Z") if dt else None


# ---------------------------------------------------------------- private review file

def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def encrypt(payload, passphrase: str, iterations: int = KDF_ITERATIONS) -> dict:
    """AES-256-GCM with a key from PBKDF2-HMAC-SHA256 over the passphrase and a random salt.

    The browser reverses it with WebCrypto: importKey("raw", passphrase) -> deriveKey(PBKDF2, SHA-256,
    salt, iterations) -> decrypt(AES-GCM, iv); the 16-byte tag is appended to `data`, as WebCrypto expects."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    salt, iv = os.urandom(16), os.urandom(12)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations).derive(passphrase.encode("utf-8"))
    data = AESGCM(key).encrypt(iv, json.dumps(payload, ensure_ascii=False).encode("utf-8"), None)
    return {"v": 1, "kdf": "PBKDF2-SHA256", "iter": iterations, "salt": _b64(salt), "iv": _b64(iv), "data": _b64(data)}


def decrypt(blob: dict, passphrase: str):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    salt, iv, data = (base64.b64decode(blob[k]) for k in ("salt", "iv", "data"))
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=int(blob.get("iter") or KDF_ITERATIONS)).derive(passphrase.encode("utf-8"))
    return json.loads(AESGCM(key).decrypt(iv, data, None).decode("utf-8"))


def write_review(out_dir: Path, held: list[dict], generated_at: str, passphrase: str | None = None) -> dict:
    """held.json (public: count only) and held.enc.json (the list, encrypted) for the admin page."""
    passphrase = os.environ.get("ADMIN_REVIEW_KEY", "") if passphrase is None else passphrase
    (out_dir / "held.json").write_text(json.dumps({"count": len(held), "updatedAt": generated_at}), encoding="utf-8")
    enc = out_dir / "held.enc.json"
    if not passphrase:
        enc.unlink(missing_ok=True)  # never leave an old list behind that the page would show as current
        return {"encrypted": False}
    blob = encrypt({"generatedAt": generated_at, "stories": held}, passphrase)
    enc.write_text(json.dumps(blob), encoding="utf-8")
    return {"encrypted": True}
