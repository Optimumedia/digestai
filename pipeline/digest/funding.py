"""Rules for the Funding tracker: which extracted funding mentions are real deals, and when two
rows are the same deal.

The summary model extracts a "funding" object from any article that mentions money around an AI
company, so the raw rows include the same round reported by seventy outlets with amounts that drift
from the headline figure to the valuation, product launches labelled "acquisition", and last year's
deal retold in a profile. The page should list each deal once, with the figures the coverage agrees
on, and a total that adds those rows only.

Modelled on the Models rules (trackers.py): a row must be about a deal (funding words in the
headline, no launch words without them), its amount must appear in the article's own text, an
"acquisition" needs the headline to say so, the article must be from the story's own time, and rows
for the same company with amounts within 25% of each other are one deal.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime

from .trackers import org_key

ROUNDS = {"seed", "series_a", "series_b", "series_c", "series_d_plus", "acquisition", "ipo", "debt", "other"}
UNKNOWN = {"", "unknown", "none", "n/a", "null", "undisclosed"}
AMOUNT_TOLERANCE = 0.25      # amounts this close (of the larger) are the same figure
SAME_DEAL_DAYS = 30          # a company's rows this close in time are the same deal
STORY_DATE_DAYS = 10         # an article this much older than its story is not that story's news
MAX_AMOUNT_USD = 200e9       # no single round or purchase is bigger; a larger figure is a mistake
MAX_VALUATION_USD = 10e12
# Approximate rates, only to recognise "€3B" as the model's "3300000000": the tolerance absorbs the rest.
RATES = {"€": 1.15, "eur": 1.15, "euros": 1.15, "£": 1.3, "gbp": 1.3, "pounds": 1.3}

FUNDING_WORDS = re.compile(
    r"\b(?:rais(?:e|es|ed|ing)|secur(?:e|es|ed|ing)|clos(?:e|es|ed|ing)|land(?:s|ed)|nab(?:s|bed)|bag(?:s|ged)|nets?|"
    r"funding|fundrais(?:e|es|ed|ing)|financing|investment|invest(?:s|ed|ing)|investors?|valuation|valued|valuable|worth|"
    r"round|series [a-f]\b|seed|backs|backed|backing|acqui(?:res?|red|ring|sition)|buys?|bought|to buy|purchas(?:e|es|ed)|"
    r"takeover|snaps up|merger|merges?|ipo|goes public|go public|files to list|debt|led by|unicorn|stake|"
    r"in talks|term sheet|capital)\b", re.I)
ACQUISITION_WORDS = re.compile(
    r"\b(?:acqui(?:re|res|red|ring|sition)|buys|bought|to buy|purchas(?:e|es|ed)|takeover|snaps up|merger|merges?|"
    r"deal to (?:buy|acquire))\b", re.I)
LAUNCH_WORDS = re.compile(
    r"\b(?:launch(?:es|ed)?|unveil(?:s|ed)?|introduc(?:e|es|ed)|debut(?:s|ed)?|releas(?:e|es|ed)|roll(?:s|ed)? out|"
    r"ships?|shipped|partner(?:s|ed|ship)?|deploy(?:s|ed)|opens?|hires?|appoints?|reviews?|hands-on|expands?|"
    r"updates?|adds|announc(?:e|es|ed)|now available|available)\b", re.I)
# "... raised $116M in 2023": a deal from another year, retold.
PAST_YEAR_DEAL = re.compile(
    r"\b(?:rais(?:ed|ing)|acquired|bought|closed|secured|valued|funding|round|investment|deal)\b[^.\n]{0,80}?"
    r"\b(?:in|back in|during|since|from)\s+((?:19|20)\d{2})\b", re.I)
LAST_YEAR = re.compile(r"\b(?:last|previous) year\b", re.I)
# Not a done deal: "Nvidia considers $10B investment", "ahead of potential $3.5B round".
SPECULATIVE = re.compile(
    r"\b(?:consider(?:s|ing)?|weigh(?:s|ing)?|mull(?:s|ing)?|eye(?:s|ing)|plan(?:s|ning)? to|potential|possible|"
    r"in talks|talks to|seek(?:s|ing)|could|may|might|reportedly|rumou?r(?:s|ed)?|expected to|aims? to|looking to|"
    r"ahead of|would|delay(?:s|ed)?|postpone(?:s|d)?|shelve(?:s|d)?)\b", re.I)
IPO_WORDS = re.compile(r"\b(?:ipo|goes public|go public|went public|listing|lists? on|files to list|debut(?:s|ed)? on)\b", re.I)
MONEY = re.compile(
    r"(?:(?P<cur>[$€£]|us\$|usd|eur|gbp)\s*)?(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*"
    r"(?P<unit>trillion|billion|million|thousand|tn|bn|mn|[tbmk])?\b(?:\s*(?P<cur2>dollars|euros|pounds|usd|eur|gbp))?", re.I)
UNITS = {"trillion": 1e12, "tn": 1e12, "t": 1e12, "billion": 1e9, "bn": 1e9, "b": 1e9,
         "million": 1e6, "mn": 1e6, "m": 1e6, "thousand": 1e3, "k": 1e3}


def company_key(name: str | None) -> str:
    """Same company, same key: "Mistral AI", "Mistral" and "MISTRAL AI Inc." (trackers.org_key)."""
    return org_key(name)


def money_in(text: str) -> list[float]:
    """Every amount the text states, in US dollars: "€3B" gives 3.45e9, "$116 million" 1.16e8.
    A bare number without a currency or a scale word is not money."""
    out = []
    for m in MONEY.finditer(text or ""):
        cur, unit = (m.group("cur") or m.group("cur2") or "").lower(), (m.group("unit") or "").lower()
        if not cur and not unit:
            continue
        if not cur and unit in ("t", "b", "m", "k"):
            continue  # "3 B" without a currency is a size or a grade, not money
        try:
            value = float(m.group("num").replace(",", "")) * UNITS.get(unit, 1.0)
        except ValueError:
            continue
        out.append(value * RATES.get(cur, 1.0))
        if cur in RATES:
            out.append(value)  # the model may have stored the figure unconverted
    return out


def close(a: float | None, b: float | None, tolerance: float = AMOUNT_TOLERANCE) -> bool:
    if not a or not b or a <= 0 or b <= 0:
        return False
    return abs(a - b) / max(a, b) <= tolerance


def amount_in_text(amount: float | None, text: str) -> bool:
    return any(close(amount, v) for v in money_in(text))


def _past_year(text: str, now: datetime) -> bool:
    if LAST_YEAR.search(text or ""):
        return True
    return any(int(y) < now.year for y in PAST_YEAR_DEAL.findall(text or ""))


def _dt(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None


def is_deal(f: dict, headline: str, text: str, now: datetime,
            article_date: str | None = None, story_date: str | None = None) -> str | None:
    """None when this looks like a real deal reported in this story; otherwise why it is not.
    `headline` is the story headline and the article's own title; `text` the article's key points,
    summary and description (where the amount must appear)."""
    company = str(f.get("company") or "").strip()
    if company.lower() in UNKNOWN or not company_key(company):
        return "no company"
    rnd = str(f.get("round") or "other").strip().lower()
    head = headline or ""
    # The deal must be the story's news: its company named in the headline, and done, not planned.
    words = [w for w in re.findall(r"[a-z0-9]+", company.lower()) if len(w) >= 3 and w not in {"inc", "ltd", "the", "labs"}]
    if words and not any(re.search(rf"\b{re.escape(w)}", head, re.I) for w in words):
        return "company not named in the headline"
    if SPECULATIVE.search(head):
        return "not a done deal"
    if rnd == "ipo" and not IPO_WORDS.search(head):
        return "called an IPO, but the headline does not say so"
    if not FUNDING_WORDS.search(head):
        if LAUNCH_WORDS.search(head):
            return "headline is about a product, not a deal"
        if not FUNDING_WORDS.search(text or ""):
            return "nothing about a deal in the text"
    if rnd == "acquisition" and not ACQUISITION_WORDS.search(head):
        return "called an acquisition, but the headline does not say so"
    amount = f.get("amount_usd")
    if amount is not None:
        if not (0 < float(amount) <= MAX_AMOUNT_USD):
            return "implausible amount"
        if not amount_in_text(float(amount), f"{head} {text or ''}"):
            return "amount not in the article's text"
        valuation = f.get("valuation_usd")
        if valuation and rnd != "acquisition" and float(amount) >= 0.8 * float(valuation):
            return "amount is the valuation"
    a_dt, s_dt = _dt(article_date), _dt(story_date)
    if a_dt and s_dt and abs((a_dt - s_dt).days) > STORY_DATE_DAYS:
        return "article is not from the story's time"
    if _past_year(f"{head}\n{text or ''}", now):
        return "a deal from an earlier year"
    return None


def clean_row(f: dict) -> dict:
    """The row as the page shows it: known round names, plausible figures, a trimmed investor list."""
    rnd = str(f.get("round") or "other").strip().lower()
    valuation = f.get("valuation_usd")
    valuation = float(valuation) if valuation and 0 < float(valuation) <= MAX_VALUATION_USD else None
    amount = f.get("amount_usd")
    return {"company": str(f.get("company") or "").strip(), "amount_usd": float(amount) if amount else None,
            "round": rnd if rnd in ROUNDS else "other",
            "investors": [str(i).strip() for i in (f.get("investors") or []) if str(i).strip()][:10],
            "valuation_usd": valuation}


def _vote(values, prefer=None):
    """The most common value, ties to the preferred one, then to the first seen."""
    values = [v for v in values if v not in (None, "")]
    if not values:
        return None
    counts = Counter(values)
    best = max(counts.values())
    if prefer is not None and counts.get(prefer) == best:
        return prefer
    return next(v for v in values if counts[v] == best)


def _majority(members: list[dict]) -> list[dict]:
    """One story's rows for one company are one deal: the amount most of them state wins (ties to
    the lead article's, then the first), rows with another figure are the coverage's mistakes, and
    rows without an amount go along."""
    ordered = sorted(members, key=lambda m: (not m.get("isLead"), m.get("date") or "~"))
    clusters: list[list[dict]] = []
    without = []
    for m in ordered:
        if not m.get("amount_usd"):
            without.append(m)
            continue
        for cl in clusters:
            if close(m["amount_usd"], cl[0]["amount_usd"]):
                cl.append(m)
                break
        else:
            clusters.append([m])
    best = max(clusters, key=len) if clusters else []  # max keeps the first of equals: the lead's
    return best + without


def fold(cands: list[dict]) -> list[dict]:
    """One row per deal. Within a story, a company's rows are one deal decided by majority
    (_majority). Across stories, a company's deals merge when their amounts are within the tolerance
    (or one has none) and they are within SAME_DEAL_DAYS of each other. Figures are then decided by
    majority over the merged rows, ties to the lead article, then the first seen."""
    by_story: dict[tuple[str, str | None], list[dict]] = {}
    for c in cands:
        by_story.setdefault((company_key(c["company"]), c.get("storySlug")), []).append(c)
    groups = [{"key": key, "members": _majority(ms)} for (key, _slug), ms in by_story.items()]
    groups.sort(key=lambda g: min((m.get("date") or "~" for m in g["members"]), default="~"))
    deals: list[dict] = []
    for g in groups:
        g_amount = next((m["amount_usd"] for m in g["members"] if m.get("amount_usd")), None)
        g_dt = _dt(next((m["date"] for m in g["members"] if m.get("date")), None))
        home = None
        for d in deals:
            if d["key"] != g["key"]:
                continue
            d_amount = next((m["amount_usd"] for m in d["members"] if m.get("amount_usd")), None)
            if g_amount and d_amount and not close(g_amount, d_amount):
                continue
            d_dt = _dt(next((m["date"] for m in d["members"] if m.get("date")), None))
            if g_dt and d_dt and abs((g_dt - d_dt).days) <= SAME_DEAL_DAYS:
                home = d
                break
        if home is None:
            deals.append(g)
        else:
            home["members"].extend(g["members"])
    out = []
    for d in deals:
        ms = d["members"]
        lead = next((m for m in ms if m.get("isLead")), ms[0])
        investors = max((m.get("investors") or [] for m in ms), key=len)
        out.append({
            "company": _vote([m["company"] for m in ms], lead["company"]),
            "amount_usd": _vote([m.get("amount_usd") for m in ms], lead.get("amount_usd")),
            "round": _vote([m.get("round") for m in ms], lead.get("round")) or "other",
            "investors": investors,
            "valuation_usd": _vote([m.get("valuation_usd") for m in ms], lead.get("valuation_usd")),
            "date": min((m["date"] for m in ms if m.get("date")), default=None),
            "storySlug": lead.get("storySlug"),
            "storyHeadline": lead.get("storyHeadline"),
            "sources": len(ms),
        })
    return out


def build(stories: list[dict], now: datetime) -> tuple[list[dict], dict]:
    """The Funding tracker rows from the exported stories (export.py's stories_out), newest first,
    and how many raw extractions each rule dropped."""
    cands, dropped = [], Counter()
    for st in stories:
        for a in st.get("articles") or []:
            f = a.get("funding")
            if not f or not f.get("company"):
                continue
            head = f"{st.get('headline') or ''} | {a.get('title') or ''} | {a.get('headline') or ''}"
            text = " ".join([*(a.get("keyPoints") or []), a.get("summaryMd") or "", a.get("description") or ""])
            why = is_deal(f, head, text, now, a.get("publishedAt"), st.get("firstPublishedAt"))
            if why:
                dropped[why] += 1
                continue
            cands.append({**clean_row(f), "date": a.get("publishedAt"), "isLead": bool(a.get("isLead")),
                          "storySlug": st.get("slug"), "storyHeadline": st.get("headline")})
    rows = fold(cands)
    rows.sort(key=lambda r: r["date"] or "", reverse=True)
    return rows, dict(dropped)


def total(rows: list[dict]) -> float:
    return float(sum(r["amount_usd"] or 0 for r in rows))
