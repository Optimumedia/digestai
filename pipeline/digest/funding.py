"""Rules for the Funding tracker: which extracted funding mentions are real deals, and when two
rows are the same deal.

The summary model extracts a "funding" object from any article that mentions money around an AI
company, so the raw rows include the same round reported by seventy outlets with amounts that drift
from the headline figure to the valuation, product launches labelled "acquisition", and last year's
deal retold in a profile. The page should list each deal once, with the figures the coverage agrees
on, and a total that adds those rows only.

Modelled on the Models rules (trackers.py). The row links to its story, so the story's own headline
decides: it names the company, says a deal was done (not planned, filed, rumoured, blocked or
approved), and is about money changing hands, not revenue, losses, contracts, data-centre leases or
bond sales. Only the headline's main clause counts ("... buys Applied Compute for $4.5B as earnings
top estimates" is a deal; "Meta forms sales team amid $145 billion spending" is not). The amount must
appear in the article's own text, and when the headline itself states the round or the price
("Sierra finalizes $1.75B Series F"), that figure wins. An acquisition is listed under the company
bought, with the buyer as the investor. Rows for the same company with amounts within 25% of each
other, close in time, are one deal.

The audit of 26 Nov 2026 (one row in four of the live /funding page) found 23 of 96 rows wrong under
the earlier rules, nearly all from story headlines that were about something else: the rules above
are the fix, and tests/test_funding.py holds those rows.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

from . import config
from .trackers import ORG_WORDS, _plain, org_key

log = logging.getLogger("digest.funding")

ROUNDS = {"seed", "series_a", "series_b", "series_c", "series_d_plus", "acquisition", "ipo", "debt", "other"}
UNKNOWN = {"", "unknown", "none", "n/a", "null", "undisclosed"}
# A group of companies, or a placeholder, where one company's name belongs.
NOT_A_COMPANY = re.compile(r"\b(?:unnamed|undisclosed|unknown|several|various|startups|companies|firms|investors)\b", re.I)
AMOUNT_TOLERANCE = 0.25      # amounts this close (of the larger) are the same figure
SAME_DEAL_DAYS = 30          # a company's rows this close in time are the same deal
SAME_FIGURE = 0.02           # the very same figure ...
SAME_FIGURE_DAYS = 90        # ... this close in time is the same deal retold
STORY_DATE_DAYS = 10         # an article this much older than its story is not that story's news
MAX_AMOUNT_USD = 200e9       # no single round or purchase is bigger; a larger figure is a mistake
MAX_VALUATION_USD = 10e12
# Approximate rates, only to recognise "€3B" as the model's "3300000000": the tolerance absorbs the rest.
RATES = {"€": 1.15, "eur": 1.15, "euros": 1.15, "£": 1.3, "gbp": 1.3, "pounds": 1.3,
         "rmb": 0.14, "cny": 0.14, "yuan": 0.14, "cad": 0.72, "c$": 0.72, "a$": 0.65, "aud": 0.65,
         "chf": 1.25, "francs": 1.25, "inr": 0.012, "₹": 0.012, "rupees": 0.012, "sek": 0.095, "jpy": 0.0066, "yen": 0.0066,
         "krw": 0.00072, "won": 0.00072}
# How the page writes a figure stated in another currency.
CURRENCY_LABEL = {"€": "€", "eur": "€", "euros": "€", "£": "£", "gbp": "£", "pounds": "£", "rmb": "RMB ", "cny": "RMB ",
                  "yuan": "RMB ", "cad": "CAD ", "c$": "CAD ", "a$": "AUD ", "aud": "AUD ", "chf": "CHF ", "francs": "CHF ",
                  "inr": "₹", "₹": "₹", "rupees": "₹", "sek": "SEK ", "jpy": "¥", "yen": "¥", "krw": "KRW ", "won": "KRW "}

FUNDING_WORDS = re.compile(
    r"\b(?:rais(?:e|es|ed|ing)|(?:secur(?:es|ed|ing)|clos(?:es|ed|ing)|land(?:s|ed)|nab(?:s|bed)|bag(?:s|ged)|nets|gets|receives)\s+"
    r"(?:about |over |more than |nearly |almost |up to |roughly |an? )?(?:[$€£₹]|rmb|cny|cad|c\$|us\$|\d)|closes (?:its |a |an )?\w+ round|"
    r"funding|fundrais(?:e|es|ed|ing)|financing|investment|invest(?:s|ed|ing)|investors?|valuation|valued|valuable|worth|"
    r"round|series [a-f]\b|seed|backs|backed|backing|acqui(?:res?|red|ring|sition)|buys?|bought|to buy|purchas(?:e|es|ed)|"
    r"takeover|snaps up|merger|merges?|ipo|goes public|go public|files to list|debt|loan|led by|unicorn|stake|"
    r"in talks|term sheet|capital|finaliz(?:e|es|ed)|finalis(?:e|es|ed))\b", re.I)
ACQUISITION_WORDS = re.compile(
    r"\b(?:acqui(?:re|res|red|ring|sition)|buys|bought|to buy|purchas(?:e|es|ed)|takeover|snaps up|merger|merges?|"
    r"deal to (?:buy|acquire))\b", re.I)
LAUNCH_WORDS = re.compile(
    r"\b(?:launch(?:es|ed)?|unveil(?:s|ed)?|introduc(?:e|es|ed)|debut(?:s|ed)?|releas(?:e|es|ed)|roll(?:s|ed)? out|"
    r"ships?|shipped|partner(?:s|ed|ship)?|deploy(?:s|ed)|opens?|hires?|appoints?|reviews?|hands-on|expands?|"
    r"updates?|adds|announc(?:e|es|ed)|now available|available)\b", re.I)
# Money that is not a round or a purchase: results, losses, contracts, leases, capacity, bonds, and
# steps around a deal done before (an approval, a probe, a block).
NOT_DEAL = re.compile(
    r"\b(?:earnings|quarter(?:ly)?|q[1-4]|fiscal|net (?:loss|income)|losses|capex|"
    r"market value|stock price|price target|buyback|(?:in|commercial|government|federal|defen[cs]e|cloud) contracts?|"
    r"contracts? (?:worth|valued|hit|hits|total\w*)|wins? (?:a |an )?[\w$.-]* ?contract|signs?|signed|leases?|licens(?:e|es|ed)|"
    r"\d+\s?(?:mw|gw)|megawatts?|gigawatts?|bonds?|senior notes|notes offering|credit rating|"
    r"bankruptcy|chapter 11|shuts? down|layoffs?|charge|warrants?|grid|"
    r"approv(?:e|es|ed|al)|clear(?:s|ed)|blocks?|blocked|scraps?|scrapped|called off|terminat(?:e|es|ed)|"
    r"inquir(?:y|es|ed)|probe|investigat(?:e|es|ion)|antitrust|predicts?|forecasts?)\b", re.I)
# Words that are not a deal unless the headline also states the round or price for the company
# ("Fal raises $1.3B ... sources say $800M annualized revenue" is a round).
NOT_DEAL_SOFT = re.compile(
    r"\b(?:revenue|revenues|loss|profit|capacity|sales|"
    r"shares? (?:fall|falls|fell|drop|drops|dropped|plunge|plunges|rise|rises|rose|jump|jumps|surge|surges|slide|slides|soar|soars))\b", re.I)
# A valuation in a headline ("at $1.6B valuation", "valued at $300 million"), not the round.
VALUATION_FIGURE = re.compile(r"\b(?:at|valued at|valuation of|worth|to reach|pre-money|post-money)\s+(?:about |nearly |over |a |an |~)?\S*\d\S*"
                              r"(?:\s+(?:million|billion|trillion))?|\S*\d\S*(?:\s+(?:million|billion|trillion))?\s+(?:valuation|pre-money|post-money)", re.I)
# A company name as a headline writes it: capitalised words, "and", "&".
NAME_LIKE = re.compile(r"^(?:[A-Z0-9][\w.&'’-]*|[a-z]+[A-Z][\w.&'’-]*)(?:\s+(?:[A-Z0-9][\w.&'’-]*|and|&|of|de))*$")
# "... raised $116M in 2023": a deal from another year, retold.
PAST_YEAR_DEAL = re.compile(
    r"\b(?:rais(?:ed|ing)|acquired|bought|closed|secured|valued|funding|round|investment|deal|loan|debt)\b[^.\n]{0,80}?"
    r"\b(?:in|back in|during|since|from)\s+((?:19|20)\d{2})\b", re.I)
LAST_YEAR = re.compile(r"\b(?:last|previous) year\b", re.I)
# Not a done deal: "Nvidia considers $10B investment", "ahead of potential $3.5B round", "in early
# talks", "files to sell $10B in stock", "files IPO".
SPECULATIVE = re.compile(
    r"\b(?:consider(?:s|ing)?|weigh(?:s|ing)?|mull(?:s|ing)?|eye(?:s|ing)|plan(?:s|ning)? to|plans|potential|possible|"
    r"talks|talks to|seek(?:s|ing)|could|may|might|reportedly|rumou?r(?:s|ed)?|expected to|aims? to|looking to|"
    r"ahead of|would|delay(?:s|ed)?|postpone(?:s|d)?|shelve(?:s|d)?|hints?|speculation|files?|filing|prospectus|s-1)\b", re.I)
# An IPO that happened: priced, raised, began trading.
IPO_DONE = re.compile(r"\b(?:prices?|priced|rais(?:es|ed)|debut(?:s|ed)?|begins? trading|began trading|went public|goes public|"
                      r"listed|lists on|soars? in|jumps? in)\b", re.I)
IPO_WORDS = re.compile(r"\b(?:ipo|goes public|go public|went public|listing|lists? on|files to list|debut(?:s|ed)? on)\b", re.I)
# The headline's main clause: what follows these words is background ("... as earnings top estimates").
BACKGROUND = re.compile(r"\s(?:amid|after|following|as|despite|while|weeks after|months after)\s|\s[-–—]\s|;", re.I)
MONEY = re.compile(
    r"(?:(?P<cur>[$€£₹]|us\$|c\$|a\$|usd|eur|gbp|rmb|cny|cad|aud|chf|inr|sek|jpy|krw)\s*)?(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*"
    r"(?P<unit>trillion|billion|million|thousand|tn|bn|mn|[tbmk])?\b"
    r"(?:\s*(?P<cur2>dollars|euros|pounds|usd|eur|gbp|yuan|rmb|cny|cad|aud|chf|francs|rupees|yen|won|sek))?", re.I)
UNITS = {"trillion": 1e12, "tn": 1e12, "t": 1e12, "billion": 1e9, "bn": 1e9, "b": 1e9,
         "million": 1e6, "mn": 1e6, "m": 1e6, "thousand": 1e3, "k": 1e3}
SERIES = re.compile(r"\bseries ([a-k])\b|\b(pre-seed|seed)\b", re.I)
SERIES_ROUND = {"a": "series_a", "b": "series_b", "c": "series_c"}
# Words of a company name that do not identify it in a headline.
NAME_FILLER = {"inc", "ltd", "the", "labs", "lab", "for", "and", "of", "group", "holding", "holdings", "technologies",
               "technology", "corp", "corporation", "company", "co", "llc", "plc", "pbc", "ab", "ag", "gmbh", "sa", "sas"}
LEGAL_SUFFIX = re.compile(r"(?:,?\s+(?:inc|ltd|llc|plc|pbc|ab|ag|gmbh|s\.?a\.?s?|corp|corporation|limited|co|holding ab|"
                          r"holdings? inc|bv|nv)\.?)+\s*$", re.I)
# What a buyer bought when it is not a company: a site, a stake, shares, a unit.
ASSET_WORDS = re.compile(r"\b(?:sites?|campus|plants?|facilit(?:y|ies)|land|assets?|stakes?|shares|units?|divisions?|"
                         r"portfolio|data cent(?:er|re)s?|patents?|business(?:es)?|team|equity|stock|%)\b|\$|€|£", re.I)
BUY = (r"(?:agrees? to |has agreed to |moves to |to |will )?(?:acquires?|buys?|bought|acquired|snaps up|takes over|"
       r"completes (?:its |the )?acquisition of|closes (?:its |the )?acquisition of)")
INVEST = r"(?:invests?|backs|leads|joins|commits)"


def company_key(name: str | None) -> str:
    """Same company, same key: "Mistral AI", "Mistral" and "MISTRAL AI Inc." (trackers.org_key)."""
    return org_key(name)


def _money_values(m: re.Match) -> tuple[float, str] | None:
    """(the amount as stated, its currency key) for one MONEY match, or None when it is not money."""
    cur, unit = (m.group("cur") or m.group("cur2") or "").lower(), (m.group("unit") or "").lower()
    if not cur and not unit:
        return None
    if not cur and unit in ("t", "b", "m", "k"):
        return None  # "3 B" without a currency is a size or a grade, not money
    try:
        return float(m.group("num").replace(",", "")) * UNITS.get(unit, 1.0), cur
    except ValueError:
        return None


def money_in(text: str) -> list[float]:
    """Every amount the text states, in US dollars: "€3B" gives 3.45e9, "$116 million" 1.16e8.
    A bare number without a currency or a scale word is not money."""
    out = []
    for m in MONEY.finditer(text or ""):
        got = _money_values(m)
        if not got:
            continue
        value, cur = got
        out.append(value * RATES.get(cur, 1.0))
        if cur in RATES:
            out.append(value)  # the model may have stored the figure unconverted
    return out


def _figures(headline: str) -> list[float]:
    """The amounts a headline states, in US dollars, leaving out its valuation."""
    out = []
    for m in MONEY.finditer(VALUATION_FIGURE.sub(" ", headline or "")):
        got = _money_values(m)
        if got:
            out.append(got[0] * RATES.get(got[1], 1.0))
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


def main_clause(headline: str) -> str:
    """The part of a story headline that is its news: before "amid", "after", "as", a dash or ";"."""
    return BACKGROUND.split(f" {headline or ''} ", maxsplit=1)[0].strip()


def _words(name: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", (name or "").lower())]


def named_as(company: str, headline: str) -> str | None:
    """The company as the headline spells it ("Zipher" for "Zipher Technologies Ltd.", "Unsiloed AI"
    for "unsiloed ai"), or None when the headline does not name it. The name's first distinctive word
    must be there; as many of the following words as the headline also has are kept."""
    words = [w for w in re.split(r"[\s,]+", LEGAL_SUFFIX.sub("", company or "").strip()) if w]
    while words and _words(words[0]) and _words(words[0])[0] in NAME_FILLER:
        words = words[1:]  # "The Browser Company": "Browser" is the distinctive word
    if not words:
        return None
    words[0] = re.sub(r"\.com$", "", words[0], flags=re.I) or words[0]
    first = re.sub(r"\W", "", words[0])
    if not first or (len(first) < 3 and not words[0].isupper() and not any(c.isdigit() for c in words[0])):
        return None
    for n in range(len(words), 0, -1):
        pattern = r"[\s-]+".join(re.escape(w) for w in words[:n])
        m = re.search(rf"(?<![\w.]){pattern}(?![\w])", headline or "", re.I)
        if m:
            return m.group(0)
    return None


def _clean_target(text: str) -> str | None:
    """The company a buyer bought, from the words after the verb: "AI coding startup Conductor" is
    "Conductor"; "two North Carolina sites" or "a 49% stake" is not a company."""
    text = text.strip(" ,.;:'\"")
    if not text or ASSET_WORDS.search(text) or not re.match(r"[A-Z0-9]", text) or re.search(r"\w['’]s\b", text):
        return None
    words = text.split()
    run: list[str] = []
    for w in reversed(words):  # the last run of capitalised words is the name
        if re.match(r"[A-Z0-9]", w) or (run and w.lower() in {"of", "&", "and", "de", "la"}):
            run.insert(0, w)
        elif run:
            break
    name = " ".join(run).strip(" ,")
    while name and name.split()[0].lower() in {"ai", "and", "&", "of"} and len(name.split()) > 1:
        name = " ".join(name.split()[1:])
    return name or None


def _buyer_target(main: str) -> tuple[str, str] | None:
    m = re.match(rf"^(?P<buyer>.+?)\s+{BUY}\s+(?P<target>.+?)(?=\s+(?:for|in|to|from|as|at|with|by|over|on)\b|[,;:(]|$)",
                 main, re.I)
    if not m or not NAME_LIKE.match(m.group("buyer").strip()):
        return None  # "Shield AI raises $2.5B to acquire Echodyne": the buyer is not the subject
    return m.group("buyer").strip(), m.group("target").strip()


def _investor_target(main: str) -> tuple[str, str | None] | None:
    m = re.match(rf"^(?P<who>.+?)\s+{INVEST}\b(?:[^,;]*?\bin\s+(?P<target>[A-Z][^,;]*?)(?=\s+(?:to|for|as|at|with|via|after|amid|and)\b|[,;:(]|$))?",
                 main)
    if not m or not NAME_LIKE.match(m.group("who").strip()):
        return None
    return m.group("who").strip(), (m.group("target") or "").strip() or None


def _same_company(a: str, b: str) -> bool:
    ka, kb = company_key(a), company_key(b)
    return bool(ka) and ka == kb


def headline_figure(company: str, main: str) -> tuple[float, str, str] | None:
    """The round or price the headline's main clause states for this company: (US dollars, the figure
    as written, its currency key). "Sierra finalizes $1.75B Series F", "Dropbox acquires Tigris Data
    for $350M", "Nvidia invests $30B in OpenAI"."""
    name = named_as(company, main)
    if not name:
        return None
    n = re.escape(name)
    lead = r"(?:about |over |more than |nearly |almost |up to |roughly |around |an? |its |a fresh |another )?"
    patterns = [
        rf"{n}\b[^$€£₹]*?\b(?:raises|raised|secures|secured|closes|closed|lands|landed|bags|bagged|nets|gets|receives|collects|"
        rf"finalizes|finalises|completes|announces)\s+{lead}(?P<money>(?:(?:rmb|cny|cad|aud|chf|inr|sek|usd|eur|gbp)\s*)?[^\s,;]*\d[^\s,;]*(?:\s+(?:million|billion|trillion|thousand|[mbk]n?))?"
        rf"(?:\s+(?:dollars|euros|pounds|yuan))?)",
        rf"{BUY}\s+(?:[\w-]+\s+){{0,4}}?{n}\b[^$€£₹]{{0,40}}?\bfor\s+{lead}(?P<money>(?:(?:rmb|cny|cad|aud|chf|inr|sek|usd|eur|gbp)\s*)?[^\s,;]*\d[^\s,;]*(?:\s+(?:million|billion|trillion|[mbk]n?))?)",
        rf"{INVEST}\s+{lead}(?P<money>(?:(?:rmb|cny|cad|aud|chf|inr|sek|usd|eur|gbp)\s*)?[^\s,;]*\d[^\s,;]*(?:\s+(?:million|billion|trillion|[mbk]n?))?)\s+in\s+(?:[\w-]+\s+){{0,3}}?{n}\b",
    ]
    for p in patterns:
        m = re.search(p, main, re.I)
        if not m:
            continue
        mm = MONEY.search(m.group("money"))
        if not mm:
            continue
        got = _money_values(mm)
        if not got:
            continue
        value, cur = got
        if not cur and not re.search(r"dollar", m.group("money"), re.I):
            if not re.search(r"[$€£₹]|usd|eur|gbp|rmb|cad", m.group("money"), re.I):
                continue
        return value * RATES.get(cur, 1.0), mm.group(0).strip(), cur
    return None


def stated_label(written: str, cur: str) -> str | None:
    """"€6.6M", "RMB 200M", "£100M": a figure stated in another currency, as the page writes it."""
    label = CURRENCY_LABEL.get(cur)
    m = MONEY.search(written)
    if not label or not m:
        return None
    num = m.group("num").replace(",", "")
    unit = (m.group("unit") or "").lower()
    short = {"million": "M", "mn": "M", "m": "M", "billion": "B", "bn": "B", "b": "B", "trillion": "T", "tn": "T", "t": "T",
             "thousand": "K", "k": "K"}.get(unit, "")
    try:
        value = float(num)
    except ValueError:
        return None
    if not short and value >= 1e3:
        for div, s in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
            if value >= div:
                value, short = value / div, s
                break
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return f"{label}{text}{short}"


def normalize(f: dict, headline: str) -> tuple[dict | None, str | None]:
    """The row with what the story headline settles: the company bought rather than the buyer, the
    company rather than its investor, the round or price the headline states, its name as the headline
    spells it. (row, None), or (None, why) when the row cannot be this story's deal."""
    row = clean_row(f)
    main = main_clause(headline)
    rnd = row["round"]
    company = row["company"]
    bought = _buyer_target(main)
    if bought:
        buyer, target_text = bought
        target = _clean_target(target_text)
        if _same_company(company, buyer) or named_as(company, buyer):
            if not target:
                return None, "a purchase of assets or a stake, not of a company"
            row["investors"] = [buyer] + [i for i in row["investors"] if not _same_company(i, buyer)]
            # The buyer's row keeps its figure only when the headline states it (as the price, or the
            # sum of the prices: "buys Fiddler AI and Dreadnode for $400M and $95M").
            stated = _figures(main)
            amount = row["amount_usd"]
            if not (amount and (any(close(amount, v, 0.01) for v in stated) or close(amount, sum(stated), 0.01))):
                amount = None
            row["company"], row["round"], row["amount_usd"], row["valuation_usd"] = target, "acquisition", amount, None
            company, rnd = target, "acquisition"
        elif re.search(rf"\bfrom\s+{re.escape(named_as(company, main) or '~~')}\b", main) or (
                not target and re.search(rf"{re.escape(named_as(company, target_text) or '~~')}['’]s\b", target_text)):
            # "... site from Crusoe", "acquires Faraday Future's robotics unit": the company sold something.
            return None, "the company is the seller"
        elif target and _same_company(company, target) and rnd != "acquisition":
            # "Anthropic acquires Opaque Systems": the row's Series B is Opaque's earlier round.
            row["round"], row["amount_usd"], row["valuation_usd"] = "acquisition", None, None
            rnd = "acquisition"
    invested = None if bought else _investor_target(main)
    if invested and (_same_company(company, invested[0]) or named_as(company, invested[0])):
        target = _clean_target(invested[1] or "")
        if not target:
            return None, "the company is the investor"
        row["investors"] = [invested[0]] + [i for i in row["investors"] if not _same_company(i, invested[0])]
        row["company"], company = target, target
        if not any(close(row["amount_usd"], v, 0.01) for v in _figures(main)):
            row["amount_usd"] = None  # the investor's own figure, not the round: the headline's comes next
    spelled = named_as(company, main)
    # The headline's spelling, except a capital it has only because it starts the headline ("E2e").
    if spelled and not (main.startswith(spelled) and spelled.lower() == company.lower() and spelled[1:] == company[1:]):
        row["company"] = spelled
    fig = headline_figure(row["company"], main)
    if fig:
        value, written, cur = fig
        amount = row["amount_usd"]
        # "raises $40M seed then $870M": with two figures the headline does not say which is the row's.
        figures = _figures(main)
        # A figure in another currency only has to agree within the conversion's slack.
        slack = AMOUNT_TOLERANCE if cur in RATES else 0.01
        if not amount or (len(figures) == 1 and not (close(amount, value, slack) or close(amount, value / RATES.get(cur, 1.0), slack))):
            row["amount_usd"] = value
        if cur in RATES:
            row["amount_stated"] = stated_label(written, cur)
    if rnd == "other" and re.search(r"\b(?:loan|debt|credit facility)\b", main, re.I):
        row["round"] = "debt"
    elif rnd == "other":
        found = {(m.group(1) or m.group(2)).lower() for m in SERIES.finditer(main)}
        if len(found) == 1:
            s = found.pop()
            row["round"] = "seed" if s in ("seed", "pre-seed") else SERIES_ROUND.get(s, "series_d_plus")
    return row, None


def is_deal(f: dict, headline: str, text: str, now: datetime,
            article_date: str | None = None, story_date: str | None = None, check_amount: bool = True) -> str | None:
    """None when this looks like a real deal reported in this story; otherwise why it is not.
    `headline` is the story's headline (the row links to that story); `text` the article's title, key
    points, summary and description (where the amount must appear)."""
    company = str(f.get("company") or "").strip()
    if company.lower() in UNKNOWN or not company_key(company) or NOT_A_COMPANY.search(company):
        return "no company"
    rnd = str(f.get("round") or "other").strip().lower()
    head = headline or ""
    main = main_clause(head)
    # The deal must be the story's news: its company named in the headline's main clause, and done, not planned.
    if not named_as(company, main):
        return "company not named in the headline"
    if SPECULATIVE.search(head):
        return "not a done deal"
    if NOT_DEAL.search(main) or (NOT_DEAL_SOFT.search(main) and not headline_figure(company, main)):
        return "not a round or a purchase"
    if rnd == "ipo" and not (IPO_WORDS.search(main) and IPO_DONE.search(main)):
        return "called an IPO, but the headline does not say it happened"
    if not FUNDING_WORDS.search(main):
        if LAUNCH_WORDS.search(main):
            return "headline is about a product, not a deal"
        return "nothing about a deal in the headline"
    if rnd == "acquisition" and not ACQUISITION_WORDS.search(main):
        return "called an acquisition, but the headline does not say so"
    amount = f.get("amount_usd")
    if amount is not None:
        if not (0 < float(amount) <= MAX_AMOUNT_USD):
            return "implausible amount"
        if check_amount and not amount_in_text(float(amount), f"{head} {text or ''}"):
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


def review(f: dict, headline: str, text: str, now: datetime, article_date: str | None = None,
           story_date: str | None = None, check_amount: bool = True) -> tuple[dict | None, str | None]:
    """The row as the tracker lists it, or (None, why it is not a deal in this story)."""
    row, why = normalize(f, headline)
    if why:
        return None, why
    why = is_deal(row, headline, text, now, article_date, story_date, check_amount)
    return (None, why) if why else (row, None)


def clean_row(f: dict) -> dict:
    """The row as the page shows it: known round names, plausible figures, a trimmed investor list."""
    rnd = str(f.get("round") or "other").strip().lower()
    valuation = f.get("valuation_usd")
    valuation = float(valuation) if valuation and 0 < float(valuation) <= MAX_VALUATION_USD else None
    amount = f.get("amount_usd")
    company = LEGAL_SUFFIX.sub("", str(f.get("company") or "").strip()).strip() or str(f.get("company") or "").strip()
    return {"company": company, "amount_usd": float(amount) if amount else None,
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


def _name_words(name: str) -> tuple[str, ...]:
    return tuple(w for w in _words(org_key_words(name)) if w)


def org_key_words(name: str) -> str:
    """The company name without legal and filler words, words kept apart: "Tutor Intelligence Inc." is
    "tutor intelligence"."""
    return ORG_WORDS.sub(" ", re.sub(r"\.com\b", " ", _plain(name or "")))


def _one_company(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    """"Tutor" and "Tutor Intelligence", "MIT" and "MIT-led team (PLOW)": one name is the other's
    first words. "Meta" and "Metano" are not."""
    if not a or not b:
        return False
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    return long_[:len(short)] == short


def _same_deal(a_amount, a_date, b_amount, b_date) -> bool:
    a_dt, b_dt = _dt(a_date), _dt(b_date)
    days = abs((a_dt - b_dt).days) if a_dt and b_dt else 0
    if a_amount and b_amount:
        if close(a_amount, b_amount, SAME_FIGURE) and days <= SAME_FIGURE_DAYS:
            return True
        if not close(a_amount, b_amount):
            return False
    return days <= SAME_DEAL_DAYS


def fold(cands: list[dict]) -> list[dict]:
    """One row per deal. Within a story, a company's rows are one deal decided by majority
    (_majority), and two spellings of one company ("Tutor", "Tutor Intelligence") are one company.
    Across stories, a company's deals merge when their amounts are within the tolerance (or one has
    none) and they are within SAME_DEAL_DAYS of each other, or state the very same figure within
    SAME_FIGURE_DAYS. Figures are then decided by majority over the merged rows, ties to the lead
    article, then the first seen."""
    by_story: dict[tuple[str, str | None], list[dict]] = {}
    for c in cands:
        by_story.setdefault((company_key(c["company"]), c.get("storySlug")), []).append(c)
    # Two spellings of one company in one story.
    for slug in {s for _k, s in by_story}:
        keys = sorted((k for k, s in by_story if s == slug), key=len)
        for i, short in enumerate(keys):
            if (short, slug) not in by_story:
                continue
            sw = _name_words(by_story[(short, slug)][0]["company"])
            for longer in keys[i + 1:]:
                if (longer, slug) in by_story and _one_company(sw, _name_words(by_story[(longer, slug)][0]["company"])):
                    by_story[(short, slug)].extend(by_story.pop((longer, slug)))
    groups = [{"key": key, "words": _name_words(ms[0]["company"]), "members": _majority(ms)} for (key, _slug), ms in by_story.items()]
    groups.sort(key=lambda g: min((m.get("date") or "~" for m in g["members"]), default="~"))
    deals: list[dict] = []
    for g in groups:
        g_amount = next((m["amount_usd"] for m in g["members"] if m.get("amount_usd")), None)
        g_date = next((m["date"] for m in g["members"] if m.get("date")), None)
        home = None
        for d in deals:
            if d["key"] != g["key"] and not _one_company(d["words"], g["words"]):
                continue
            d_amount = next((m["amount_usd"] for m in d["members"] if m.get("amount_usd")), None)
            d_date = next((m["date"] for m in d["members"] if m.get("date")), None)
            if d["key"] != g["key"] and not (g_amount and d_amount and close(g_amount, d_amount)):
                continue  # "Warp" and "Warp Sciences": one company only when the figure agrees
            if _same_deal(g_amount, g_date, d_amount, d_date):
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
        amount = _vote([m.get("amount_usd") for m in ms], lead.get("amount_usd"))
        stated = next((m["amount_stated"] for m in ms if m.get("amount_stated") and m.get("amount_usd") == amount), None)
        names = [m["company"] for m in ms]
        out.append({
            "company": _vote(names, lead["company"]),
            "amount_usd": amount,
            **({"amount_stated": stated} if stated else {}),
            "round": _vote([m.get("round") for m in ms], lead.get("round")) or "other",
            "investors": investors,
            "valuation_usd": _vote([m.get("valuation_usd") for m in ms], lead.get("valuation_usd")),
            "date": min((m["date"] for m in ms if m.get("date")), default=None),
            "storySlug": lead.get("storySlug"),
            "storyHeadline": lead.get("storyHeadline"),
            "sources": sum(int(m.get("sources") or 1) for m in ms),
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
            text = " ".join([a.get("title") or "", a.get("headline") or "", *(a.get("keyPoints") or []),
                             a.get("summaryMd") or "", a.get("description") or ""])
            row, why = review(f, st.get("headline") or "", text, now, a.get("publishedAt"), st.get("firstPublishedAt"))
            if why:
                dropped[why] += 1
                continue
            cands.append({**row, "date": a.get("publishedAt"), "isLead": bool(a.get("isLead")),
                          "storySlug": st.get("slug"), "storyHeadline": st.get("headline")})
    rows = fold(cands)
    rows.sort(key=lambda r: r["date"] or "", reverse=True)
    return rows, dict(dropped)


def total(rows: list[dict]) -> float:
    return float(sum(r["amount_usd"] or 0 for r in rows))


# ---- The tracker's memory ----
# The export builds the rows from the stories in its window (EXPORT_DAYS), so a deal left /funding,
# its company's page and the week pages some 45 days after it was reported. Like the model ledger
# (releases.py), each run merges this run's rows into the kept ones: a small JSON file in the runner's
# cache, carried between runs by the Actions cache and backed up inside archive.json.gz (archive.py).
# A kept row whose story is still in the window is rebuilt from that story this run (so a rule fixed
# today also fixes it); a row whose story has left is kept if its own fields still pass the headline
# rules; rows of unpublished stories go.

FORMAT = 1
ARCHIVE_KEY = "funding"
MAX_ROWS = 20000
# Which rules a kept row was last tested under: this file's own text. A row tested under the current
# rules is not tested again, so a run reads its kept rows in milliseconds; after a rule change every
# row is tested once (under two seconds per thousand rows).
RULES = hashlib.sha1(Path(__file__).read_bytes()).hexdigest()[:12]


def path() -> Path:
    return config.CACHE_DIR / "funding.json"


def read_file() -> list[dict] | None:
    """The runner's copy, or None when it is missing or unreadable."""
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and data.get("format") == FORMAT and isinstance(data.get("rows"), list):
        return [r for r in data["rows"] if isinstance(r, dict)]
    return None


def load() -> list[dict]:
    """Kept rows: the runner's copy, else the copy inside the archive (its daily backup), else none."""
    rows = read_file()
    if rows is not None:
        return rows
    try:
        from . import archive
        kept = archive._load().get(ARCHIVE_KEY)
        if isinstance(kept, list):
            log.info("funding rows taken from the archive's copy: %d", len(kept))
            return [r for r in kept if isinstance(r, dict)]
    except Exception as exc:  # the backup is a nicety; never stop the export for it
        log.warning("archive copy of funding rows unreadable: %s", str(exc)[:120])
    return []


def save(rows: list[dict]) -> None:
    path().parent.mkdir(parents=True, exist_ok=True)
    tmp = path().with_name(path().name + ".tmp")
    tmp.write_text(json.dumps({"format": FORMAT, "rows": rows}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path())


def still_a_deal(row: dict, now: datetime) -> bool:
    """A kept row whose story has left the window, tested again on its own fields and its story's
    headline (the article text is gone, so the amount check is skipped)."""
    head = row.get("storyHeadline") or ""
    got, _why = review(row, head, "", now, check_amount=False)
    return got is not None and _same_company(got["company"], row.get("company") or "")


def remember(current: list[dict], live_slugs: set[str], unpublished, now: datetime) -> list[dict]:
    """Every deal kept so far plus this run's, newest first. `live_slugs` are the stories in this
    run's window: their deals are this run's rows."""
    gone = set(unpublished or [])
    kept = [r for r in load()
            if r.get("storySlug") not in gone and r.get("storySlug") not in live_slugs
            and (r.get("checked") == RULES or still_a_deal(r, now))]
    rows = [dict(r) for r in current]
    # Rows by the first word of the company's name: a twin shares at least that.
    by_word: dict[str, list[int]] = {}

    def index(i: int) -> None:
        words = _name_words(rows[i]["company"])
        rows[i]["_k"], rows[i]["_w"] = company_key(rows[i]["company"]), words
        by_word.setdefault(words[0] if words else "", []).append(i)

    for i in range(len(rows)):
        index(i)
    for old in sorted(kept, key=lambda r: r.get("date") or ""):
        k, w = company_key(old["company"]), _name_words(old["company"])
        twin = next((rows[i] for i in by_word.get(w[0] if w else "", [])
                     if (rows[i]["_k"] == k or (_one_company(rows[i]["_w"], w) and close(rows[i].get("amount_usd"), old.get("amount_usd"))))
                     and _same_deal(rows[i].get("amount_usd"), rows[i].get("date"), old.get("amount_usd"), old.get("date"))), None)
        if twin is None:
            rows.append(dict(old))
            index(len(rows) - 1)
            continue
        # The earlier report is the deal's: its date, story and headline stay; figures it lacks come
        # from the other row.
        first, second = (old, twin) if (old.get("date") or "~") <= (twin.get("date") or "~") else (twin, old)
        merged = dict(first)
        for f in ("amount_usd", "valuation_usd", "amount_stated"):
            if not merged.get(f) and second.get(f):
                merged[f] = second[f]
        if (merged.get("round") or "other") == "other" and second.get("round"):
            merged["round"] = second["round"]
        if len(second.get("investors") or []) > len(merged.get("investors") or []):
            merged["investors"] = second["investors"]
        merged["sources"] = max(int(old.get("sources") or 0), int(twin.get("sources") or 0))
        merged["_k"], merged["_w"] = twin["_k"], twin["_w"]
        twin.clear()
        twin.update(merged)
    rows = [{k: v for k, v in r.items() if k not in ("_k", "_w", "checked")} for r in rows]
    rows.sort(key=lambda r: r.get("date") or "", reverse=True)
    rows = rows[:MAX_ROWS]
    try:
        save([{**r, "checked": RULES} for r in rows])
    except OSError as exc:
        log.warning("funding rows not saved: %s", str(exc)[:120])
    return rows
