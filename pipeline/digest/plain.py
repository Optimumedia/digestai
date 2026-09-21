"""Plain-language rules for AI at Work cards (/work): short, concrete words a busy owner reads in seconds.

The reader is a café owner between customers, not an engineer. Every card field that reaches a
collapsed card (the headline, "What you get", the catch) and the ones behind "How to set it up"
(what it does, what to use it for) goes through the same rules, whether the summary model wrote it
(enrich.py), the simplify pass rewrote it (simplify.py) or a stored card is exported again
(export.py through work.screen_card):

- a length limit per field (LIMITS: characters, words, items), easy to change in one place;
- one shared jargon list (JARGON), with a plain swap where one says the same thing (SWAPS:
  "workflow" -> "routine", "LLM" -> "AI model"); a field that still has jargon falls back;
- no unexplained acronyms (ACRONYMS_OK are the ones every owner knows);
- a readability check: Flesch-Kincaid grade at most READ_MAX_GRADE, no sentence over
  READ_MAX_SENTENCE_WORDS words, and few long words (readability());
- the headline formula: a verb from HEADLINE_VERBS first, then the business result, then where or with
  what; sentence case, no question, no "this" / "these" teaser, no "AI" outside a product's name, the
  tool never first (headline_ok()).

Fallbacks never invent anything: a headline is rebuilt from the card's own use_for ("Draft a week of
posts with Canva"), "What you get" from its use_for, the catch from the caveat's own first clause or
the plan and region limits it names. A card whose headline cannot be rebuilt, or that has nothing to
use it for, stays off the hub (work.card_out, "hub").

All pure functions: tested offline in tests/test_plain.py.
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------- the limits

LIMITS = {
    # 45-70 characters, 6-11 words: long enough to say the result and where, short enough to scan.
    "headline": {"min_chars": 45, "max_chars": 70, "min_words": 6, "max_words": 11},
    # A rules-built headline ("Draft a week of posts with Canva Magic Studio") may be shorter: it
    # only repeats the card's own words, and a short true line beats a padded one.
    "headline_fallback": {"min_chars": 25, "max_chars": 70, "min_words": 4, "max_words": 11},
    # One sentence to "you": so what for my week?
    "you_get": {"min_chars": 60, "max_chars": 120},
    "you_get_fallback": {"min_chars": 30, "max_chars": 120},
    # The catch, on one line.
    "watch_out": {"min_chars": 10, "max_chars": 80},
    "what_it_does": {"min_chars": 15, "max_chars": 90},
    # Three uses, each a short action.
    "use_for": {"items": 3, "max_words": 6},
}
READ_MAX_GRADE = 8.0             # Flesch-Kincaid grade, per field
READ_MAX_SENTENCE_WORDS = 20
READ_MAX_LONG_SHARE = 0.3        # words of three syllables or more (names not counted)
OVERLAP_MAX = 0.7                # "What you get" that shares this much with the headline says it twice

# The first word of a headline, and of a use a headline or an example can be built from. Imperative
# verbs an owner uses about their own work; "Follow" covers "Follow up".
HEADLINE_VERBS = {
    "draft", "reply", "turn", "get", "find", "book", "answer", "write", "plan", "track", "send", "make",
    "resize", "schedule", "summarise", "summarize", "check", "create", "fix", "save", "sell", "collect",
    "organise", "organize", "translate", "edit", "post", "build", "run", "cut", "spot", "keep", "follow",
    "stop", "handle", "prepare",
    # ...extended with the same kind of plain verb.
    "add", "ask", "automate", "call", "catch", "clean", "compare", "confirm", "convert", "design",
    "fill", "grow", "hire", "improve", "launch", "manage", "match", "measure", "move",
    "open", "pay", "personalise", "personalize", "pick", "pitch", "polish", "promote", "publish",
    "reach", "reduce", "remind", "remove", "repurpose", "research", "respond", "reuse", "review",
    "rewrite", "see", "set", "share", "show", "sort", "speed", "split", "start", "test",
    "thank", "transcribe", "update", "upload", "use", "welcome", "win", "scan", "read",
    "shoot", "film", "animate", "adjust", "crop", "sketch", "draw", "block", "protect", "avoid",
    "control", "connect", "sync", "import", "export", "merge", "boost", "lower", "raise", "earn", "spend",
    "learn", "teach", "explain", "tell", "give", "help", "let", "try", "view", "take", "look",
    "greet", "pull", "rank", "outline", "generate", "increase", "evaluate", "select", "define", "reserve",
    "apply", "expand", "refine", "gather", "produce", "deliver", "attract", "engage", "drive", "audit",
    "activate", "route", "fetch", "compose", "remember", "store", "clear", "chase", "invite", "notify",
    "discover", "identify", "detect", "verify", "prevent", "recover", "retouch", "dub", "narrate",
}
# Words that are as often a noun as a verb ("File existence checks", "Email outreach sequences",
# "List of prompts") are left out: a use that starts with one reads as a thing, not an action.

# Businesses an example line can name without an article saying so, one per job tile: a small fixed
# list a reader recognises as "someone like me". The article's own business type wins (business_in).
JOB_BUSINESS = {
    "customers": "a café",
    "content": "a hair salon",
    "sell": "a small shop",
    "support": "a trades business",
    "business": "a consultant",
}
# Business types read out of an article (or a maker's page), as they may be written there.
BUSINESS_WORDS = [
    ("a bakery", r"bakery|bakeries"), ("a café", r"caf[ée]s?|coffee shops?"), ("a restaurant", r"restaurants?"),
    ("a hair salon", r"hair salons?|salons?|barbers?|barbershops?"), ("a plumber", r"plumbers?|plumbing business"),
    ("an electrician", r"electricians?"), ("a florist", r"florists?"), ("a gym", r"gyms?|fitness studios?"),
    ("a dental practice", r"dentists?|dental practices?"), ("a clinic", r"clinics?"), ("a hotel", r"hotels?|b&bs?"),
    ("an online shop", r"online (?:shops?|stores?)|e-?commerce stores?|shopify stores?"),
    ("a boutique", r"boutiques?"), ("an agency", r"(?:marketing |creative |small )?agenc(?:y|ies)"),
    ("an accountant", r"accountants?|bookkeepers?"), ("a law firm", r"law firms?|lawyers?"),
    ("a consultant", r"consultants?|freelancers?"), ("a real estate agent", r"real estate agents?|estate agents?|realtors?"),
    ("a landscaper", r"landscapers?|gardeners?"), ("a builder", r"builders?|contractors?|tradespeople|tradesmen"),
]

# ---------------------------------------------------------------------------- jargon

# Hype and jargon a small-business owner should not have to decode, in any field. A swap below
# rewrites the ones a plain word says as well; anything still here after the swaps fails the field.
JARGON = re.compile(
    r"\b(?:llms?|large language models?|agentic|workflows?|apis?|integrations?|leverag\w*|streamlin\w*|"
    r"orchestrat\w*|multi-?modal|inference|fine[- ]?tun\w*|rag|sdks?|deploy\w*|seamless\w*|unlock\w*|"
    r"empower\w*|supercharg\w*|revolutioni[sz]\w*|game[- ]?chang\w*|powerful|effortless\w*|ai[- ]powered|"
    r"ai[- ]driven|utili[sz]\w*|robust|cutting[- ]edge|synerg\w*|next[- ]level|best[- ]in[- ]class|"
    r"paradigm\w*|generative ai|genai|10x|holistic|scalab\w*|end[- ]to[- ]end|frictionless|turnkey|"
    r"optimi[sz]ations?|mcp|embeddings?|vector|endpoints?|tokens?|context window|hallucinat\w*|"
    r"use[- ]cases?|functionalit(?:y|ies)|facilitat\w*|actionable|granular|ecosystem|omnichannel|"
    r"bleeding[- ]edge|state[- ]of[- ]the[- ]art|world[- ]class|harness\w*)\b",
    re.I)
# Talking down to the reader.
CONDESCENDING = re.compile(r"\b(?:don'?t worry|no need to worry|simply|obviously|of course|even you|"
                           r"easy peasy|anyone can)\b|!", re.I)
# A plain word that says the same thing. Order matters: longer phrases first.
_SWAP_LIST = [
    (r"\blarge language models\b", "AI models"), (r"\blarge language model\b", "AI model"),
    (r"\bllms\b", "AI models"), (r"\bllm\b", "AI model"),
    (r"\bapi integrations\b", "connections to other apps"), (r"\bapi integration\b", "connection to other apps"),
    (r"\bintegrates (?:directly |natively )?with\b", "works with"), (r"\bintegrate (?:directly |natively )?with\b", "work with"),
    (r"\bintegrations with\b", "connections to"), (r"\bintegration with\b", "connection to"),
    (r"\bintegrations\b", "connections"), (r"\bintegration\b", "connection"),
    (r"\bfinal[- ]urls?\b", "final web address"),
    (r"\bURLs\b", "web addresses"), (r"\bURL\b", "web address"),
    (r"\bworkflows\b", "routines"), (r"\bworkflow\b", "routine"),
    (r"\bleverages\b", "uses"), (r"\bleveraging\b", "using"), (r"\bleverage\b", "use"),
    (r"\butili[sz]es\b", "uses"), (r"\butili[sz]ing\b", "using"), (r"\butili[sz]e\b", "use"),
    (r"\bstreamlines\b", "simplifies"), (r"\bstreamlining\b", "simplifying"), (r"\bstreamlined\b", "simpler"),
    (r"\bstreamline\b", "simplify"),
    (r"\bseamlessly\s+", ""), (r"\bseamless\s+", ""), (r"\beffortlessly\s+", ""), (r"\beffortless\s+", ""),
    (r"\bpowerful\s+", ""), (r"\brobust\s+", ""), (r"\bcutting[- ]edge\s+", ""),
    (r"\bsupercharges\b", "boosts"), (r"\bsupercharge\b", "boost"),
    (r"\bempowers (\w+) to\b", r"lets \1"), (r"\bempower (\w+) to\b", r"let \1"),
    (r"\benables (\w+) to\b", r"lets \1"), (r"\benable (\w+) to\b", r"let \1"),
    (r"\ballows (\w+) to\b", r"lets \1"), (r"\ballow (\w+) to\b", r"let \1"),
    (r"\bdeploys\b", "sets up"), (r"\bdeploying\b", "setting up"), (r"\bdeployed\b", "set up"), (r"\bdeploy\b", "set up"),
    (r"\bdeployment\b", "setup"),
    (r"\bagentic ai\b", "AI agents"), (r"\bagentic\b", "automated"),
    (r"\bhallucinates\b", "makes things up"), (r"\bhallucinate\b", "make things up"),
    (r"\bhallucinations\b", "made-up answers"), (r"\bhallucination\b", "a made-up answer"),
    (r"\buse[- ]cases\b", "uses"), (r"\buse[- ]case\b", "use"),
    (r"\bfunctionalities\b", "features"), (r"\bfunctionality\b", "features"),
    (r"\bfacilitates\b", "helps with"), (r"\bfacilitate\b", "help with"),
    (r"\boptimal\b", "best"), (r"\bin order to\b", "to"), (r"\bcapabilities\b", "features"),
    (r"\bAI[- ]powered\s+", ""), (r"\bAI[- ]driven\s+", ""),
]
SWAPS = [(re.compile(p, re.I), r) for p, r in _SWAP_LIST]
# Acronyms every owner knows. Any other run of capitals in a field is jargon unless it is part of the
# tool's or the maker's name ("Semrush MCP" may say MCP about itself, nowhere else).
ACRONYMS_OK = {"AI", "PDF", "CRM", "SEO", "US", "USA", "UK", "EU", "EEA", "FAQ", "FAQS", "CSV", "PC", "TV", "OK",
               "PM", "AM", "VAT", "ID", "IOS", "SMS", "QR", "HR", "CEO", "B2B", "B2C", "DIY", "USD", "EUR", "GBP"}
ACRONYM = re.compile(r"(?<![\w.-])[A-Z][A-Z0-9&]{1,6}s?(?![\w-])")
# A headline never teases or asks.
HEADLINE_BANNED = re.compile(r"[?!]|\b(?:this|these|here'?s|here is|you won'?t)\b", re.I)


def _dashes(text: str) -> str:
    """The model writes non-breaking and other Unicode hyphens ("first‑token"); the rules match "-"."""
    return re.sub(r"[‐-―−]", "-", text or "")


def squash(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def swap_jargon(text: str) -> str:
    """The plain word where one says the same thing ("workflow" -> "routine"); an article before a
    swapped word is set right ("an workflow" never happens, "a AI model" does not either)."""
    text = _dashes(squash(text))
    for pattern, plain in SWAPS:
        text = pattern.sub(plain, text)
    text = re.sub(r"\b([Aa])n (routine|connection|simpl\w*|set up|boost|let|made|help|best|features?|uses?|web)\b",
                  lambda m: f"{m.group(1)} {m.group(2)}", text)
    text = re.sub(r"\b([Aa]) (AI\b)", lambda m: f"{m.group(1)}n {m.group(2)}", text)
    return squash(text)


def _mask(text: str, names=()) -> str:
    """The text with the tool's and maker's own names blanked out, so a name is never jargon."""
    for n in sorted({n for n in names if n and len(n) >= 2}, key=len, reverse=True):
        text = re.sub(re.escape(n), " ", text, flags=re.I)
    return text


def jargon_in(text: str, names=()) -> list[str]:
    """The jargon and the unknown acronyms a text still has, after the names are blanked out."""
    masked = _mask(_dashes(text or ""), names)
    found = [m.group(0) for m in JARGON.finditer(masked)]
    for m in ACRONYM.finditer(masked):
        word = m.group(0)
        if word.upper() not in ACRONYMS_OK and word.rstrip("s").upper() not in ACRONYMS_OK:
            found.append(word)
    return found


def plain_enough(text: str, names=()) -> bool:
    """No jargon, no unknown acronym and no talking down."""
    return not jargon_in(text, names) and not CONDESCENDING.search(text or "")


# ---------------------------------------------------------------------------- readability

def syllables(word: str) -> int:
    w = re.sub(r"[^a-z]", "", word.lower())
    if not w:
        return 0
    if len(w) <= 3:
        return 1
    w = re.sub(r"(?:[^laeiouy]es|[^laeiouy]ed|[^laeiouy]e)$", lambda m: m.group(0)[0], w)
    w = re.sub(r"^y", "", w)
    return max(1, len(re.findall(r"[aeiouy]+", w)))


def sentences_of(text: str) -> list[str]:
    parts = re.split(r"(?<=[.?!])\s+(?=[A-Z0-9\"“])", squash(text))
    return [p for p in parts if re.search(r"[A-Za-z]", p)]


def readability(text: str, names=()) -> dict:
    """Flesch-Kincaid grade, average and longest sentence (words), and the share of long words (three
    syllables or more). A name (the tool's, the maker's, or any capitalised word after a sentence's
    first) is read as one short word: it is not what makes a line hard to read."""
    name_words = {w.lower() for n in names if n for w in re.findall(r"[A-Za-z][A-Za-z'’-]*", n)}
    sents = sentences_of(text)
    total_words = total_syll = long_words = 0
    longest = 0
    for s in sents:
        words = re.findall(r"[A-Za-z][A-Za-z'’-]*|\d[\d,.%$€£]*", s)
        longest = max(longest, len(words))
        for i, w in enumerate(words):
            total_words += 1
            if w.lower() in name_words or (i > 0 and w[0].isupper()) or w[0].isdigit():
                total_syll += 1
                continue
            n = sum(syllables(p) for p in re.split(r"[-’']", w) if p)
            total_syll += n
            long_words += n >= 3
    if not total_words:
        return {"words": 0, "sentences": 0, "avg_sentence": 0.0, "longest": 0, "long_share": 0.0, "grade": 0.0}
    n_sent = max(1, len(sents))
    grade = 0.39 * (total_words / n_sent) + 11.8 * (total_syll / total_words) - 15.59
    return {
        "words": total_words, "sentences": n_sent, "avg_sentence": round(total_words / n_sent, 1), "longest": longest,
        "long_share": round(long_words / total_words, 2), "grade": round(grade, 1),
    }


def readable(text: str, names=()) -> bool:
    """Grade 8 or below, no sentence over 20 words, and not a pile of long words."""
    r = readability(text, names)
    return (r["grade"] <= READ_MAX_GRADE and r["longest"] <= READ_MAX_SENTENCE_WORDS
            and r["long_share"] <= READ_MAX_LONG_SHARE)


# ---------------------------------------------------------------------------- small helpers

_STOP = set(("a an the and or but of to in on for with from by at as is are be it its this that your you yours their "
             "them they can will get gets lets let into out up more less one all any every each so than then").split())


def _plain_words(text: str) -> set[str]:
    return {re.sub(r"(?:ing|ed|es|s)$", "", w) for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if len(w) > 2 and w not in _STOP}


def overlap(a: str, b: str) -> float:
    """Share of the shorter text's meaningful words the other one has (the site's wordOverlap)."""
    x, y = _plain_words(a), _plain_words(b)
    if not x or not y:
        return 0.0
    return len(x & y) / min(len(x), len(y))


def words_of(text: str) -> list[str]:
    return squash(text).split()


def verb_base(word: str) -> str | None:
    """The allow-listed verb a word is ("Drafts", "drafting", "Draft" -> "draft"), else None."""
    w = re.sub(r"[^a-z]", "", (word or "").lower())
    if not w:
        return None
    if w in HEADLINE_VERBS:
        return w
    candidates = []
    if w.endswith("ies"):
        candidates.append(w[:-3] + "y")
    if w.endswith("es"):
        candidates.append(w[:-2])
    if w.endswith("s"):
        candidates.append(w[:-1])
    if w.endswith("ing"):
        stem = w[:-3]
        candidates += [stem, stem + "e"]
        if len(stem) > 2 and stem[-1] == stem[-2]:
            candidates.append(stem[:-1])
    return next((c for c in candidates if c in HEADLINE_VERBS), None)


def to_imperative(text: str) -> str | None:
    """"drafts product captions" / "Drafting product captions" -> "Draft product captions"; None when
    the text does not start with an allow-listed verb."""
    parts = words_of(text)
    if not parts:
        return None
    base = verb_base(parts[0])
    if not base:
        return None
    return " ".join([base.capitalize(), *parts[1:]])


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def _lower_first(text: str) -> str:
    """Lower-case the first word unless it is a name ("iPhone", "Gmail" stays)."""
    if not text:
        return text
    first = text.split(" ", 1)[0]
    if re.search(r"[A-Z0-9]", first[1:]) or first in {"I"}:
        return text
    return text[:1].lower() + text[1:]


def _clauses(text: str) -> list[str]:
    """The text's first sentence, then shorter and shorter leading clauses of it."""
    first = sentences_of(text)[0] if sentences_of(text) else squash(text)
    out = [first.rstrip(".")]
    # A sentence that opens with "If ...," or "When ...," has no main clause before its comma.
    subordinate = re.match(r"(?:if|when|while|although|though|because|since|as|unless|once)\b", first, re.I)
    # Not at a bare comma: that cuts a list in half ("find prospects, enrich data").
    for sep in (r";", r"\s[—–-]\s", r":", r",\s(?:but|so|which|while|though)\b", r",\s(?=causing|making|meaning)",
                r"\s(?:but|so|while|because|although)\s"):
        m = re.search(sep, first)
        if m and m.start() >= 8 and not (subordinate and sep.startswith(",")):
            out.append(first[: m.start()].rstrip(" ,;:"))
    return list(dict.fromkeys(o.strip() for o in out if o.strip()))


def _end(text: str) -> str:
    text = text.rstrip(" ,;:")
    return text if text.endswith((".", "?", "…")) else text + "."


# Words a shortened use may not end on: a phrase cut before its object reads as a mistake.
_BOUNDARY = {"by", "and", "with", "from", "for", "to", "in", "on", "via", "through", "using", "so", "that", "which",
             "without", "while", "rather", "as", "into", "across", "within", "at", "or", "then", "when", "if"}
_TRAIL = _BOUNDARY | {"a", "an", "the", "your", "their", "its", "of", "every", "each", "all", "any", "more", "new"}


def phrase_prefixes(text: str, max_words: int, max_chars: int | None = None) -> list[str]:
    """Every leading part of a phrase, longest first, within the limits, that ends where a phrase can
    end: the whole phrase, or a part cut before a joining word ("by", "and", "with"...), never on one
    and never at a comma (that is where a list is cut in half: "prompts for ChatGPT, Claude")."""
    parts = words_of(text.rstrip("."))
    out = []
    if len(parts) <= max_words and (max_chars is None or len(" ".join(parts)) <= max_chars):
        out.append(" ".join(parts))
    for k in range(min(len(parts) - 1, max_words), 1, -1):
        head = parts[:k]
        line = " ".join(head)
        if max_chars is not None and len(line) > max_chars:
            continue
        # A new clause after a comma needs a few words of its own ("..., and fonts" is a stub).
        commas = [i for i, w in enumerate(head[:-1]) if w.endswith(",")]
        stub = bool(commas) and len(head) - 1 - commas[-1] < 3
        if (parts[k].lower().strip(",") in _BOUNDARY and not head[-1].endswith(",") and head[-1].lower() not in _TRAIL
                and not stub):
            out.append(line)
    return out


def cut_phrase(text: str, max_words: int, max_chars: int | None = None) -> str:
    """The longest of phrase_prefixes, or "" when no such part has 2+ words."""
    return next(iter(phrase_prefixes(text, max_words, max_chars)), "")


# ---------------------------------------------------------------------------- the headline

def headline_problems(text: str, names=(), tool: str = "", fallback: bool = False) -> list[str]:
    """Why a headline breaks the rules ([] when it keeps them)."""
    lim = LIMITS["headline_fallback" if fallback else "headline"]
    text = squash(text)
    words = words_of(text)
    out = []
    if not text:
        return ["empty"]
    if not (lim["min_chars"] <= len(text) <= lim["max_chars"]):
        out.append("length")
    if not (lim["min_words"] <= len(words) <= lim["max_words"]):
        out.append("words")
    if not verb_base(words[0]) or not words[0][0].isupper() or words[0].lower() != (verb_base(words[0]) or ""):
        out.append("verb first")
    first_tool = (words_of(tool) or [""])[0].lower()
    if first_tool and words[0].lower() == first_tool:
        out.append("tool first")
    if HEADLINE_BANNED.search(text):
        out.append("teaser")
    if re.search(r"\bAI\b", _mask(text, names)):
        out.append("AI")
    if not plain_enough(text, names):
        out.append("jargon")
    # Sentence case: most words after the first are lower case, apart from names.
    masked_words = words_of(_mask(text, names))[1:]
    caps = [w for w in masked_words if w[:1].isupper() and not re.search(r"[A-Z0-9]", w[1:])]
    if len(masked_words) >= 4 and len(caps) / len(masked_words) > 0.5:
        out.append("title case")
    if not readable(text, names):
        out.append("reading level")
    return out


def headline_ok(text: str, names=(), tool: str = "", fallback: bool = False) -> bool:
    return not headline_problems(text, names, tool, fallback)


def plain_headline(text, names=(), tool: str = "") -> str:
    """The model's headline with plain swaps, or "" when it breaks a rule."""
    text = squash(str(text or "")).strip("\"“”'").rstrip(".").strip()
    if not text:
        return ""
    text = _cap(swap_jargon(text))
    return text if headline_ok(text, names, tool) else ""


def fallback_headline(card: dict) -> str:
    """A headline built from the card's own uses, verb first: "Draft a week of posts with Canva Magic
    Studio". The first use that makes a headline inside the rules wins; "" when none does (the card
    then stays off the hub)."""
    tool = squash(card.get("tool"))
    names = [n for n in (tool, card.get("maker")) if n]
    for use in card.get("use_for") or []:
        line = to_imperative(swap_jargon(str(use or "")).rstrip("."))
        if not line:
            continue
        for candidate in (f"{line} with {tool}" if tool and tool.lower() not in line.lower() else "", line):
            if candidate and headline_ok(candidate, names, tool, fallback=True):
                return candidate
    return ""


# ---------------------------------------------------------------------------- what you get

def _second_person(text: str) -> bool:
    return bool(re.search(r"\byou(?:r|'ll|’ll)?\b", text, re.I)) or bool(verb_base(words_of(text)[0] if text else ""))


def you_get_problems(text: str, names=(), fallback: bool = False) -> list[str]:
    lim = LIMITS["you_get_fallback" if fallback else "you_get"]
    text = squash(text)
    out = []
    if not text:
        return ["empty"]
    if not (lim["min_chars"] <= len(text) <= lim["max_chars"]):
        out.append("length")
    if len(sentences_of(text)) > 1:
        out.append("one sentence")
    if not _second_person(text):
        out.append("to you")
    if not plain_enough(text, names):
        out.append("jargon")
    if not readable(text, names):
        out.append("reading level")
    return out


def plain_you_get(text, names=()) -> str:
    """The model's line, plain-swapped and cut back to its first sentence, or "" when it breaks a rule."""
    text = squash(str(text or "")).strip("\"“”'").strip()
    if not text or "!" in text:
        return ""
    text = swap_jargon(text)
    first = sentences_of(text)[0] if sentences_of(text) else text
    first = _end(_cap(first))
    return first if not you_get_problems(first, names) else ""


def fallback_you_get(card: dict, headline: str = "") -> str:
    """A line to "you" built only from the card's own uses: "You can draft a week of posts and resize
    one ad for five places." Uses the headline already says are skipped, so the two lines never say
    the same thing. "" when nothing usable is left."""
    names = [n for n in (card.get("tool"), card.get("maker")) if n]
    uses = []
    for use in card.get("use_for") or []:
        use = swap_jargon(str(use or "")).rstrip(".;:, ")
        if len(words_of(use)) < 3 or not plain_enough(use, names):
            continue
        if headline and overlap(use, headline) >= OVERLAP_MAX:
            continue
        uses.append(use)
    lim = LIMITS["you_get_fallback"]
    for i, use in enumerate(uses):
        verb = to_imperative(use)
        if not verb:
            continue  # "You can increase ..." only reads right from an action
        line = f"You can {_lower_first(verb)}"
        for other in uses[i + 1:]:
            other_verb = to_imperative(other)
            if not other_verb:
                continue
            longer = f"{line} and {_lower_first(other_verb)}"
            if len(longer) + 1 <= lim["max_chars"]:
                line = longer
            break
        line = _end(line)
        if not you_get_problems(line, names, fallback=True) and (not headline or overlap(line, headline) < OVERLAP_MAX):
            return line
    return ""


# ---------------------------------------------------------------------------- the catch

def watch_out_candidates(text: str) -> list[str]:
    """The caveat plain-swapped, then its first sentence, then shorter leading clauses of it."""
    text = swap_jargon(squash(text).strip("\"“”'"))
    out = [text] + _clauses(text)
    # A caveat is two clauses more often than a list: "..., and fonts may shift" ends a clause.
    m = re.search(r",\s(?:and|or)\s", out[1] if len(out) > 1 else text)
    if m and m.start() >= 8 and not re.match(r"(?:if|when|while|although|because|unless)\b", text, re.I):
        out.append((out[1] if len(out) > 1 else text)[: m.start()])
    # ...and the longest leading phrase of each that ends where a phrase can end ("Requires a Semrush
    # plan" from "Requires a Semrush plan with API units and ...").
    out += [p for o in list(out) for p in phrase_prefixes(o, 16, LIMITS["watch_out"]["max_chars"])]
    return list(dict.fromkeys(_cap(o.rstrip(".")) for o in out if o))


GENERIC_CATCH = "Check the details on the maker's page first"


def plain_watch_out(text: str, names=(), keep=None, labels: list[str] | None = None) -> str:
    """The catch on one line (at most 80 characters), plain. `keep(candidate)` says whether a shorter
    version still carries what the caveat says about who cannot use it (work.py: the same "leave for
    now" reason and the same region or plan limits); `labels` are those limits in plain words, used
    when no part of the caveat fits. Never empty: a card always shows its catch. Honesty comes before
    the reading level: a short, jargon-free part of the caveat itself beats a label."""
    lim = LIMITS["watch_out"]
    fits = lambda c: (lim["min_chars"] <= len(c) <= lim["max_chars"] and plain_enough(c, names)  # noqa: E731
                      and (keep is None or keep(c)))
    candidates = [c for c in watch_out_candidates(text) if fits(c)]
    if candidates:
        # The fullest version that fits; a shorter one only when it reads more easily and still keeps
        # most of what the fullest says ("...; not available" is not the catch any more).
        best = candidates[0]
        if not readable(best, names):
            easier = [c for c in candidates[1:] if readable(c, names) and len(c) >= 0.75 * len(best)]
            best = easier[0] if easier else best
        return best
    if labels:
        line = _cap("; ".join(labels))
        if len(line) <= lim["max_chars"] and (keep is None or keep(line)):
            return line
    return GENERIC_CATCH


# ---------------------------------------------------------------------------- what it does, uses

def plain_what(text: str, names=()) -> str:
    """What it does, in at most 90 plain characters: the whole line, its first sentence or a leading
    clause; "" when every version has jargon or is hard to read."""
    lim = LIMITS["what_it_does"]
    text = swap_jargon(squash(text).strip("\"“”'"))
    for c in [text, *_clauses(text)]:
        c = _end(_cap(c))
        if lim["min_chars"] <= len(c) <= lim["max_chars"] and plain_enough(c, names) and readable(c, names):
            return c
    shorts = [s for s in (cut_phrase(c, 16, lim["max_chars"] - 1) for c in [text, *_clauses(text)])
              if s and len(s) >= lim["min_chars"] and plain_enough(s, names)]
    for s in shorts:
        if readable(s, names):
            return _end(_cap(s))
    # Behind the fold, a plain line a little above the reading level beats none.
    for c in [text, *_clauses(text)]:
        c = _end(_cap(c))
        if lim["min_chars"] <= len(c) <= lim["max_chars"] and plain_enough(c, names):
            return c
    return _end(_cap(shorts[0])) if shorts else ""


def plain_uses(uses, names=()) -> list[str]:
    """At most three uses of at most six words each, plain, each starting with a capital."""
    lim = LIMITS["use_for"]
    out: list[str] = []
    for u in uses or []:
        u = swap_jargon(squash(str(u or "")).rstrip("."))
        if not u:
            continue
        # A gerund use ("Answering customer questions") reads as an action ("Answer customer questions").
        u = to_imperative(u) or u
        short = cut_phrase(u, lim["max_words"])
        if not short or not plain_enough(short, names):
            continue
        short = _cap(short)
        if short.lower() not in {x.lower() for x in out}:
            out.append(short)
    return out[: lim["items"]]


# ---------------------------------------------------------------------------- the example line

BUSINESS_LABELS = {label for label, _rx in BUSINESS_WORDS} | set(JOB_BUSINESS.values())

# The third label a card may carry, only when the article (or the maker's page) says so.
EASE_LABELS = ("No tech skills", "No card needed")
_EASE = [
    ("No tech skills", r"no[- ]code|no coding|without (?:writing )?(?:any )?code|no (?:technical|tech|coding) "
                       r"(?:skills?|knowledge|expertise|experience)|non-?technical users|no developer needed"),
    ("No card needed", r"no credit card|without a credit card|no card (?:is )?(?:required|needed)|"
                       r"credit card (?:is )?not required|no payment details"),
]


def ease_in(source: str) -> str:
    """"No tech skills" or "No card needed" when the source says so in as many words, else ""."""
    for label, rx in _EASE:
        if re.search(rf"\b(?:{rx})\b", source or "", re.I):
            return label
    return ""


def business_in(source: str) -> str:
    """The kind of business an article names ("a bakery"), or ""."""
    for label, rx in BUSINESS_WORDS:
        if re.search(rf"\b(?:{rx})\b", source or "", re.I):
            return label
    return ""


def scenario(card: dict, jobs: list[str], business: str = "") -> str:
    """"For example, a hair salon could use it to confirm bookings by text." Always "could": an idea,
    never a claim that someone does. The job is the card's own first use that is an action; the
    business the article's (card["business"]) or the job tile's everyday stand-in. "" when the card
    has no use to build it from."""
    who = business or card.get("business") or next((JOB_BUSINESS[j] for j in jobs if j in JOB_BUSINESS), "")
    if not who:
        return ""
    names = [n for n in (card.get("tool"), card.get("maker")) if n]
    for use in card.get("use_for") or []:
        verb = to_imperative(str(use or ""))
        if not verb or len(words_of(verb)) < 2 or not plain_enough(verb, names):
            continue
        return f"For example, {who} could use it to {_lower_first(verb).rstrip('.')}."
    return ""


# ---------------------------------------------------------------------------- the prompt's wording

def prompt_guide() -> dict[str, str]:
    """How the prompts (enrich.py, simplify.py) describe each field, from the limits above, so a limit
    changed here changes what the model is asked for too."""
    h, g, w, u = LIMITS["headline"], LIMITS["you_get"], LIMITS["watch_out"], LIMITS["use_for"]
    return {
        "headline": (f'a verb (Draft, Reply, Turn, Get, Find, Answer), the result, then where; {h["min_chars"]}-{h["max_chars"]} '
                     'characters, no "AI", e.g. "Draft customer emails in seconds, right inside Gmail"'),
        "you_get": f'what the owner gains, one sentence to "you", {g["min_chars"]}-{g["max_chars"]} characters, a number only if the article gives it',
        "what_it_does": f'max {LIMITS["what_it_does"]["max_chars"]} characters',
        "use_for": f'{u["items"]} uses, each a verb and max {u["max_words"]} words',
        "watch_out": f'the catch (limit, region, plan, risk), max {w["max_chars"]} characters, e.g. "Only in the US for now"',
        "style": "Card words: plain (grade 6-8), no jargon (API, workflow, LLM, integration, leverage).",
    }
