"""The cheap check a summary passes before it is stored.

Every figure and every named party a summary states has to be in the article it summarises. The
check is rules only - no model call, no cost, no rate limit - and it runs on the model's answer
while the article text is still in hand, which is the only moment the source is free to compare
against. quality.py runs the same idea over the published site, on headlines only; this is the
per-article version at write time, so a wrong number never reaches a page in the first place.

What it does on a mismatch, in order:
1. ask the same provider once more, naming what was wrong (`stricter_prompt`);
2. if that answer is wrong too, keep the safe part: sentences and bullets carrying the unsupported
   figure or name are dropped, an unsupported headline falls back to the source's own title, and a
   summary left too short falls back to the article's opening sentences.

Everything here is a pure function over strings, so the rules are easy to test and impossible to
make expensive. `report()` returns what was caught, which the run's stats carry to the dashboard.
"""
from __future__ import annotations

import re

from .quality import numbers_in, same_figure
from .textutil import first_sentences, word_count

MIN_SOURCE_CHARS = 200   # less source text than this cannot support or contradict anything
MIN_SUMMARY_WORDS = 60   # a repaired summary shorter than this is not worth showing
YEAR = re.compile(r"^(?:19|20)\d{2}$")
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _flat(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def _digit_figures(text: str | None) -> list[tuple[str, float]]:
    """Figures written with digits. Spelled-out numbers ("three paragraphs") and bare years are
    left out: they say nothing about the facts and produce noise, and quality.py's site-side check
    already reads headlines more strictly."""
    out = []
    for written, value in numbers_in(text or ""):
        if not any(ch.isdigit() for ch in written):
            continue
        if YEAR.match(written.strip()):
            continue
        out.append((written, value))
    return out


def unsupported_figures(claim: str | None, source: str) -> list[str]:
    """Figures in the claim that no figure in the source matches."""
    found = [v for _w, v in numbers_in(source)]
    out = []
    for written, value in _digit_figures(claim):
        if not any(same_figure(value, f) for f in found):
            out.append(written)
    return list(dict.fromkeys(out))


def unsupported_names(names, claim: str | None, source: str) -> list[str]:
    """Named parties the claim uses that the source never names. Only names the claim actually
    uses are checked: a model listing an entity it did not write about is untidy, not wrong."""
    flat_source, low_claim = _flat(source), (claim or "").lower()
    out = []
    for name in names:
        name = str(name).strip()
        if len(name) < 3 or name.lower() not in low_claim:
            continue
        if _flat(name) not in flat_source:
            out.append(name)
    return list(dict.fromkeys(out))


def _names_of(clean: dict) -> list[str]:
    ents = clean.get("entities") or {}
    return [n for kind in ("companies", "models", "people") for n in (ents.get(kind) or [])]


def report(clean: dict, source: str) -> dict:
    """What the summary states that the article does not. Empty when the article gives too little
    text to judge, so a feed-only stub never blocks a summary."""
    out: dict = {"figures": [], "names": [], "where": []}
    if len(source or "") < MIN_SOURCE_CHARS:
        return out
    headline = clean.get("headline") or ""
    body = " ".join([clean.get("summary_md") or "", " ".join(clean.get("key_points") or []),
                     clean.get("why_it_matters") or ""])
    claim = f"{headline}\n{body}"
    out["figures"] = unsupported_figures(claim, source)
    out["names"] = unsupported_names(_names_of(clean), claim, source)
    bad = out["figures"] + out["names"]
    if bad:
        if any(b.lower() in headline.lower() for b in bad):
            out["where"].append("headline")
        if any(b.lower() in body.lower() for b in bad):
            out["where"].append("summary")
    return out


def items(rep: dict) -> list[str]:
    return list(dict.fromkeys(list(rep.get("figures") or []) + list(rep.get("names") or [])))


STRICTER = """
The answer you just gave used figures or names that the article does not contain: {items}.
Write the JSON object again, with the same fields, using only figures, dates and names that appear
in the article text above, exactly as the article writes them. Leave a fact out rather than guess
it, and do not convert or round any figure. Return only the JSON object.
"""


def stricter_prompt(prompt: str, rep: dict) -> str:
    """The same prompt with one correction appended, for a single retry."""
    return prompt + STRICTER.format(items=", ".join(f'"{i}"' for i in items(rep)[:6]))


def _drop_sentences(text: str, bad: list[str]) -> str:
    kept = []
    for para in (text or "").split("\n\n"):
        sentences = [s for s in SENTENCE_SPLIT.split(para) if s.strip()]
        keep = [s for s in sentences if not any(b.lower() in s.lower() for b in bad)]
        if keep:
            kept.append(" ".join(keep))
    return "\n\n".join(kept).strip()


def safer(clean: dict, rep: dict, row, source: str) -> tuple[dict, list[str]]:
    """The summary with the unsupported parts taken out. Returns the cleaned answer and what was
    changed, in plain words, for the run's stats."""
    bad = items(rep)
    if not bad:
        return clean, []
    clean = dict(clean)
    changed: list[str] = []
    headline = clean.get("headline") or ""
    if any(b.lower() in headline.lower() for b in bad):
        title = (getattr(row, "title", None) or headline).strip()[:160]
        title = discipline_headline(title, title)[0]  # the same headline rules as our own
        if title and title != headline:
            clean["headline"] = title
            changed.append("headline replaced with the source's own title")
    summary = _drop_sentences(clean.get("summary_md") or "", bad)
    if summary != (clean.get("summary_md") or ""):
        changed.append("sentence dropped from the summary")
    if word_count(summary) < MIN_SUMMARY_WORDS:
        fallback = " ".join(first_sentences(source, 5))
        if word_count(fallback) >= word_count(summary):
            summary = fallback
            changed.append("summary replaced with the article's opening")
    clean["summary_md"] = summary
    points = [p for p in (clean.get("key_points") or []) if not any(b.lower() in p.lower() for b in bad)]
    if len(points) != len(clean.get("key_points") or []):
        changed.append("key point dropped")
    clean["key_points"] = points
    why = clean.get("why_it_matters") or ""
    if any(b.lower() in why.lower() for b in bad):
        clean["why_it_matters"] = _drop_sentences(why, bad)
        changed.append("line dropped from why it matters")
    lower_bad = {b.lower() for b in bad}
    ents = clean.get("entities") or {}
    clean["entities"] = {k: [n for n in (ents.get(k) or []) if str(n).lower() not in lower_bad]
                         for k in ("companies", "models", "people")}
    return clean, changed


# ---------------------------------------------------------------------------- headline discipline
#
# Google Discover, like a reader, rewards a plain headline that names the company and the thing and
# punishes hype and clickbait. The prompts ask for that; this is the rules-only net behind them,
# run on every generated headline (enrich, upgrade) and again at export for headlines already stored.
# It only ever takes words out or swaps one for a plainer one. Whatever it produces is checked, and a
# rewrite that would leave a broken or empty headline falls back to the source's own title (itself
# cleaned), then to the headline exactly as written. The source's hedging is never removed: a
# question mark stays whenever the source title hedges, or when there is no title to compare with.

# Hype words and their plain replacement ("" drops the word, an adjective that adds nothing).
_HYPE = [
    (re.compile(r"\brevolutioni[sz]es\b", re.I), "changes"),
    (re.compile(r"\brevolutioni[sz]ing\b", re.I), "changing"),
    (re.compile(r"\brevolutioni[sz]ed\b", re.I), "changed"),
    (re.compile(r"\brevolutioni[sz]e\b", re.I), "change"),
    (re.compile(r"\bunleashes\b", re.I), "releases"),
    (re.compile(r"\bunleashing\b", re.I), "releasing"),
    (re.compile(r"\bunleashed\b", re.I), "released"),
    (re.compile(r"\bunleash\b", re.I), "release"),
    (re.compile(r"\bgame[- ]?changers\b", re.I), "major shifts"),
    (re.compile(r"\bgame[- ]?changer\b", re.I), "major shift"),
]
# Hype adjectives that add nothing: dropped, with the "a"/"an" before them fixed for the next word.
_HYPE_ADJ = re.compile(r"\b(?:(an?)\s+)?(?:game[- ]?changing|revolutionary|groundbreaking|ground-breaking|"
                       r"mind-blowing|jaw-dropping|earth-shattering|mind-bending)\s+(?=(\S+))", re.I)
# Clickbait frames: (pattern, replacement). Order matters: the whole-phrase frames go first.
_BAIT = [
    # "BREAKING:", "Just in -", "Exclusive |" in front of the headline.
    (re.compile(r"^(?:breaking(?: news)?|just in|exclusive|urgent|must read)\s*[:|\-–—]\s*", re.I), ""),
    (re.compile(r"^you won[’']?t believe\s+", re.I), ""),
    (re.compile(r"[\s,;:.\-–—]*\b(?:and )?you won[’']?t believe\b.*$", re.I), ""),
    (re.compile(r"^everything you need to know about\s+", re.I), "What we know about "),
    (re.compile(r"[\s,;:.\-–—(]*\b(?:here[’']?s |here is )?(?:everything|all|what) you need to know(?: about (?:it|this|that|them))?[)\s.!]*$", re.I), ""),
    (re.compile(r"^everything you need to know\s*[:\-–—]\s*", re.I), ""),
    (re.compile(r"^here[’']?s (why|how|what)\s+", re.I), r"\1 "),
    (re.compile(r"^here is (why|how|what)\s+", re.I), r"\1 "),
    # A trailing "here's why" needs punctuation or "and"/"but" before it: "Study shows this is how
    # models learn" is a sentence, not a frame.
    (re.compile(r"\s*(?:[,;:.–—(]|\s-)\s*(?:and |but )?(?:here[’']?s|here is|this is) (?:why|how|what)\b[^.?!]{0,40}[).?!]*$", re.I), ""),
    (re.compile(r"\s+(?:and|but) here[’']?s (?:why|how|what)\b[^.?!]{0,40}[).?!]*$", re.I), ""),
    (re.compile(r"[\s,;:.\-–—]+(?:and )?(?:it |this |that )?will (?:shock|stun|surprise|amaze) you\b.*$", re.I), ""),
    (re.compile(r"[\s,;:.\-–—]+(?:and )?(?:it[’']?s|this is) (?:huge|insane|wild|crazy)\b[.!]*$", re.I), ""),
]
# Written in capitals on purpose. Short all-capital words are treated as acronyms (GPU, NASA, META
# the ticker) unless they are in _SHOUT; a word of six or more capitals is shouting unless listed here.
_ACRONYMS = {"NVIDIA", "NASDAQ", "UNESCO", "UNICEF", "FEDRAMP", "SPARQL", "FORTRAN", "OPENCV", "SCOTUS",
             "POTUS", "FLOTUS", "ARPANET", "CAPTCHA", "ERCOT"}
_SHOUT = {"BREAKING", "HUGE", "NEW", "FREE", "WOW", "OMG", "MUST", "NEVER", "EVER", "BIG", "NOW", "JUST",
          "BEST", "WORST", "SHOCKING", "EXCLUSIVE", "URGENT", "MASSIVE", "INSANE", "CRAZY", "AMAZING",
          "WARNING", "ALERT", "STOP", "REALLY", "VERY", "THIS", "THAT", "NOT", "YOU", "YOUR", "EVERYTHING",
          "SECRET", "TRUTH", "INCREDIBLE", "EPIC", "WILD", "FINALLY", "OFFICIAL", "OFFICIALLY", "LIVE"}
_CAPS_WORD = re.compile(r"(?<![\w’'-])([A-Z][A-Z’']{2,})(?![\w’'-])")
# Brands spelled with an exclamation mark.
_BANG_NAMES = re.compile(r"\b(Yahoo|Jeopardy|Wham)!", re.I)
_QUESTION_WORDS = {"who", "what", "when", "where", "why", "how", "which", "whose", "whom", "is", "are", "was",
                   "were", "do", "does", "did", "can", "could", "will", "would", "should", "shall", "may",
                   "might", "has", "have", "had", "must", "isn't", "aren't", "won't", "can't", "doesn't",
                   "don't", "didn't", "wasn't", "shouldn't", "wouldn't", "couldn't", "am"}
_OPINION = re.compile(r"^(opinion|analysis|explainer|review|interview|podcast)\s*:\s*", re.I)
MIN_HEADLINE_CHARS = 12
MIN_HEADLINE_WORDS = 3


def _first_words(text: str) -> list[str]:
    return re.sub(r"^[\"“‘'(\[]+", "", text.strip()).lower().replace("’", "'").split()


def real_question(headline: str) -> bool:
    """Whether a headline that ends in "?" asks something: its opening, or the clause the question
    mark closes ("Nvidia's new chip: can it beat AMD?"), starts with a question word."""
    body = _OPINION.sub("", headline.strip())
    clauses = [body] + [c for c in re.split(r"[:;—–]|\s-\s|,", body)[1:]]
    return any((_first_words(c) or [""])[0] in _QUESTION_WORDS for c in clauses if c.strip())


def _article(word: str) -> str:
    """"a" or "an" before a word (vowel sound, rough but right for news headlines)."""
    w = word.strip("\"“‘'(")
    if not w:
        return "a"
    if w.isupper() and len(w) <= 5 and w.isalpha():  # an AI, an LLM, an FTC, a GPU
        return "an" if w[0] in "AEFHILMNORSX" else "a"
    low = w.lower()
    if low.startswith(("uni", "use", "usu", "eu", "one", "once")):
        return "a"
    if low.startswith(("hour", "honest", "heir")):
        return "an"
    return "an" if low[0] in "aeiou" else "a"


def _drop_adjective(m: re.Match) -> str:
    art = m.group(1)
    if not art:
        return ""
    new = _article(m.group(2))
    return (new.capitalize() if art[0].isupper() else new) + " "


def _title_case_heavy(text: str) -> bool:
    words = [w for w in re.findall(r"[A-Za-z][a-z’']{3,}", text)]
    return bool(words) and sum(w[0].isupper() for w in words[1:]) >= max(1, (len(words) - 1) * 0.6)


def _tidy(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s+([,;:.!?])", r"\1", text)
    text = re.sub(r"^[\s,;:.\-–—|]+", "", text)
    text = re.sub(r"[\s,;:\-–—|(]+$", "", text)
    if text and text[0].islower():
        text = text[0].upper() + text[1:]
    return text


def usable_headline(text: str | None) -> bool:
    """A headline that can go on a page: enough words, starts and ends cleanly, brackets and quotes balanced."""
    t = (text or "").strip()
    if len(t) < MIN_HEADLINE_CHARS or len(t.split()) < MIN_HEADLINE_WORDS or not re.search(r"[A-Za-z]", t):
        return False
    if not re.match(r"[\w\"“‘'(\[$£€]", t) or re.search(r"[,;:\-–—(\[|]$", t):
        return False
    if t.count("(") != t.count(")") or t.count("[") != t.count("]") or t.count('"') % 2 or t.count("“") != t.count("”"):
        return False
    return True


def _rules(headline: str, title_hedges: bool) -> tuple[str, list[str]]:
    """Apply every rule once; returns the text and what changed."""
    h, changed = headline, []

    def swap(pattern, repl, label):
        nonlocal h
        new = pattern.sub(repl, h)
        if new != h:
            h = new
            if label not in changed:
                changed.append(label)

    # Exclamation marks first (a frame after "!!!" is then found like one after a full stop): none in
    # a headline, except inside a brand's own name. One that ends a sentence inside the headline
    # becomes a full stop; any other is dropped.
    protected = _BANG_NAMES.sub(lambda m: m.group(1) + "\x00", h)
    if "!" in protected:
        protected = re.sub(r"\?!+|!+\?", "?", protected)
        protected = re.sub(r"!+(?=\s+[A-Z0-9])", ".", protected)
        protected = protected.replace("!", "")
        changed.append("exclamation mark removed")
    h = protected.replace("\x00", "!")
    h = re.sub(r"\?{2,}", "?", h)
    # A trailing ellipsis is a cliffhanger, not a headline.
    if re.search(r"(?:\.\.\.|…)\s*$", h):
        h = re.sub(r"\s*(?:\.\.\.|…)\s*$", "", h)
        changed.append("trailing ellipsis removed")

    for pat, repl in _BAIT:
        swap(pat, repl, "clickbait phrase removed")
    for pat, repl in _HYPE:
        def keep_case(m, repl=repl):
            if repl and m.group(0)[:1].isupper():
                return repl[:1].upper() + repl[1:]
            return repl
        swap(pat, keep_case, "hype word replaced")
    swap(_HYPE_ADJ, _drop_adjective, "hype word replaced")

    # Words in capitals that are not acronyms.
    title_case = _title_case_heavy(h)

    def calm(m):
        w = m.group(1)
        bare = w.replace("’", "").replace("'", "")
        if bare in _ACRONYMS or (bare not in _SHOUT and len(bare) < 6):
            return w
        if m.start() == 0 or title_case:
            return w[0] + w[1:].lower()
        return w.lower()

    new = _CAPS_WORD.sub(calm, h)
    if new != h:
        h = new
        changed.append("capitals lowered")

    # More than one colon (the "Opinion:" label and times like 10:30 aside): the later ones become dashes.
    label = _OPINION.match(h)
    head, body = (h[:label.end()], h[label.end():]) if label else ("", h)
    colons = [m.start() for m in re.finditer(r":", body)
              if not (m.start() > 0 and body[m.start() - 1].isdigit() and body[m.start() + 1:m.start() + 2].isdigit())]
    if len(colons) > 1:
        for pos in reversed(colons[1:]):
            body = body[:pos].rstrip() + " —" + body[pos + 1:]
        h = head + body
        changed.append("extra colons replaced")

    # A question mark on a statement. Kept whenever the source hedges: there it is the hedge.
    stripped = h.rstrip()
    if stripped.endswith("?") and not real_question(stripped) and not title_hedges:
        h = stripped.rstrip("?").rstrip()
        changed.append("question mark on a statement removed")

    return _tidy(h), changed


def discipline_headline(headline: str | None, title: str | None = None) -> tuple[str, list[str]]:
    """The headline with hype words, clickbait frames, shouting and stray punctuation taken out.

    Returns (headline, what changed). Never returns something worse than it was given: when the
    rules would leave a broken headline, the source's own title (cleaned the same way) is used, and
    if that is no good either the headline comes back exactly as written.
    """
    original = (headline or "").strip()
    if not original:
        return original, []
    hedges = True
    if title and title.strip():
        from .enrich import title_hedges as _title_hedges  # enrich imports this module

        hedges = _title_hedges(title)
    fixed, changed = _rules(original, hedges)
    if not changed:
        return original, []
    if usable_headline(fixed) and len(fixed) >= 0.4 * len(original):
        return fixed, changed
    if title and title.strip() and title.strip() != original:
        alt, _ = _rules(title.strip()[:160], hedges)
        if usable_headline(alt):
            return alt, ["headline replaced with the source's own title"]
    return original, []


def note(row, rep: dict, changed: list[str], retried: bool) -> dict:
    """One line for the dashboard: what was caught on which article, and what happened to it."""
    return {"id": getattr(row, "id", None),
            "headline": (getattr(row, "title", None) or "")[:120],
            "items": items(rep)[:4],
            "where": ", ".join(rep.get("where") or []) or "summary",
            "fixed": "retry" if retried and not changed else ("edited" if changed else "kept"),
            "changed": changed[:3]}
