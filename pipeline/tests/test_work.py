"""AI at Work (/work): card validation, the tool directory, the section briefing and the data the
section's pages are built from. Offline: python tests/test_work.py"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import update  # noqa: E402

from digest import db, work  # noqa: E402

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


def card(**over) -> dict:
    base = {
        "fits": True,
        "tool": "Canva Magic Studio",
        "maker": "Canva",
        "what_it_does": "Writes and lays out social posts from a short brief.",
        "who_for": ["marketer"],
        "use_for": ["Draft a week of posts", "Resize one ad for five places", "Write product captions"],
        "cost": "free tier",
        "effort": "minutes",
        "watch_out": "The free tier watermarks video exports.",
        "link": "https://www.canva.com/magic-studio/",
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------- card validation

def test_a_card_is_kept_only_when_it_can_keep_its_promises():
    assert work.clean_card(card()) is not None
    assert work.clean_card(None) is None
    assert work.clean_card("yes") is None
    assert work.clean_card(card(fits=False)) is None
    assert work.clean_card(card(tool="")) is None, "no tool, no card"
    assert work.clean_card(card(watch_out="")) is None, "every card owes the reader a caveat"
    assert work.clean_card(card(watch_out="None.")) is None, "'None.' is not a caveat"
    assert work.clean_card(card(what_it_does="It helps.")) is None, "one vague clause is not a sentence"
    assert work.clean_card(card(use_for=[])) is None


def test_a_card_is_clamped_to_what_the_pages_can_show():
    out = work.clean_card(card(
        who_for=["marketer", "marketer", "ceo", "sales", "support", "ops", "founder"],
        use_for=["A " * 80, "Second use", "Third use", "Fourth use"],
        tool="T" * 300, watch_out="W" * 400, what_it_does="D" * 400, link="ftp://example.com/x"))
    assert out["who_for"] == ["marketer", "sales", "support", "ops"], "unknown readers dropped, order kept, max four"
    assert len(out["use_for"]) == 3 and len(out["use_for"][0]) <= 90
    assert len(out["tool"]) == 120 and len(out["watch_out"]) == 260 and len(out["what_it_does"]) == 220
    assert out["link"] is None, "only http(s) links reach a page"
    assert work.clean_card(card(who_for=["nobody"]))["who_for"] == ["marketer"]


def test_effort_and_cost_are_read_for_their_meaning():
    assert work.clean_card(card(effort="a few minutes"))["effort"] == "minutes"
    assert work.clean_card(card(effort="You need an engineer to wire up the API"))["effort"] == "needs a developer"
    assert work.clean_card(card(effort="about half a day"))["effort"] == "an afternoon"
    assert work.clean_card(card(effort="depends"))["effort"] is None
    assert work.cost_kind("paid from $20 a month") == "paid"
    assert work.cost_kind("Free tier, then $9/mo") == "free tier"
    assert work.cost_kind("free") == "free"
    assert work.cost_kind("included in a tool you already have") == "included"
    assert work.cost_kind("") == "unknown"


def test_jobs_come_from_who_it_is_for_and_what_it_does():
    assert work.jobs_for(work.clean_card(card(who_for=["sales", "ecommerce"], use_for=["Follow up on leads"],
                                              what_it_does="Scores leads in the CRM and suggests who to call."))) == ["sell"]
    assert work.jobs_for(work.clean_card(card(who_for=["sales"], use_for=["Reply to enquiries"],
                                              what_it_does="Drafts the reply to a sales enquiry."))) == ["content", "sell"]
    assert work.jobs_for(work.clean_card(card())) == ["customers", "content"], "writing posts is making content"
    assert work.jobs_for(work.clean_card(card(who_for=["ops"], what_it_does="Reconciles invoices with the bank feed.",
                                              use_for=["Match payments"]))) == ["business"]
    assert set(work.JOBS) == {"customers", "content", "sell", "support", "business"}


def test_what_to_skip_this_week_is_stated_honestly():
    assert work.skip_reason(work.clean_card(card())) is None
    assert work.skip_reason(work.clean_card(card(effort="needs a developer"))) == "needs a developer"
    assert work.skip_reason(work.clean_card(card(watch_out="It is behind a waitlist for now."))) == "waitlist only"
    assert work.skip_reason(work.clean_card(card(watch_out="Only on enterprise plans."))) == "enterprise plans only"
    assert work.skip_reason(work.clean_card(card(watch_out="It is in beta and changes weekly."))) == "not open to everyone yet"


# Cards as the model wrote them for the live site on 18 September 2026 (Unicode hyphens included).
LIVE_DEVELOPER = {
    "SageMaker HyperPod Inference Gateway": card(
        tool="SageMaker HyperPod Inference Gateway", maker="Amazon", who_for=["founder", "ops", "support"],
        what_it_does="Routes LLM inference requests to the most suitable GPU pod using real‑time metrics.",
        use_for=["reduce first‑token latency", "increase GPU utilization", "enable multi‑model routing"],
        cost="included in a tool you already have", link=None,
        watch_out="Only works on SageMaker HyperPod clusters in AWS; not usable on other Kubernetes environments."),
    "ZCode": card(tool="ZCode", maker="Z.ai", who_for=["founder", "ops"], link=None,
                  what_it_does="Desktop AI coding assistant that runs GLM models",
                  use_for=["Refactor a project", "Explain unfamiliar files", "Roll back a change"],
                  watch_out="Disabling the checkpoints directory breaks the rollback feature."),
    "OpenCode": card(tool="OpenCode", maker="OpenCode", who_for=["founder"], cost="free", link=None,
                     what_it_does="Runs local LLMs via Ollama, LM Studio, and llama.cpp with a single command.",
                     use_for=["Try models offline", "Keep data on your machine", "Compare models"],
                     watch_out="Lacks built‑in permission controls; users must sandbox for security."),
    "Fulcra Multiplayer": card(
        tool="Fulcra Multiplayer", maker="Fulcra Dynamics", who_for=["marketer", "founder", "ops"],
        what_it_does="Enables agents from any provider to coordinate tasks using shared user‑owned context.",
        use_for=["schedule meetings across different agents", "coordinate project tasks with shared context",
                 "manage household chores via AI assistants"],
        watch_out="Requires each participant to have a compatible agent and grant permission."),
}
LIVE_KEEP = {
    "Canva": card(),
    "Gmail": card(tool="Gmail", maker="Google", who_for=["marketer", "sales", "support"],
                  what_it_does="Provides AI‑generated concise summaries for email search queries.",
                  use_for=["quickly find client email details", "summarize project threads", "extract key dates from messages"],
                  cost="included in a tool you already have", link="https://workspace.google.com/products/gmail/",
                  watch_out="Only works for English‑language accounts on paid plans; unavailable in EEA, UK, Switzerland, Japan."),
    "Gemini for Google Workspace": card(
        tool="Gemini for Google Workspace", maker="Google", who_for=["marketer", "sales", "founder"],
        what_it_does="Lets Gemini AI access data from Asana, HubSpot, Salesforce and other apps inside Workspace.",
        use_for=["pull CRM data into Docs", "generate email drafts from Mailchimp lists", "create financial reports from QuickBooks"],
        cost="included in a tool you already have", link="https://workspace.google.com/blog/",
        watch_out="Admins must enable connectors; unavailable if Gemini is disabled or in unsupported editions."),
    "HubSpot Breeze": card(tool="Breeze Copilot", maker="HubSpot", who_for=["sales", "marketer"],
                           what_it_does="Drafts follow-up emails and scores leads inside the HubSpot CRM.",
                           use_for=["Follow up after a demo", "Rank this week's leads", "Summarise a deal"],
                           link="https://www.hubspot.com/products/artificial-intelligence",
                           watch_out="Lead scoring needs a paid Sales Hub seat."),
    "Shopify Magic": card(tool="Shopify Magic", maker="Shopify", who_for=["ecommerce"],
                          what_it_does="Writes product descriptions in bulk for a store's catalogue.",
                          use_for=["Describe 50 products at once", "Rewrite old listings", "Draft a sale email"],
                          cost="included in a tool you already have", link="https://www.shopify.com/magic",
                          watch_out="Descriptions are drafts and need a human edit before publishing."),
    "Notion AI Skills": card(tool="Notion AI Skills", maker="Notion", who_for=["marketer", "founder", "ops"],
                             what_it_does="Provides a shared library of reusable AI instructions that teams can run in Notion agents.",
                             use_for=["automate monthly report generation", "standardize document critiques", "connect data into prompts"],
                             watch_out="Only available on Business and Enterprise plans; smaller plans cannot use the skill library yet."),
    # The same power as a developer tool, but with a screen for everyone: it stays.
    "No-code builder": card(tool="Zapier Agents", maker="Zapier", who_for=["ops"],
                            what_it_does="Builds AI agents with a no-code visual builder, no API work needed.",
                            use_for=["Route new leads", "Answer form replies", "Update the CRM"]),
}
E_DEGREE = card(tool="Claude AI Professional E-Degree", maker="Claude AI", who_for=["marketer", "founder", "ops"],
                what_it_does="Teaches how to use Claude for task automation via prompts, agents, and integrations.",
                use_for=["Automate email handling in Gmail", "Build AI‑powered website workflows",
                         "Create virtual assistant tasks with Chrome"],
                cost="paid from $19.99", effort="an afternoon", link=None,
                watch_out="Only works with Claude AI and MCP connectors; not compatible with other AI platforms.")


def test_developer_and_infrastructure_tools_are_kept_out_by_rules():
    for name, raw in LIVE_DEVELOPER.items():
        assert raw["fits"], "the model said these fit"
        assert work.clean_card(raw) is None, f"{name} is a developer tool"
        assert work.screen_card(raw) == (None, "developer"), name
    for name, raw in LIVE_KEEP.items():
        kept, why = work.screen_card(raw)
        assert kept is not None and why is None, f"{name} is for everyone"
    # A caveat that mentions an API does not make the tool a developer tool.
    assert work.clean_card(card(watch_out="The API costs extra; the app itself is free.")) is not None
    # A card that never was one is not counted as dropped.
    assert work.screen_card(card(watch_out="")) == (None, None)
    assert work.screen_card(None) == (None, None)


def test_courses_and_reseller_listings_are_not_tools():
    assert work.screen_card(E_DEGREE) == (None, "course"), "a third party's course, credited to 'Claude AI'"
    assert work.screen_card(card(tool="ChatGPT Mastery Bundle", what_it_does="Six online courses on prompting for $29.99.",
                                 use_for=["Learn prompts"]))[1] == "course"
    assert work.screen_card(card(tool="1min.AI", maker="1min.AI", what_it_does="Gives lifetime access to multiple AI models in one app.",
                                 use_for=["Chat with GPT and Claude"]))[1] == "course"
    assert work.clean_card(card(what_it_does="Writes captions and, of course, the hashtags that go with them.",
                                use_for=["Caption a post"])) is not None, "'of course' is not a course"
    assert work.clean_card(card(use_for=["Sell gift certificates", "Write captions", "Plan posts"])) is not None


def test_a_maker_that_does_not_match_its_link_is_taken_from_the_link():
    assert work.clean_card(card(maker="Canva Pty Ltd"))["maker"] == "Canva Pty Ltd", "the link is Canva's own"
    assert work.clean_card(card(tool="Claude for Sheets", maker="Claude AI",
                                link="https://www.stacksocial.com/sales/claude-pro"))["maker"] == "stacksocial.com"
    assert work.clean_card(card(tool="Gemini in HubSpot", maker="Google",
                                link="https://www.hubspot.com/gemini"))["maker"] == "HubSpot", "a known maker's own domain"
    assert work.clean_card(card(tool="Claude", maker="Anthropic", link="https://claude.ai/download"))["maker"] == "Anthropic"
    assert work.clean_card(card(tool="Gmail", maker="Google", link="https://workspace.google.com/x"))["maker"] == "Google"
    assert work.clean_card(card(tool="Muse", maker="Meta Platforms, Inc.", link="https://www.meta.ai/muse"))["maker"] == "Meta Platforms, Inc."
    assert work.clean_card(card(tool="Hedy", maker="Hedy AI", link="https://hedy.bot"))["maker"] == "Hedy AI", "unknown makers are not checked"
    assert work.clean_card(card(maker="Google", link=None))["maker"] == "Google", "no link, no evidence"


def test_who_cannot_use_it_is_said_without_dropping_the_card():
    gmail = work.clean_card(LIVE_KEEP["Gmail"])
    assert work.limits(gmail) == ["not in the EEA, UK, Switzerland, Japan", "Paid plans only"]
    assert work.skip_reason(gmail) is None, "most readers can use it this week"
    notion = work.clean_card(LIVE_KEEP["Notion AI Skills"])
    assert work.limits(notion) == ["Business and Enterprise plans only"]
    assert work.skip_reason(notion) is None, "a Business plan is a small team's plan"
    assert work.skip_reason(work.clean_card(card(watch_out="Only on enterprise plans."))) == "enterprise plans only"
    assert work.limits(work.clean_card(card(watch_out="UK not supported at launch."))) == ["not in the UK"]
    assert work.limits(work.clean_card(card(watch_out="Available on the Business plan and above."))) == ["Business plan and above"]
    eea = work.clean_card(card(watch_out="Not available in the European Economic Area for now."))
    assert work.limits(eea) == ["not in the EEA"] and work.skip_reason(eea) is None
    assert work.limits(work.clean_card(card())) == []
    assert work.card_out(gmail)["limits"] == work.limits(gmail)
    # A waitlist is still a reason to wait.
    assert work.skip_reason(work.clean_card(card(watch_out="Behind a waitlist, not available in the UK."))) == "waitlist only"


def test_marketer_alone_does_not_mean_get_customers():
    hedy = work.clean_card(card(tool="Hedy", maker="Hedy AI", who_for=["founder", "marketer", "ops"],
                                what_it_does="Generates meeting summaries, detailed notes and live suggestions during calls.",
                                use_for=["Create post-meeting summary", "Generate detailed meeting notes", "Get live suggestions"]))
    assert work.jobs_for(hedy) == ["business"]
    merchant = work.clean_card(card(tool="Merchant Center", maker="Google", who_for=["marketer", "founder"],
                                    what_it_does="Adds FAQs and substitutes to AI-driven product recommendations.",
                                    use_for=["Boost product visibility in AI search results", "Richer product data", "Prepare for checkout"]))
    assert "customers" in work.jobs_for(merchant)
    only_marketer = work.clean_card(card(who_for=["marketer"], what_it_does="Summarises long reports into short briefs.",
                                         use_for=["Summarise a report"]))
    assert work.jobs_for(only_marketer) == ["customers"], "a marketer's card that fits nowhere else still has a home"


def test_the_same_tool_under_two_spellings_is_one_row_and_one_briefing_card():
    assert work.tool_key("Notebooks in Gemini", "Google") == work.tool_key("Gemini Notebook", "Google")
    assert work.tool_key("Gemini desktop app", "Google") != work.tool_key("Gemini Notebook", "Google")
    a = with_card(1, {1: work.clean_card(card(tool="Notebooks in Gemini", maker="Google", link="https://gemini.google.com/"))})
    b = with_card(2, {2: work.clean_card(card(tool="Gemini Notebook", maker="Google", link=None))})
    c = with_card(3, {3: work.clean_card(card(tool="Other Thing", maker="Other"))})
    out = work.build_briefing([a, b, c], NOW)
    assert sorted(out["storyIds"]) == [1, 3] and not out["alsoIds"], "the more useful of the two notebook stories stays"
    assert len(work.build_tools([a, b, c])) == 2


def test_the_week_counts_are_the_weeks_own_not_the_cut_lists():
    stories = [with_card(i, {i: work.clean_card(card(tool=f"Tool {i}"))}) for i in range(1, 12)]
    week = next(iter(work.weeks(stories).values()))
    assert len(week["try"]) == 8 and week["tryCount"] == 11 and week["skipCount"] == 0


def test_usefulness_ranks_what_a_small_team_can_do_not_how_big_the_news_is():
    easy = work.clean_card(card())
    hard = work.clean_card(card(cost="paid from $500 a month", effort="needs a developer", link=None, maker=None,
                                use_for=["Build a custom pipeline"]))
    assert work.usefulness(easy) > work.usefulness(hard)
    # The big story is the hard one; usefulness does not care.
    big = {"importance": 10, "score": 9.0, "articleCount": 12, "contentType": "news"}
    small = {"importance": 2, "score": 0.1, "articleCount": 1, "contentType": "tutorial"}
    assert work.usefulness(easy, small) > work.usefulness(hard, big)


# ---------------------------------------------------------------------------- stories and tools

def story(sid: int, cards: dict[int, dict], **over) -> dict:
    s = {
        "id": sid, "slug": f"story-{sid}", "headline": f"Story {sid}", "leadArticleId": min(cards) if cards else None,
        "importance": 5, "score": 1.0, "articleCount": len(cards) or 1, "contentType": "news",
        "firstPublishedAt": (NOW - timedelta(hours=sid)).isoformat().replace("+00:00", "Z"),
        "updatedAt": (NOW - timedelta(hours=sid)).isoformat().replace("+00:00", "Z"),
        "articles": [{"id": aid} for aid in sorted(cards)],
    }
    s.update(over)
    return s


def with_card(sid: int, cards: dict[int, dict], **over) -> dict:
    s = story(sid, cards, **over)
    chosen = work.story_card(s, cards)
    s["workCard"] = work.card_out(chosen, s) if chosen else None
    return s


def test_a_story_takes_its_lead_articles_card_unless_another_is_fuller():
    thin = work.clean_card(card(maker=None, link=None, effort=None, use_for=["One use"]))
    full = work.clean_card(card(tool="Fuller spelling"))
    s = story(1, {10: thin, 11: full}, leadArticleId=10)
    assert work.story_card(s, {10: thin, 11: full})["tool"] == "Fuller spelling"
    same = work.clean_card(card(tool="Lead card"))
    assert work.story_card(s, {10: same, 11: full})["tool"] == "Lead card", "a lead that is as good wins"
    assert work.story_card(story(2, {}), {}) is None


def test_the_tool_directory_keeps_one_row_per_tool_newest_change_first():
    stories = [
        with_card(1, {1: work.clean_card(card(tool="Magic Studio", maker="Canva Inc.", link=None, effort=None))}),
        with_card(3, {3: work.clean_card(card(tool="Canva Magic Studio", maker="Canva"))}),
        with_card(2, {2: work.clean_card(card(tool="Zapier Agents", maker="Zapier", who_for=["ops"],
                                              what_it_does="Runs a saved workflow when a form is filled in.",
                                              use_for=["Route new leads"], cost="paid from $20 a month"))}),
    ]
    rows = work.build_tools(stories)
    assert len(rows) == 2, [r["tool"] for r in rows]
    canva = next(r for r in rows if "Canva" in r["tool"])
    assert canva["tool"] == "Canva Magic Studio", "the fuller spelling is the one the directory shows"
    assert canva["maker"] == "Canva Inc.", "the newest card's own words win where it has them"
    assert canva["link"] and canva["effort"] == "minutes", "what the newest card left out is filled in from an older one"
    assert canva["changeCount"] == 2 and [c["slug"] for c in canva["changes"]] == ["story-1", "story-3"]
    assert [r["lastChange"] for r in rows] == sorted((r["lastChange"] for r in rows), reverse=True)
    assert canva["firstSeen"] < canva["lastChange"]
    zapier = next(r for r in rows if r["tool"] == "Zapier Agents")
    assert zapier["costKind"] == "paid" and zapier["jobs"] == ["business"]


def test_the_section_is_broader_than_the_marketing_category():
    marketing_news = with_card(1, {}, category="marketing")  # a category story with nothing to act on
    agent_tool = with_card(2, {2: work.clean_card(card())}, category="agents")
    section = work.section_stories([marketing_news, agent_tool])
    assert [s["id"] for s in section] == [2]


# ---------------------------------------------------------------------------- briefing and weeks

def test_the_section_briefing_orders_by_usefulness_not_importance():
    big = with_card(1, {1: work.clean_card(card(tool="Big Launch", cost="paid from $500 a month",
                                                effort="needs a developer", link=None, maker=None,
                                                use_for=["Build a custom pipeline"]))}, importance=10, articleCount=9)
    small = with_card(2, {2: work.clean_card(card(tool="Small Free Thing"))}, importance=2, articleCount=1)
    plain_news = with_card(3, {})
    out = work.build_briefing([big, small, plain_news], NOW)
    assert out["storyIds"] == [2, 1], "the free five-minute thing leads"
    assert out["stats"]["items"] == 2 and out["stats"]["tools"] == 2 and out["stats"]["free"] == 1
    assert out["windowHours"] == 24 and out["date"] == "2026-09-17"


def test_the_section_briefing_widens_its_window_on_a_quiet_day_and_holds_at_five():
    old = [with_card(i, {i: work.clean_card(card(tool=f"Tool {i}"))},
                     firstPublishedAt=(NOW - timedelta(hours=30 + i)).isoformat().replace("+00:00", "Z"))
           for i in range(1, 9)]
    out = work.build_briefing(old, NOW)
    assert out["windowHours"] == 48 and len(out["storyIds"]) == 5 and len(out["alsoIds"]) == 3


def test_the_main_briefing_is_untouched_by_the_section():
    from digest import export

    stories = [with_card(i, {i: work.clean_card(card(tool=f"Tool {i}"))}, summaryMd="Words. " * 40,
                         pinned=False, category="marketing") for i in range(1, 9)]
    for s in stories:
        s["pinned"] = False
    main = export.build_briefing(stories, NOW)
    section = work.build_briefing(stories, NOW)
    assert main["storyIds"] and section["storyIds"]
    # Both may carry the same item: the front page says what happened, the section what to do.
    assert set(main["storyIds"]) & set(section["storyIds"])
    assert "windowHours" in main and main["stats"]["stories"] == len(stories)


def test_the_weekly_playbook_splits_what_to_try_from_what_to_skip():
    this_week = NOW  # Thursday of 2026-W38
    last_week = NOW - timedelta(days=8)
    def at(dt):
        return dt.isoformat().replace("+00:00", "Z")

    stories = [
        with_card(1, {1: work.clean_card(card(tool="Try Me"))}, firstPublishedAt=at(this_week)),
        with_card(2, {2: work.clean_card(card(tool="Waitlist Thing", watch_out="Behind a waitlist in most countries."))},
                  firstPublishedAt=at(this_week - timedelta(days=1))),
        with_card(3, {3: work.clean_card(card(tool="Old Thing"))}, firstPublishedAt=at(last_week)),
    ]
    out = work.weeks(stories)
    assert set(out) == {work.week_key(at(this_week)), work.week_key(at(last_week))}
    now_week = out[work.week_key(at(this_week))]
    assert now_week["changed"] == [1, 2] and now_week["try"] == [1] and now_week["skip"] == [2]
    assert now_week["tools"] == 2
    assert work.week_key("2026-09-17T12:00:00Z") == "2026-W38"


# ---------------------------------------------------------------------------- what the export writes

def test_the_export_writes_the_section_files_and_rides_the_existing_reads():
    import test_reads as tr
    from digest import cache, config, export

    with tr.fresh_db() as (eng, tmp):
        tr.seed(eng, stories=6, per_story=2)
        stored = work.clean_card(card())
        with eng.begin() as conn:
            conn.execute(update(db.articles).where(db.articles.c.id.in_([1, 3])).values(work_card=stored))
            conn.execute(update(db.articles).where(db.articles.c.id == 5).values(
                work_card=work.clean_card(card(tool="Zapier Agents", maker="Zapier", who_for=["ops"],
                                               what_it_does="Runs a saved workflow when a form is filled in.",
                                               use_for=["Route new leads"], cost="paid from $20 a month",
                                               effort="needs a developer"))))
            conn.execute(update(db.articles).where(db.articles.c.id == 7).values(work_card={"fits": True, "tool": "Half"}))
            # Stored before the rules existed: the export keeps it out and counts it.
            conn.execute(update(db.articles).where(db.articles.c.id == 9).values(work_card=LIVE_DEVELOPER["ZCode"]))
        config.SITE_DATA_DIR = tmp / "site"
        stats = export.run()
        site = config.SITE_DATA_DIR
        stories = json.loads((site / "stories.json").read_text(encoding="utf-8"))
        work_json = json.loads((site / "work.json").read_text(encoding="utf-8"))
        brief = json.loads((site / "work-briefing.json").read_text(encoding="utf-8"))

    carded = [s for s in stories if s.get("workCard")]
    assert len(carded) == len(work_json["storyIds"]) == stats["work"] == 3
    assert not any(a["workCard"] for s in stories for a in s["articles"] if a["id"] == 7), "a half card never ships"
    one = next(s for s in carded if s["workCard"]["tool"] == "Canva Magic Studio")
    assert one["workCard"]["jobs"] and one["workCard"]["costKind"] == "free tier" and one["workCard"]["skip"] is None
    assert stats["workTools"] == len(work_json["tools"]) == 2
    assert set(work_json["jobs"]) == set(work.JOBS)
    assert work_json["weeks"], "the playbook pages need at least one week"
    assert work_json["dropped"] == {"developer": {"count": 1, "tools": ["ZCode"]}}
    assert stats["workDropped"] == {"developer": 1}
    assert not any(t["tool"] == "ZCode" for t in work_json["tools"]), "the directory rebuild leaves it out"
    assert brief["storyIds"] and len(brief["storyIds"]) <= work.BRIEFING_SIZE
    # The section rides the reads the export already does: no new mirror or detail store.
    assert "work_card" in [c.name for c in cache.ARTICLE_TEXT_COLUMNS]
    assert cache.ARTICLE_TEXT.names.count("work_card") == 1


# ---------------------------------------------------------------------------- the admin page

def test_the_admin_health_line_is_built_from_the_exported_files():
    from digest import admin

    now = datetime(2026, 9, 22, 9, 0, tzinfo=timezone.utc)  # Tuesday of 2026-W39
    work_json = {
        "storyIds": [1, 2, 3, 4],
        "tools": [{"costKind": "free", "link": "https://a"}, {"costKind": "unknown", "link": None}],
        "weeks": {"2026-W39": {"changed": [1], "try": [1], "skip": [], "tools": 1, "tryCount": 1, "skipCount": 0},
                  "2026-W38": {"changed": [2, 3, 4], "try": [2, 3, 4], "skip": [], "tools": 3, "tryCount": 3, "skipCount": 0}},
        "jobs": {"customers": [1, 2, 3], "content": [1], "sell": [], "support": [], "business": [2, 3, 4]},
        "dropped": {"developer": {"count": 4, "tools": ["OpenCode", "ZCode"]}},
    }
    rows = [{"step": "audio", "stats": {"work": {"reason": "no time left this run"}}},
            {"step": "audio", "stats": {"work": {"reason": "older"}}}]
    w = admin.work_summary(work_json, {"windowHours": 24, "stats": {"items": 1}}, [], rows, now)
    assert (w["week"], w["cards"], w["tools"], w["try"], w["skip"]) == ("2026-W39", 1, 1, 1, 0)
    assert w["noCost"] == 1 and w["noLink"] == 1 and w["toolCount"] == 2
    assert w["thinJobs"] == ["Make content", "Sell", "Support customers"] and w["emptyJobs"] == ["Sell", "Support customers"]
    assert w["dropped"] == {"developer": 4} and w["droppedTools"] == ["OpenCode", "ZCode"]
    assert w["audioReason"] == "no time left this run", "the newest run's audio step"
    cards = admin.work_health_cards(w, now)
    assert [c["id"] for c in cards] == ["work:episode"], "last week had three items and no episode by Tuesday"
    assert "Audio step: no time left this run" in cards[0]["detail"]
    # A published episode, and a quiet section: the episode card goes, the quiet card comes.
    w = admin.work_summary(work_json, {"windowHours": 48, "stats": {"items": 0}}, [{"week": "2026-W38", "date": "2026-09-21"}], rows, now)
    assert [c["id"] for c in admin.work_health_cards(w, now)] == ["work:quiet"]
    assert admin.work_summary(None, None, None, rows, now) is None and admin.work_health_cards(None, now) == []


# ---------------------------------------------------------------------------- the weekly episode

@contextmanager
def audio_sandbox():
    """The audio step's directories in a temporary tree, with a voice that costs nothing."""
    from digest import audio, config, media

    tmp = Path(tempfile.mkdtemp())
    saved = (config.CACHE_DIR, config.SITE_DATA_DIR, media.SITE_PUBLIC, audio.AUDIO_DIR, audio.WORK_DIR,
             audio._open_engine, audio._encode, audio.engine_name)
    config.CACHE_DIR, config.SITE_DATA_DIR = tmp / "cache", tmp / "site"
    media.SITE_PUBLIC, audio.AUDIO_DIR, audio.WORK_DIR = tmp / "public", tmp / "public" / "audio", tmp / "cache" / "audio"
    media.reset()
    audio._open_engine = lambda: type("E", (), {"rate": 24000})()
    audio._encode = lambda _engine, para: (b"\x00" * 128, len(para) / 15)
    audio.engine_name = lambda: "kokoro"
    try:
        yield tmp
    finally:
        (config.CACHE_DIR, config.SITE_DATA_DIR, media.SITE_PUBLIC, audio.AUDIO_DIR, audio.WORK_DIR,
         audio._open_engine, audio._encode, audio.engine_name) = saved
        media.reset()
        shutil.rmtree(tmp, ignore_errors=True)


def _section_site(site: Path, items: int = 4) -> None:
    """What the export leaves behind: the section's stories and its work.json, nothing else."""
    site.mkdir(parents=True, exist_ok=True)
    stories, ids = [], list(range(1, items + 1))
    for i in ids:
        c = work.card_out(work.clean_card(card(tool=f"Tool {i}", maker=f"Maker {i}")))
        stories.append({"id": i, "slug": f"tool-{i}", "headline": f"Tool {i} ships",
                        "firstPublishedAt": "2026-09-15T09:00:00Z", "workCard": c})
    (site / "stories.json").write_text(json.dumps(stories), encoding="utf-8")
    (site / "work.json").write_text(json.dumps(
        {"weeks": {"2026-W38": {"changed": ids, "try": ids, "skip": [], "tools": items}}}), encoding="utf-8")


def test_the_section_episode_is_weekly_waits_its_turn_and_reads_no_database():
    from digest import audio, config, db

    monday = datetime(2026, 9, 21, 6, 0, tzinfo=timezone.utc)   # the Monday after 2026-W38
    with audio_sandbox() as tmp:
        _section_site(config.SITE_DATA_DIR)
        budget = config.AUDIO_TIME_BUDGET_SECONDS

        # Not before the chosen hour of the chosen day.
        assert audio._run_work(monday.replace(hour=0), budget)["reason"] == "before Monday 5:00 UTC"
        # The daily briefing has first call on the run's budget; the section waits for the next run.
        thin_budget = config.WORK_AUDIO_MIN_BUDGET_SECONDS - 1
        late = audio._run_work(monday, thin_budget)
        assert late["reason"] == "no time left this run" and late["generated"] == 0
        # A Monday that could not be finished is caught up later in the week, on the same week.
        assert audio.last_week(monday + timedelta(days=3)) == "2026-W38"
        assert audio._run_work(monday + timedelta(days=3), thin_budget)["reason"] == "no time left this run"

        before = db.bytes_read()
        made = audio._run_work(monday, budget)
        assert db.bytes_read() == before, "the episode is built from what the export already wrote"
        assert made["generated"] == 1 and made["week"] == "2026-W38" and made["items"] == 4
        episode = json.loads((config.SITE_DATA_DIR / "work-episodes.json").read_text())[0]
        assert episode["week"] == "2026-W38" and episode["file"] == "work-2026-W38.mp3"
        assert episode["date"] == "2026-09-21", "dated the Monday it comes out"
        assert episode["title"].startswith("AI at Work, 14-20 September 2026: Tool ")
        assert (audio.AUDIO_DIR / "work-2026-W38.mp3").exists()
        assert len(episode["transcript"].split("\n\n")) == 6   # the opening, four items, the close
        assert not (tmp / "cache" / "audio" / "work-2026-W38").exists(), "finished parts are cleared"

        # It is never read twice, and the daily briefing's own manifest is not touched.
        assert audio._run_work(monday, budget)["reason"] == "episode exists"
        assert not (config.SITE_DATA_DIR / "episodes.json").exists()


def test_a_thin_week_gets_no_episode():
    from digest import audio, config

    monday = datetime(2026, 9, 21, 6, 0, tzinfo=timezone.utc)
    with audio_sandbox():
        _section_site(config.SITE_DATA_DIR, items=config.WORK_AUDIO_MIN_ITEMS - 1)
        thin = audio._run_work(monday, config.AUDIO_TIME_BUDGET_SECONDS)
        assert thin["reason"] == "week too thin" and thin["generated"] == 0
        assert json.loads((config.SITE_DATA_DIR / "work-episodes.json").read_text()) == []


def test_the_run_gives_the_briefing_the_budget_first():
    """Both episodes come out of one AUDIO_TIME_BUDGET_SECONDS, spent in that order."""
    from digest import audio, config

    with audio_sandbox():
        _section_site(config.SITE_DATA_DIR)
        seen = []
        saved = audio._run_briefing, audio._run_work
        audio._run_briefing = lambda now, budget: seen.append(("briefing", round(budget))) or {"generated": 0}
        audio._run_work = lambda now, budget: seen.append(("work", round(budget))) or {"generated": 0}
        try:
            stats = audio.run()
        finally:
            audio._run_briefing, audio._run_work = saved
    assert [k for k, _ in seen] == ["briefing", "work"]
    assert seen[0][1] == config.AUDIO_TIME_BUDGET_SECONDS
    assert seen[1][1] <= config.AUDIO_TIME_BUDGET_SECONDS, "the section only gets what is left"
    assert "work" in stats


# ---------------------------------------------------------------------------- how the site is wired

def test_the_section_episode_has_its_own_feed_and_leaves_the_news_show_alone():
    site = Path(__file__).resolve().parents[2] / "site"
    work_feed = (site / "src" / "pages" / "work" / "podcast.xml.ts").read_text(encoding="utf-8")
    news_feed = (site / "src" / "pages" / "podcast.xml.ts").read_text(encoding="utf-8")
    # Two feeds, two shows: neither reads the other's episodes.
    assert "workEpisodes" in work_feed and "/work/podcast.xml" in work_feed
    assert "workEpisodes" not in news_feed and "episodes" in news_feed
    assert "digestai-work-" in work_feed, "its own guids, so no app sees one show's episode twice"
    assert 'itunes:category text="Business"' in work_feed and 'itunes:category text="News"' in news_feed
    # The player, the listing and the playbook link.
    assert "latestWorkEpisode" in (site / "src" / "pages" / "work" / "index.astro").read_text(encoding="utf-8")
    assert "workEpisodes" in (site / "src" / "pages" / "listen.astro").read_text(encoding="utf-8")
    assert "workEpisodeFor" in (site / "src" / "pages" / "work" / "week" / "[week].astro").read_text(encoding="utf-8")
    assert 'readJson<Episode[]>("work-episodes.json"' in (site / "src" / "lib" / "work.ts").read_text(encoding="utf-8")


def test_generic_assistant_cards_are_not_news():
    from digest import work

    base = {"fits": True, "maker": "OpenAI", "who_for": ["founders"], "cost": "free", "effort": "minutes",
            "watch_out": "Answers can be wrong.", "use_for": ["ask for dinner ideas"], "link": "https://chatgpt.com"}
    for tool, does in [("ChatGPT", "generates text responses to user prompts"),
                       ("ChatGPT", "generates draft text that can be edited into personalized content"),
                       ("Claude (Anthropic)", "answers questions and writes emails for you")]:
        card, why = work.screen_card({**base, "tool": tool, "what_it_does": does})
        assert card is None and why == "nothing new", (tool, does, why)
    for tool, does in [("ChatGPT", "now schedules tasks and sends you the results each morning"),
                       ("Gemini", "adds a Help me write button to Gmail replies for Workspace users"),
                       ("Canva", "generates on-brand social posts from your brand kit")]:
        card, why = work.screen_card({**base, "tool": tool, "what_it_does": does})
        assert card is not None, (tool, does, why)


# ---------------------------------------------------------------------------- the teaching fields

ARTICLE = (
    "Canva said on Tuesday that Magic Studio can now turn customer reviews into ad copy. To try it, open "
    "Magic Studio from the Canva home page, paste up to 20 customer reviews into the Magic Write panel, and "
    "ask for ad angles. Canva says one bakery cut the time it spends writing a week of social posts from "
    "3 hours to 40 minutes. The feature is included in Canva Pro and in Canva Teams at no extra cost, and a "
    "free tier gets 50 uses. Exports from the free tier carry a watermark. The update rolls out worldwide "
    "this week, the company said, starting with English-language accounts."
)


def test_the_teaching_fields_are_clamped():
    long_prompt = ("Here are 20 reviews of my bakery. Find three angles a Facebook ad could use, each with a headline. "
                   "Keep each under 12 words. " * 6)
    out = work.clean_card(card(prompt=long_prompt, steps=["1. Open Magic Studio", "Step 2: paste reviews", "Ask for angles",
                                                        "Pick one", "Publish it"],
                               example={"before": "3 hours", "after": "40 minutes"}, included_in="Canva Pro",
                               headline="Turn 20 customer reviews into three ad angles"))
    assert out["prompt"] and len(out["prompt"]) <= work.PROMPT_MAX, len(out["prompt"])
    assert out["prompt"].endswith((".", "?")), "cut back to a whole sentence, never mid-word"
    assert out["steps"] == ["Open Magic Studio", "paste reviews", "Ask for angles", "Pick one"], "numbering off, four at most"
    assert out["headline"] == "Turn 20 customer reviews into three ad angles"
    assert out["included_in"] == "Canva Pro"
    # One long unbroken prompt cannot be cut back to a sentence: it goes.
    assert work.clean_card(card(prompt="word " * 120))["prompt"] == ""
    assert work.clean_card(card(prompt="None"))["prompt"] == "" and work.clean_card(card(prompt=None))["prompt"] == ""
    # A single step, a paragraph for a step, or half an example is none at all.
    assert work.clean_card(card(steps=["Open it"]))["steps"] == []
    assert work.clean_card(card(steps=["Open it", "x " * 100]))["steps"] == []
    assert work.clean_card(card(example={"before": "3 hours", "after": ""}))["example"] is None
    assert work.clean_card(card(example="faster"))["example"] is None
    assert work.clean_card(card(included_in="not stated"))["included_in"] == ""
    # Older stored cards have none of it and still export, with nothing the site has to guard against.
    plain = work.card_out(work.clean_card(card()))
    assert "steps" not in plain and "prompt" not in plain and "example" not in plain and plain["includedIn"] == ""


def test_the_exported_card_uses_the_names_the_site_reads():
    out = work.card_out(work.ground_card(work.clean_card(card(
        prompt="Here are my customer reviews. Suggest three ad angles, each with a one-line headline.",
        steps=["Open Magic Studio from the Canva home page", "Paste up to 20 customer reviews into Magic Write"],
        example={"before": "3 hours writing a week of social posts", "after": "40 minutes"},
        included_in="Canva Pro", headline="Turn 20 customer reviews into three ad angles")), ARTICLE))
    assert out["steps"] == ["Open Magic Studio from the Canva home page", "Paste up to 20 customer reviews into Magic Write"]
    assert out["prompt"].startswith("Here are my customer reviews")
    assert out["example"] == {"before": "3 hours writing a week of social posts", "after": "40 minutes"}
    assert out["includedIn"] == "Canva Pro" and out["headline"] == "Turn 20 customer reviews into three ad angles"


def test_steps_the_article_does_not_describe_are_dropped_whole():
    grounded = work.clean_card(card(steps=["Open Magic Studio from the Canva home page",
                                           "Paste customer reviews into the Magic Write panel",
                                           "Ask for ad angles"]))
    assert len(work.ground_card(grounded, ARTICLE)["steps"]) == 3
    # One invented step takes the others with it: half a how-to teaches the wrong thing.
    invented = work.clean_card(card(steps=["Open Magic Studio from the Canva home page",
                                           "Connect your Shopify store under Integrations and sync inventory"]))
    assert work.ground_card(invented, ARTICLE)["steps"] == []
    # A UI path the article never wrote is invented, however plausible its words.
    path = work.clean_card(card(steps=["Go to Settings > Brand Kit > Reviews", "Paste customer reviews into Magic Write"]))
    assert work.ground_card(path, ARTICLE)["steps"] == []
    # A figure the article does not have ("50 reviews") fails the step too.
    figure = work.clean_card(card(steps=["Paste 45 customer reviews into Magic Write", "Ask for ad angles from the reviews"]))
    assert work.ground_card(figure, ARTICLE)["steps"] == []
    # Without the article's text there is nothing to check against, so the steps go.
    assert work.ground_card(grounded, "Canva launches a thing.")["steps"] == []
    # The same check runs on the way in (screen_card with the source) and in enrich.verify.
    assert work.screen_card(card(steps=["Connect Shopify under Integrations", "Sync inventory nightly"]), ARTICLE)[0]["steps"] == []


def test_an_example_with_an_invented_figure_is_dropped():
    real = work.clean_card(card(example={"before": "3 hours on a week of social posts", "after": "40 minutes"}))
    assert work.ground_card(real, ARTICLE)["example"] == {"before": "3 hours on a week of social posts", "after": "40 minutes"}
    made_up = work.clean_card(card(example={"before": "3 hours on a week of social posts", "after": "15 minutes"}))
    assert work.ground_card(made_up, ARTICLE)["example"] is None
    off_topic = work.clean_card(card(example={"before": "Manual invoices every month", "after": "Automatic payroll runs"}))
    assert work.ground_card(off_topic, ARTICLE)["example"] is None, "a before/after the article never mentions"


def test_the_plan_it_comes_with_must_be_named_in_the_article():
    assert work.ground_card(work.clean_card(card(included_in="Canva Pro")), ARTICLE)["included_in"] == "Canva Pro"
    assert work.ground_card(work.clean_card(card(included_in="Canva Enterprise")), ARTICLE)["included_in"] == ""
    assert work.ground_card(work.clean_card(card(included_in="Google Workspace Business Standard")), ARTICLE)["included_in"] == ""


def test_the_outcome_headline_falls_back_to_the_tool_and_what_it_does():
    base = dict(tool="Canva Magic Studio", what_it_does="Writes and lays out social posts from a short brief.")
    # No headline (an older stored card, or the model left it out): the tool reads on into what it does.
    assert work.clean_card(card(**base))["headline"] == "Canva Magic Studio writes and lays out social posts from a short brief"
    assert work.card_out({**work.clean_card(card(**base)), "headline": None})["headline"].startswith("Canva Magic Studio writes")
    # Too long, hype the rules cannot rescue, or an empty answer: the same fallback.
    assert work.clean_card(card(**base, headline="Turn reviews into ads " * 6))["headline"].startswith("Canva Magic Studio writes")
    assert work.clean_card(card(**base, headline="None"))["headline"].startswith("Canva Magic Studio writes")
    assert work.clean_card(card(**base, headline="WOW!"))["headline"].startswith("Canva Magic Studio writes")
    # Hype words come out through the same rules as news headlines.
    h = work.clean_card(card(**base, headline="Revolutionary way to turn reviews into ad angles"))["headline"]
    assert "evolutionary" not in h.lower() and "ad angles" in h, h
    # A sentence that does not start with a verb gets a colon; one naming the tool keeps its words.
    assert work.fallback_headline("Zapier Agents", "An assistant for routing leads.") == "Zapier Agents: An assistant for routing leads"
    assert work.fallback_headline("Zapier Agents", "Zapier Agents routes new leads.") == "Zapier Agents routes new leads"
    assert len(work.fallback_headline("T", "Writes " + "very " * 40 + "long things.")) <= work.HEADLINE_MAX + 10
    # A headline figure the article does not have goes back to the fallback.
    wrong = work.clean_card(card(**base, headline="Turn 35 customer reviews into three ad angles"))
    assert work.ground_card(wrong, ARTICLE)["headline"].startswith("Canva Magic Studio writes")
    right = work.clean_card(card(**base, headline="Turn 20 customer reviews into three ad angles"))
    assert work.ground_card(right, ARTICLE)["headline"] == "Turn 20 customer reviews into three ad angles"


def test_enrich_verify_grounds_the_card_even_with_checks_off():
    from types import SimpleNamespace

    from digest import config, enrich

    clean = {"headline": "Canva adds review-to-ad writing to Magic Studio", "summary_md": ARTICLE, "key_points": [],
             "why_it_matters": "", "entities": {}, "work_card": work.clean_card(card(
                 steps=["Connect Shopify under Integrations", "Sync inventory nightly"],
                 example={"before": "3 hours", "after": "15 minutes"}))}
    was = config.CHECK_SUMMARIES
    try:
        for on in (True, False):
            config.CHECK_SUMMARIES = on
            out, _note = enrich.verify(None, SimpleNamespace(id=1, title="Canva"), ARTICLE, clean, None, {}, {},
                                       {"checked": 0, "flagged": 0, "retried": 0, "edited": 0})
            assert out["work_card"]["steps"] == [] and out["work_card"]["example"] is None, on
    finally:
        config.CHECK_SUMMARIES = was


def test_the_prompt_asks_for_the_teaching_fields_and_still_fits_the_local_window():
    from types import SimpleNamespace

    from digest import config, enrich

    row = SimpleNamespace(title="T", source_name="S", published_at=None)
    for provider in ("gemini", "groq", "ollama"):
        p = enrich.build_prompt(row, "word " * 20000, provider)
        for field in ('"headline": what the reader gets', '"prompt"', '"steps"', '"example"', '"included_in"'):
            assert field in p, (provider, field)
        cap = config.PROMPT_TOKEN_BUDGET.get(provider)
        assert cap is None or enrich.estimated_tokens(p) <= cap, (provider, enrich.estimated_tokens(p))


def test_google_s_gemini_domain_keeps_google_as_the_maker():
    # The Gemini for Windows card (blog.google, 16 Sep) credited "gemini.google" because the link's
    # domain was not on Google's list.
    assert work.check_maker({"maker": "Google", "link": "https://gemini.google/desktop"}) == ("Google", False)


# ---------------------------------------------------------------------------- the featured pick

def _community_story(sid: int, maker: str, **over) -> dict:
    s = with_card(sid, {sid: work.clean_card(card(tool="Veloop", maker=maker, link="https://github.com/x/veloop",
                                                  what_it_does="Fakes an attentive webcam feed during video calls.",
                                                  cost="free", effort="minutes"))}, **over)
    s["articles"] = [{"id": sid, "domain": "reddit.com", "via": "r/SideProject", "author": maker}]
    return s


def test_a_forum_handle_can_never_be_the_featured_pick():
    hobby = _community_story(1, "MoistTonight3997")
    real = with_card(2, {2: work.clean_card(card(tool="Canva Magic Studio", cost="paid from $15 a month", effort="an afternoon"))})
    real["articles"] = [{"id": 2, "domain": "techcrunch.com", "via": None, "author": "Jane Reporter"}]
    assert work.username_maker("MoistTonight3997") and work.username_maker("u/somebody") and work.username_maker("jane_doe")
    assert not work.username_maker("Canva") and not work.username_maker("Google DeepMind") and not work.username_maker("3M")
    assert work.username_maker("sidequest", hobby | {"articles": [{"domain": "reddit.com", "author": "sidequest"}]})
    assert not work.featurable(hobby) and work.featurable(real)
    out = work.build_briefing([hobby, real], NOW)
    assert out["storyIds"][0] == 2 and out["featuredId"] == 2, out
    assert 1 in out["storyIds"], "the hobby project stays in the list, just not in front"
    # No link, or only a Reddit post as coverage: no featured pick, but a named maker still leads.
    no_link = with_card(3, {3: work.clean_card(card(tool="Quiet Tool", maker="Quiet Co", link=None))})
    only_forum = with_card(4, {4: work.clean_card(card(tool="Forum Tool", maker="Forum Co"))})
    only_forum["articles"] = [{"id": 4, "domain": "reddit.com", "via": "r/marketing"}]
    out = work.build_briefing([hobby, no_link, only_forum], NOW)
    assert out["featuredId"] is None and out["storyIds"][0] != 1, out
    # The week's first "try" follows the same rule.
    week = work.weeks([hobby, real])[work.week_key(hobby["firstPublishedAt"])]
    assert week["try"][0] == 2


# ---------------------------------------------------------------------------- reader events on /work

def test_the_work_events_are_accepted_by_the_database_rules():
    from digest import db as dbm

    for t in ("try", "copy_prompt", "expand", "next_click"):
        assert t in dbm.PUBLIC_EVENT_TYPES and t in dbm.DETAIL_EVENT_TYPES
    policy = "\n".join(dbm.EVENTS_POLICY_SQL)
    assert "'try', 'copy_prompt', 'expand', 'next_click'" in policy
    assert "detail IS NULL OR type IN ('search', 'depth', 'try', 'copy_prompt', 'expand', 'next_click')" in policy
    # A /work page sends no story: the guard only checks a story_id that is given.
    assert "new.story_id is not null and not exists" in dbm.EVENTS_GUARD_SQL
    schema = (Path(__file__).resolve().parents[2] / "supabase" / "schema.sql").read_text(encoding="utf-8")
    assert "'search', 'depth', 'try', 'copy_prompt', 'expand', 'next_click')" in schema
    assert "detail is null or type in ('search', 'depth', 'try', 'copy_prompt', 'expand', 'next_click')" in schema
    # The site sends them under these attributes (site/public/app.js).
    js = (Path(__file__).resolve().parents[2] / "site" / "public" / "app.js").read_text(encoding="utf-8")
    for attr in ("data-work-try", "data-work-copy", "data-work-howto", "data-work-next"):
        assert attr in js, attr
    for t in ('"try"', '"copy_prompt"', '"expand"', '"next_click"'):
        assert f"send({t}" in js, t


def test_the_admin_work_engagement_line_is_one_narrow_query():
    import test_reads as tr
    from sqlalchemy import insert

    from digest import admin

    now = datetime.now(timezone.utc)
    with tr.fresh_db() as (eng, _tmp):
        rows = [
            {"type": "view", "path": "/work", "value": 1},
            {"type": "view", "path": "/work/tools", "value": 1},
            {"type": "view", "path": "/story/x", "value": 1, "story_id": None},  # not a /work page
            {"type": "view", "path": "/workshop", "value": 1},                   # nor this one
            {"type": "depth", "path": "/work", "value": 40, "detail": "top"},
            {"type": "depth", "path": "/work/week/2026-W38", "value": 100, "detail": "end"},
            {"type": "dwell", "path": "/work", "value": 30},
            {"type": "try", "path": "/work", "value": 1, "detail": "Canva Magic Studio"},
            {"type": "try", "path": "/story/x", "value": 1, "detail": "Canva Magic Studio"},  # a card on a story page
            {"type": "copy_prompt", "path": "/work", "value": 1, "detail": "Canva Magic Studio"},
            {"type": "expand", "path": "/work", "value": 1, "detail": "Canva Magic Studio"},
            {"type": "try", "path": "/work", "value": 1, "created_at": now - timedelta(days=9)},  # outside the window
        ]
        with eng.begin() as conn:
            conn.execute(insert(db.events), [{"session": f"s{i}", "created_at": now, **r} for i, r in enumerate(rows)])
        with eng.connect() as conn:
            got = admin.work_engagement(conn, now - timedelta(days=7))
    assert got == {"views": 2, "depthAvg": 70, "depthReads": 2, "tries": 2, "copies": 1, "expands": 1, "nextClicks": 0}, got
    assert admin.work_engagement_summary([])["depthAvg"] is None


if __name__ == "__main__":
    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print("FAIL", name, type(exc).__name__, exc)
    sys.exit(1 if failures else 0)
