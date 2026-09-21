"""AI at Work (/work): the practical card behind the section, and everything its pages are built from.

The summary model fills a "work_card" for an article only when a marketer, a small-business owner or
a small team can act on it: a tool, a feature, a how-to, or a price or policy change that affects
their work. Industry news, funding rounds and research get no card, so the section is a different
kind of output from the news digest, not a second category page.

This module holds three things, all pure functions so they can be tested without a database:

- clean_card: validate and clamp what the model returned (enrich.py), the way model_release and
  funding are cleaned. A card without a tool, a plain sentence or an honest caveat is dropped: the
  section promises "watch out" on every card, so a card that cannot keep that promise is no card.
- the section's derived facts: which job a card belongs to, how useful it is, whether it is
  something to skip this week, and one row per tool for the directory (export.py).
- the section's own briefing: the most useful practical items of the last day or two, ordered by
  usefulness rather than news importance.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from .trackers import _plain, org_key

# Who a card is for, as the model may answer.
WHO = ("marketer", "sales", "founder", "support", "ops", "ecommerce")
WHO_LABELS = {
    "marketer": "marketers",
    "sales": "sales",
    "founder": "founders",
    "support": "support teams",
    "ops": "operations",
    "ecommerce": "online shops",
}
# How much work it is before it does anything useful.
EFFORTS = ("minutes", "an afternoon", "needs a developer")
# The jobs the section's filters offer, in the order they are shown.
JOBS = {
    "customers": "Get customers",
    "content": "Make content",
    "sell": "Sell",
    "support": "Support customers",
    "business": "Run the business",
}
WHO_JOBS = {
    "marketer": ["customers"],
    "sales": ["sell"],
    "ecommerce": ["sell"],
    "support": ["support"],
    "founder": ["business"],
    "ops": ["business"],
}
# "Make content" is not a reader, it is a task: it comes from what the card says the tool does.
CONTENT_WORDS = re.compile(
    r"\b(content|copy(?:writing)?|writ(?:e|es|ing)|draft(?:s|ing)?|blog|article|newsletter|caption|"
    r"headline|script|video|image|photo|design|graphic|slide|podcast|voice ?over|social (?:post|media)|"
    r"seo|transcri(?:be|pt)|translat)\w*", re.I)
# Wording in a caveat that means "not for everyone yet": the weekly playbook lists these as things
# to skip for now rather than things to try.
NOT_YET = re.compile(
    r"\b(wait ?list|beta|preview|early access|invite[- ]only|pilot|limited (?:to|availability|rollout|release)|"
    r"rolling out|not (?:yet )?(?:available|live)|coming (?:soon|later)|deprecat|sunset|us[- ]only|"
    r"enterprise[- ](?:only|plan|tier|customers))\w*", re.I)


def _text(value, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _list(value) -> list:
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str) and value.strip():
        return [value]
    return []


def cost_kind(cost: str) -> str:
    """What the reader actually has to pay, from the model's own words."""
    text = (cost or "").strip().lower()
    if not text:
        return "unknown"
    if "included" in text or "already" in text:
        return "included"
    if "free tier" in text or "freemium" in text or "free plan" in text:
        return "free tier"
    if re.search(r"\$|\beur\b|€|£|paid|subscription|per month|/mo|per seat|credits", text):
        return "paid"
    if "free" in text:
        return "free"
    return "unknown"


def _effort(value) -> str | None:
    """Three answers only; anything else is read for its meaning, then dropped if it has none."""
    text = (str(value or "")).strip().lower()
    if not text:
        return None
    if text in EFFORTS:
        return text
    if re.search(r"develop|engineer|\bapi\b|\bcode\b|technical", text):
        return "needs a developer"
    if re.search(r"minute|instant|straight away|right away|no setup|sign ?up", text):
        return "minutes"
    if re.search(r"afternoon|hour|a day|half a day|weekend|evening", text):
        return "an afternoon"
    return None


def clean_card(value) -> dict | None:
    """The card as it is stored, or None when the article does not belong in the section.

    Dropped when the model says it does not fit, when it names no tool, when it cannot say in one
    sentence what the thing does, or when it has no honest caveat; and, by rules rather than the
    model's word, when it is a developer or infrastructure tool or a course (screen_card). Everything
    else is clamped, so a talkative or a sloppy answer cannot reach a page.
    """
    return screen_card(value)[0]


# ---------------------------------------------------------------------------- rules-only screen
#
# The model's "fits" flag let developer tooling through: an inference gateway for GPU clusters, a
# coding assistant, a runner for local models. None of them is something a marketer or a shop owner
# can pick up this week, whatever the model says about "minutes" and "founders". These rules read
# the card's own words (the tool, what it does, what to use it for) and never the caveat, which may
# rightly mention an API or a region without the tool being for developers.

DEV_WORDS = re.compile(
    r"\b(?:gpus?|cuda|kubernetes|k8s|docker|containeri[sz]\w*|"
    r"(?:gpu|compute|kubernetes|training|inference|hpc) clusters?|clusters? of gpus|"
    r"sdks?|inference (?:gateway|endpoints?|server|serving|engine)|model serving|api endpoints?|"
    r"api[- ]only|(?:only |available )?(?:via|through) (?:the |an |its )?api|developer (?:api|platform|preview)|"
    r"for developers|clis?|command[- ]line|in (?:the|your) terminal|terminal (?:app|tool|commands?)|"
    r"ides?|vs ?code|coding (?:assistant|agent|tool|model)s?|code (?:editor|assistant|completion|review|generation)|"
    r"(?:write|writing|generate|generating|review|reviewing|debug|debugging) code|codebases?|(?:code|git) repositor(?:y|ies)|git(?:hub)? (?:history|repos?)|pull requests?|"
    r"fine[- ]?tun\w*|local (?:llms?|models?)|run (?:llms?|models?) locally|ollama|llama\.cpp|lm studio|self[- ]host\w*|"
    r"open[- ]weights?|vector (?:database|store)s?|embeddings|embedding models?|rag pipelines?|first[- ]token latency|tokens per second|"
    r"agent (?:harness|framework|orchestration|sdk)\w*|agents? from any provider|multi[- ]?agent|agent[- ]to[- ]agent|mcp servers?|"
    r"aws|amazon web services|sagemaker|(?:amazon |aws )bedrock|ec2|gcp|google cloud|vertex ai|azure|hyperpod)\b",
    re.I)
# A tool that wraps the same power in a screen a non-developer can use stays in.
NO_CODE = re.compile(r"\b(?:no[- ]code|without (?:writing )?(?:any )?code|drag[- ]and[- ]drop|point[- ]and[- ]click|"
                     r"visual (?:builder|editor)|non[- ]?(?:technical|developers?))\b", re.I)
# Courses, certificates and deal-site bundles are sold to people who want to learn AI; they are not a
# tool that does a job. A reseller or "wrapper" app that sells access to other companies' models is
# the same kind of listing.
COURSE_WORDS = re.compile(
    r"\b(?:(?<!of )courses?|e-?degrees?|(?<!gift )certificat(?:e|es|ion|ions)|bootcamps?|masterclass(?:es)?|training bundle|"
    r"(?:course|training|learning) (?:bundle|pack)|bundle of \w+ courses|lifetime (?:access|deal|subscription|licen[cs]e)|"
    r"resell\w*|wrapper (?:app|for)|access to (?:multiple|several|all the|many|\d+\+?) (?:ai )?(?:models|chatbots|ais))\b",
    re.I)


def _dashes(text: str) -> str:
    """The model writes non-breaking and other Unicode hyphens ("first‑token"); the rules match "-"."""
    return re.sub(r"[‐-―−]", "-", text or "")


def developer_only(card: dict) -> str | None:
    """"developer" when the card is about developer or infrastructure tooling, "course" when it is a
    course or a reseller listing, else None. The tool's name, what it does and what to use it for are
    read; the caveat is not."""
    text = _dashes(" ".join([card.get("tool") or "", card.get("what_it_does") or "", *(card.get("use_for") or [])]))
    if COURSE_WORDS.search(text):
        return "course"
    if DEV_WORDS.search(text) and not NO_CODE.search(text):
        return "developer"
    return None


# Makers the model is known to credit wrongly, with the domains their own pages live on. A "try it"
# link elsewhere means the card credits the wrong company (a third party's course "by Claude AI"), so
# the maker is taken from the link instead.
MAKER_DOMAINS: dict[str, tuple[str, tuple[str, ...], tuple[str, ...]]] = {
    # key: (display name, names the model uses, domains)
    "anthropic": ("Anthropic", ("anthropic", "claude", "claude ai"), ("anthropic.com", "claude.ai", "claude.com")),
    "openai": ("OpenAI", ("openai", "open ai", "chatgpt"), ("openai.com", "chatgpt.com")),
    "google": ("Google", ("google", "alphabet", "google deepmind", "deepmind", "gemini", "google workspace"),
               ("google.com", "blog.google", "googleblog.com", "withgoogle.com", "youtube.com", "android.com")),
    "microsoft": ("Microsoft", ("microsoft", "microsoft copilot", "copilot"),
                  ("microsoft.com", "office.com", "microsoft365.com", "bing.com", "live.com", "linkedin.com")),
    "meta": ("Meta", ("meta", "meta platforms", "facebook", "instagram", "whatsapp"),
             ("meta.com", "meta.ai", "facebook.com", "fb.com", "instagram.com", "whatsapp.com", "threads.net")),
    "canva": ("Canva", ("canva",), ("canva.com",)),
    "hubspot": ("HubSpot", ("hubspot",), ("hubspot.com",)),
    "shopify": ("Shopify", ("shopify",), ("shopify.com",)),
    "adobe": ("Adobe", ("adobe",), ("adobe.com",)),
}


def _host(link: str | None) -> str:
    m = re.match(r"https?://([^/:?#]+)", link or "", re.I)
    return (m.group(1).lower().removeprefix("www.")) if m else ""


def _on(host: str, domains) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def check_maker(card: dict) -> tuple[str | None, bool]:
    """The maker to show, and whether it was corrected. Only makers on MAKER_DOMAINS are checked,
    and only against a link the card gives: no link, no evidence either way."""
    maker, host = card.get("maker"), _host(card.get("link"))
    if not maker or not host:
        return maker, False
    name = re.sub(r"[^a-z0-9 ]+", " ", maker.lower())
    name = re.sub(r"\b(?:inc|llc|ltd|corp|corporation|platforms inc|pbc)\b", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    claimed = next((k for k, (_d, names, _h) in MAKER_DOMAINS.items() if name in names), None)
    if claimed is None or _on(host, MAKER_DOMAINS[claimed][2]):
        return maker, False
    other = next((display for display, _n, domains in MAKER_DOMAINS.values() if _on(host, domains)), None)
    return other or host, True


# General assistants everyone has heard of. A card about one of them must say what changed ("adds
# scheduled tasks", "now drafts replies in Gmail"); "ChatGPT generates text responses to user
# prompts, use it for dinner ideas" is a personal blog post describing the product, not news a small
# team can act on, and it pushed real launches off the top of /work.
ASSISTANTS = re.compile(r"^(?:chat ?gpt|claude|gemini|copilot|microsoft copilot|perplexity|grok|meta ai|le chat|"
                        r"deepseek|computer use|chatgpt and claude|claude and chatgpt)(?:\s*\(.*\))?$", re.I)
GENERIC_DOES = re.compile(
    r"^(?:it\s+)?(?:generates?|writes?|creates?|produces?|answers?|responds?(?: to)?|drafts?|helps?(?: you)?|"
    r"can|lets you (?:chat|ask)|is an? (?:ai )?(?:chatbot|assistant))\b(?!.*\b(?:new|now|adds?|added|launch\w*|"
    r"introduc\w*|rolls? out|update\w*|feature|mode|integrat\w*|connect\w*|automatic\w*|schedul\w*|agent\w*)\b)", re.I)

VAGUE_OBJECT = re.compile(r"\b(?:text|responses?|answers?|replies|content|questions|prompts?|anything|ideas|conversations?)\b", re.I)


def generic_card(card: dict) -> bool:
    """A card that describes a well-known assistant in general terms instead of a change to it."""
    tool = " ".join((card.get("tool") or "").split())
    does = " ".join((card.get("what_it_does") or "").split())
    return bool(ASSISTANTS.match(tool) and GENERIC_DOES.match(does) and VAGUE_OBJECT.search(does))


def screen_card(value) -> tuple[dict | None, str | None]:
    """(card, None) for a card that belongs on the section; (None, why) for one the rules drop, with
    why in "developer", "course"; (None, None) for one that never was a card. The reason is what the
    export and the enrich step count, so the admin page can say how much the rules keep out."""
    card = _clamp_card(value)
    if card is None:
        return None, None
    reason = developer_only(card)
    if reason:
        return None, reason
    if generic_card(card):
        return None, "nothing new"
    card["maker"], _fixed = check_maker(card)
    return card, None


def _clamp_card(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    if not value.get("fits"):
        return None
    tool = _text(value.get("tool"), 120)
    what = _text(value.get("what_it_does"), 220)
    watch = _text(value.get("watch_out"), 260)
    if not tool or len(what) < 15 or len(watch) < 10:
        return None
    who, seen = [], set()
    for w in _list(value.get("who_for")):
        w = str(w).strip().lower()
        if w in WHO and w not in seen:
            seen.add(w)
            who.append(w)
    if not who:
        who = ["marketer"]
    uses = []
    for u in _list(value.get("use_for")):
        u = _text(u, 90)
        if u and u.lower() not in {x.lower() for x in uses}:
            uses.append(u)
    uses = uses[:3]
    if not uses:
        return None
    link = _text(value.get("link"), 500)
    if not link.startswith(("http://", "https://")):
        link = ""
    return {
        "fits": True,
        "tool": tool,
        "maker": _text(value.get("maker"), 120) or None,
        "what_it_does": what,
        "who_for": who[:4],
        "use_for": uses,
        "cost": _text(value.get("cost"), 60) or "unknown",
        "effort": _effort(value.get("effort")),
        "watch_out": watch,
        "link": link or None,
    }


def jobs_for(card: dict) -> list[str]:
    """The jobs a card helps with: from who it is for, plus "make content" and "get customers" from
    what it does.

    "Marketer" is the model's default reader (and clean_card's), so on its own it no longer puts a
    card under "Get customers": a meeting-notes app for "marketers, founders and operations" is not a
    way to find customers. It does when the card's own words are about ads, search, campaigns or
    reach, or when nothing else would place the card at all.
    """
    text = " ".join([card.get("what_it_does") or "", *(card.get("use_for") or [])])
    jobs: list[str] = []
    for w in card.get("who_for") or []:
        if w == "marketer":
            continue
        for job in WHO_JOBS.get(w, []):
            if job not in jobs:
                jobs.append(job)
    if CONTENT_WORDS.search(text) and "content" not in jobs:
        jobs.append("content")
    if CUSTOMER_WORDS.search(text) or ("marketer" in (card.get("who_for") or []) and not jobs):
        jobs.append("customers")
    return [j for j in JOBS if j in jobs]


# What "Get customers" means in a card's own words: advertising, search, campaigns and reach.
CUSTOMER_WORDS = re.compile(
    r"\b(?:ads?|advert\w*|campaigns?|seo|search (?:ranking|rankings|visibility|traffic|results)|ai search|"
    r"ai overviews?|google ads|meta ads|audiences?|email marketing|marketing emails?|newsletters?|social media|"
    r"social posts?|promot\w*|brand (?:awareness|visibility)|visibility|new customers|find customers|"
    r"reach (?:more |new )?(?:customers|buyers|people)|website traffic|traffic to|shopping ads|product listings?|"
    r"merchant center|marketing)\b", re.I)


def skip_reason(card: dict) -> str | None:
    """Why a reader should not spend this week on it, in their own words, or None."""
    if card.get("effort") == "needs a developer":
        return "needs a developer"
    match = NOT_YET.search(card.get("watch_out") or "")
    if match:
        word = match.group(0).lower()
        if (word.startswith("not ") or word.startswith("limited to")) and limits(card):
            # "Not available in the EEA" or "limited to Business plans": most readers can use it
            # now, so it stays a thing to try and the card names who is left out (limits).
            return None
        if "enterprise" in word and any(re.match(r"(?!enterprise)\w+ (?:and|or) enterprise", x, re.I) for x in limits(card)):
            # "Business and Enterprise plans": a small team on a Business plan has it.
            return None
        if "wait" in word:
            return "waitlist only"
        if "us-only" in word or "us only" in word:
            return "United States only"
        if "enterprise" in word:
            return "enterprise plans only"
        if "deprecat" in word or "sunset" in word:
            return "being withdrawn"
        return "not open to everyone yet"
    return None


# Who cannot use it, when the caveat says so: a region it is not available in, or the plan it needs.
# These are not reasons to skip the tool (most readers can use it), so the card stays where it is and
# says plainly who is left out.
REGIONS = (
    ("EEA", r"eea|european economic area"), ("EU", r"\beu\b|european union"), ("Europe", r"\beurope\b"),
    ("UK", r"\buk\b|united kingdom|britain"), ("Switzerland", r"switzerland"), ("Japan", r"japan"),
    ("China", r"china"), ("Canada", r"canada"), ("Australia", r"australia"), ("India", r"\bindia\b"),
)
UNAVAILABLE = re.compile(r"\b(?:unavailable|not (?:yet )?(?:available|supported|offered|launched)|excluded|excluding|except|"
                         r"outside (?:of )?the|blocked|not in)\b", re.I)
PLAN = re.compile(
    r"\b((?:(?:business|enterprise|pro|premium|plus|team|teams|max|ultra|standard|starter|advanced|paid|education)"
    r"(?:,? (?:and|or) (?:business|enterprise|pro|premium|plus|team|teams|max|ultra|standard|advanced|education))?)"
    r" (?:plans?|tiers?|editions?|subscriptions?|subscribers|accounts|customers))"
    r"(?: (?:and (?:above|up|higher)|or (?:higher|above)|only))?", re.I)
PLAN_QUALIFIER = re.compile(r"\b(?:only|requires?|required|and (?:above|up|higher)|or (?:higher|above)|available (?:on|to|for)|"
                            r"limited to|restricted to|need(?:s)? (?:a|an|the))\b", re.I)


def limits(card: dict) -> list[str]:
    """Plain labels for who cannot use it, read from the caveat: ["not in the EEA, UK",
    "Business and Enterprise plans only"]. Empty when the caveat names no region or plan limit."""
    watch = _dashes(card.get("watch_out") or "")
    out: list[str] = []
    if UNAVAILABLE.search(watch):
        places = [name for name, rx in REGIONS if re.search(rx, watch, re.I)]
        if "EEA" in places and "EU" in places:
            places.remove("EU")
        if places:
            out.append("not in " + ("the " if places[0] in ("EEA", "EU", "UK") else "") + ", ".join(places[:4]))
    elif re.search(r"\b(?:us|u\.s\.|united states)[- ]only\b|only (?:in|for|to) (?:the )?(?:us|u\.s\.|united states)\b", watch, re.I):
        out.append("United States only")
    plan = PLAN.search(watch)
    if plan and PLAN_QUALIFIER.search(watch):
        label = plan.group(1)
        label = label[0].upper() + label[1:]
        tail = plan.group(0)[len(plan.group(1)):].strip()
        out.append(f"{label} {tail}" if tail and tail != "only" else f"{label} only")
    return out


EFFORT_POINTS = {"minutes": 1.0, "an afternoon": 0.6, "needs a developer": 0.0}
COST_POINTS = {"free": 0.8, "free tier": 0.7, "included": 0.6, "paid": 0.3, "unknown": 0.1}


def usefulness(card: dict, story: dict | None = None) -> float:
    """How much use a card is to a small team, which is not how big the news is.

    A free thing you can try in minutes, with an official link and three concrete uses, beats a
    headline-grabbing launch that needs a developer. News importance is deliberately not part of
    this: the main briefing already ranks by that.
    """
    score = 0.0
    score += 1.0 if card.get("link") else 0.0
    score += 0.5 * min(len(card.get("use_for") or []), 3)
    score += EFFORT_POINTS.get(card.get("effort") or "", 0.2)
    score += COST_POINTS.get(cost_kind(card.get("cost") or ""), 0.1)
    score += 0.3 * min(len(card.get("who_for") or []), 3)
    score += 0.4 if card.get("maker") else 0.0
    if skip_reason(card):
        score -= 0.6
    if story:
        # A how-to is something to do today; and coverage by more than one publisher is mild support.
        if (story.get("contentType") or "") == "tutorial":
            score += 0.5
        score += min(0.6, 0.2 * max(0, (story.get("articleCount") or 1) - 1))
    return round(score, 3)


# ---------------------------------------------------------------------------- export shapes

def card_out(card: dict, story: dict | None = None) -> dict:
    """The card as the site reads it, with the facts the pages derive from it."""
    return {
        "tool": card["tool"],
        "maker": card.get("maker"),
        "whatItDoes": card["what_it_does"],
        "whoFor": card.get("who_for") or [],
        "useFor": card.get("use_for") or [],
        "cost": card.get("cost") or "unknown",
        "costKind": cost_kind(card.get("cost") or ""),
        "effort": card.get("effort"),
        "watchOut": card["watch_out"],
        "link": card.get("link"),
        "jobs": jobs_for(card),
        "skip": skip_reason(card),
        "limits": limits(card),
        "usefulness": usefulness(card, story),
    }


def _completeness(card: dict) -> int:
    return sum(1 for k in ("maker", "link", "effort") if card.get(k)) + len(card.get("use_for") or [])


def story_card(story: dict, cards: dict[int, dict]) -> dict | None:
    """One card per story: the lead article's when it has one, else the most complete of the others.

    A story gathers several articles about the same thing; they do not all carry a card, and the
    ones that do can be more or less specific about the tool. `cards` maps article id to the stored
    card, so the answer is the card itself, not a copy of the exported shape.
    """
    mine = [(a["id"], cards[a["id"]]) for a in story.get("articles") or [] if cards.get(a["id"])]
    if not mine:
        return None
    best = max(_completeness(c) for _i, c in mine)
    lead = next((c for i, c in mine if i == story.get("leadArticleId")), None)
    if lead is not None and _completeness(lead) >= best - 1:
        return lead
    return max((c for _i, c in mine), key=_completeness)


def section_stories(stories: list[dict]) -> list[dict]:
    """The stories the section covers: any story with a card, whatever its category."""
    return [s for s in stories if s.get("workCard")]


# ---------------------------------------------------------------------------- tools directory

def tool_key(tool: str, maker: str | None) -> str:
    """Same tool, same key: "Gemini in Google Ads" and "Google Ads (Gemini)" are not the same tool,
    but "Canva Magic Studio" (Canva) and "Magic Studio" (Canva Inc.) are."""
    org = org_key(maker)
    # Word order, filler and plurals do not make a different tool: "Notebooks in Gemini" and
    # "Gemini Notebook" (both Google) are one row.
    words = sorted({_singular(w) for w in _plain(tool or "").split() if w and w != org and w not in TOOL_FILLER})
    return ("".join(words).replace(".", "") or org) + "|" + org


TOOL_FILLER = {"in", "for", "the", "a", "an", "of", "on", "with", "by", "and", "app", "feature", "features", "new"}


def _singular(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


def build_tools(stories: list[dict]) -> list[dict]:
    """One row per tool for /work/tools, newest change first.

    Spellings are merged (tool_key), the most complete fields win, and every story that mentioned
    the tool is kept so the row can show what changed and when.
    """
    rows: dict[str, dict] = {}
    for s in sorted(stories, key=lambda s: s.get("firstPublishedAt") or "", reverse=True):
        card = s.get("workCard")
        if not card:
            continue
        key = tool_key(card["tool"], card.get("maker"))
        date = s.get("firstPublishedAt") or s.get("updatedAt")
        row = rows.get(key)
        if row is None:
            rows[key] = {
                "key": key,
                "tool": card["tool"],
                "maker": card.get("maker"),
                "whatItDoes": card["whatItDoes"],
                "whoFor": list(card.get("whoFor") or []),
                "jobs": list(card.get("jobs") or []),
                "cost": card.get("cost"),
                "costKind": card.get("costKind"),
                "effort": card.get("effort"),
                "watchOut": card.get("watchOut"),
                "link": card.get("link"),
                "lastChange": date,
                "firstSeen": date,
                "changes": [{"slug": s["slug"], "headline": s["headline"], "date": date}],
            }
            continue
        # Later rows are older: fill what is missing, keep the earliest date as first seen.
        if len(card["tool"]) > len(row["tool"]):
            row["tool"] = card["tool"]  # the fuller spelling reads better in the directory
        for field, source in (("maker", "maker"), ("link", "link"), ("effort", "effort")):
            if not row.get(field) and card.get(source):
                row[field] = card[source]
        if not row.get("costKind") or row["costKind"] == "unknown":
            row["cost"], row["costKind"] = card.get("cost"), card.get("costKind")
        for w in card.get("whoFor") or []:
            if w not in row["whoFor"]:
                row["whoFor"].append(w)
        for j in card.get("jobs") or []:
            if j not in row["jobs"]:
                row["jobs"].append(j)
        if (date or "") < (row["firstSeen"] or "~"):
            row["firstSeen"] = date
        if len(row["changes"]) < 8:
            row["changes"].append({"slug": s["slug"], "headline": s["headline"], "date": date})
    out = sorted(rows.values(), key=lambda r: r["lastChange"] or "", reverse=True)
    for row in out:
        row["jobs"] = [j for j in JOBS if j in row["jobs"]]
        row["changeCount"] = len(row["changes"])
    return out


# ---------------------------------------------------------------------------- weekly playbook

def week_key(iso: str | None) -> str:
    """ISO week key like 2026-W38, the same one intros.py and the site use."""
    d = datetime.fromisoformat(iso.replace("Z", "+00:00")) if iso else datetime.now(timezone.utc)
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def weeks(stories: list[dict]) -> dict[str, dict]:
    """Every week of the section, as the playbook page shows it: what changed, what to try, what to
    skip. "Skip" is never a judgement of the tool, only of this week: a waitlist, a beta, or work
    that needs a developer."""
    out: dict[str, dict] = {}
    for s in stories:
        card = s.get("workCard")
        if not card:
            continue
        key = week_key(s.get("firstPublishedAt") or s.get("updatedAt"))
        bucket = out.setdefault(key, {"week": key, "changed": [], "try": [], "skip": []})
        bucket["changed"].append(s)
        (bucket["skip"] if card.get("skip") else bucket["try"]).append(s)
    for bucket in out.values():
        bucket["changed"].sort(key=lambda s: s.get("firstPublishedAt") or "", reverse=True)
        bucket["try"].sort(key=lambda s: -(s["workCard"]["usefulness"]))
        bucket["skip"].sort(key=lambda s: -(s["workCard"]["usefulness"]))
        bucket["tools"] = len({tool_key(s["workCard"]["tool"], s["workCard"].get("maker")) for s in bucket["changed"]})
        # The lists below are cut to what a page shows; the counts are the week's own.
        bucket["tryCount"], bucket["skipCount"] = len(bucket["try"]), len(bucket["skip"])
        bucket["changed"] = [s["id"] for s in bucket["changed"]]
        bucket["try"] = [s["id"] for s in bucket["try"][:8]]
        bucket["skip"] = [s["id"] for s in bucket["skip"][:6]]
    return out


# ---------------------------------------------------------------------------- section briefing

BRIEFING_SIZE = 5
BRIEFING_ALSO = 6


def build_briefing(stories: list[dict], now) -> dict:
    """The section's own briefing: the most useful practical items of the last day or two.

    Its selection rule is not the main briefing's. That one asks "what is the biggest news"; this
    one asks "what can a small team do this week", and orders by usefulness (work.usefulness), so a
    free tool you can try in minutes leads over a larger launch that needs a developer. Overlap
    between the two briefings is allowed on purpose: the same launch is news on the front page and
    a thing to try here, and the two pages say different things about it.
    """
    section = section_stories(stories)

    def since(hours: int) -> list[dict]:
        cutoff = (now - timedelta(hours=hours)).isoformat().replace("+00:00", "Z")
        return [s for s in section if (s.get("firstPublishedAt") or "") >= cutoff]

    fresh, window = since(24), 24
    if len(fresh) < BRIEFING_SIZE:
        # A quiet day reaches back a second day, but only when that actually finds something, so
        # the stated window is always the window the list came from.
        wider = since(48)
        if len(wider) > len(fresh):
            fresh, window = wider, 48
    fresh.sort(key=lambda s: (-(s["workCard"]["usefulness"]), s.get("firstPublishedAt") or ""))
    # One card per tool: two stories about the same product the same day (a rollout and a feature
    # of it) read as a duplicate side by side. The more useful one stays.
    seen: set[str] = set()
    unique = []
    for s in fresh:
        key = tool_key(s["workCard"]["tool"], s["workCard"].get("maker"))
        if key not in seen:
            seen.add(key)
            unique.append(s)
    fresh = unique
    top = fresh[:BRIEFING_SIZE]
    also = fresh[BRIEFING_SIZE : BRIEFING_SIZE + BRIEFING_ALSO]
    tools = {tool_key(s["workCard"]["tool"], s["workCard"].get("maker")) for s in top + also}
    free = sum(1 for s in top + also if s["workCard"]["costKind"] in ("free", "free tier", "included"))
    return {
        "date": now.date().isoformat(),
        "generatedAt": now.isoformat().replace("+00:00", "Z"),
        "windowHours": window,
        "storyIds": [s["id"] for s in top],
        "alsoIds": [s["id"] for s in also],
        "stats": {
            "items": len(fresh),
            "tools": len(tools),
            "free": free,
            "minutes": max(2, round(sum(len(s["workCard"]["useFor"]) + 3 for s in top) / 4)),
        },
    }
