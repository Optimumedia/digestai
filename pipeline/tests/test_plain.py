"""AI at Work: the plain-words rules every card field keeps (plain.py, work.plain_card) and the simplify
pass that rewrites older cards (simplify.py). Offline: python tests/test_plain.py"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from digest import plain, simplify, work  # noqa: E402

NAMES = ["Canva Magic Studio", "Canva"]


def card(**over) -> dict:
    base = {
        "fits": True, "tool": "Canva Magic Studio", "maker": "Canva",
        "what_it_does": "Writes and lays out social posts from a short brief.",
        "who_for": ["marketer"], "use_for": ["Draft a week of posts", "Resize one ad for five places", "Write product captions"],
        "cost": "free tier", "effort": "minutes", "watch_out": "The free tier watermarks video exports.",
        "link": "https://www.canva.com/magic-studio/",
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------- length limits per field

def test_every_field_keeps_its_length_limit():
    wordy = work.clean_card(card(
        headline="Canva Magic Studio now lets small business owners and their marketing teams write and lay out posts",
        you_get=("You get a whole week of social posts drafted, laid out and ready to schedule in one short sitting, "
                 "and you can also resize them for every network. It also writes captions."),
        what_it_does=("Canva Magic Studio writes and lays out social posts from a short brief, resizes them for each "
                      "network, and suggests captions and hashtags for each one."),
        use_for=["Draft a whole week of social media posts for your shop in one go",
                 "Resize one ad for five different places without doing it by hand",
                 "Write product captions for your online store from photos", "A fourth use"],
        watch_out=("The free tier watermarks every video export, and the brand kit that keeps colours and fonts "
                   "consistent is only in the paid Pro plan.")))
    lim = plain.LIMITS
    assert lim["headline"]["min_chars"] <= len(wordy["headline"]) <= lim["headline"]["max_chars"] or \
        lim["headline_fallback"]["min_chars"] <= len(wordy["headline"]) <= lim["headline_fallback"]["max_chars"]
    assert len(wordy["headline"].split()) <= lim["headline"]["max_words"]
    assert wordy["you_get"] == "" or len(wordy["you_get"]) <= lim["you_get"]["max_chars"]
    assert len(wordy["what_it_does"]) <= lim["what_it_does"]["max_chars"] and wordy["what_it_does"]
    assert len(wordy["watch_out"]) <= lim["watch_out"]["max_chars"] and len(wordy["watch_out"]) >= 10
    assert len(wordy["use_for"]) <= 3 and all(len(u.split()) <= lim["use_for"]["max_words"] for u in wordy["use_for"])
    # Cut where a phrase can end, never on a joining word or in the middle of a list; a use with no
    # such place inside six words is left out rather than cut mid-phrase.
    assert wordy["use_for"] == ["Resize one ad", "Write product captions"], wordy["use_for"]
    assert plain.cut_phrase("Generate exact-match prompts for ChatGPT, Claude, Gemini", 6) == "Generate exact-match prompts"
    assert plain.cut_phrase("Answer customer questions in your online store automatically", 6) == "Answer customer questions"
    # The catch names a plan limit; its first part alone ("The free tier watermarks every video export")
    # would lose it, so the plan limit is what the one line says.
    assert wordy["watch_out"] == "Pro plan only", wordy["watch_out"]
    short = work.clean_card(card(watch_out="The free tier watermarks every video export, and fonts and colours may shift between exports."))
    assert short["watch_out"] == "The free tier watermarks every video export", short["watch_out"]
    exported = work.card_out(wordy)
    assert len(exported["youGet"]) <= lim["you_get"]["max_chars"]
    assert exported["whatItDoes"] == wordy["what_it_does"]


def test_the_limits_are_one_place_and_reach_the_prompts():
    from types import SimpleNamespace

    from digest import enrich

    was = dict(plain.LIMITS["watch_out"])
    try:
        plain.LIMITS["watch_out"]["max_chars"] = 64
        assert "max 64 characters" in plain.prompt_guide()["watch_out"]
        assert len(work.clean_card(card(watch_out="Only in the US for now, and the paid plan is needed for any video export at all."))["watch_out"]) <= 64
    finally:
        plain.LIMITS["watch_out"] = was
    p = enrich.build_prompt(SimpleNamespace(title="T", source_name="S", published_at=None), "word " * 50, "gemini")
    assert "45-70 characters" in p and "60-120 characters" in p and "max 80 characters" in p and "max 6 words" in p


def test_the_catch_keeps_who_cannot_use_it():
    # A shorter catch must say the same about region, plan and waitlist, or the limits label says it.
    out = work.clean_card(card(watch_out=("Not available in the EEA, the UK or Switzerland at launch, and the free tier "
                                          "watermarks every single video export you make.")))
    assert len(out["watch_out"]) <= 80
    assert work.limits(out) == ["not in the EEA, UK, Switzerland"], (out["watch_out"], work.limits(out))
    wait = work.clean_card(card(watch_out=("Right now there is a waitlist and only some accounts in a handful of "
                                           "countries have been let in, with more promised over the coming months.")))
    assert work.skip_reason(wait) == "waitlist only" and len(wait["watch_out"]) <= 80, wait["watch_out"]
    # Honest and short beats a label: the caveat's own first part.
    assert work.clean_card(card(watch_out="Requires a Semrush plan with API units and a Trends API subscription."))["watch_out"] \
        == "Requires a Semrush plan"


# ---------------------------------------------------------------------------- jargon

def test_jargon_is_swapped_or_the_field_falls_back_in_every_field():
    out = work.clean_card(card(
        headline="Leverage agentic workflows to streamline your posts",
        you_get="You get an LLM that writes your posts through our API.",
        what_it_does="Integrates with Shopify to deploy AI workflows for your product pages.",
        use_for=["Streamline your posting workflow", "Orchestrate multimodal captions", "Write product captions"],
        watch_out="Requires API access and a developer to deploy the integration."))
    assert out is not None
    for field in ("headline", "you_get", "what_it_does", "watch_out"):
        assert not plain.jargon_in(out[field], NAMES), (field, out[field])
    for use in out["use_for"]:
        assert not plain.jargon_in(use, NAMES), use
    # The swaps: a plain word that says the same thing.
    assert out["what_it_does"].startswith("Works with Shopify to set up AI routines"), out["what_it_does"]
    assert out["use_for"][0] == "Simplify your posting routine"
    assert out["use_for"] == ["Simplify your posting routine", "Write product captions"], "the jargon-only use is left out"
    assert plain.swap_jargon("An LLM drafts replies") == "An AI model drafts replies"
    assert plain.swap_jargon("It allows teams to share an integration") == "It lets teams share a connection"
    assert plain.swap_jargon("If it hallucinates, check the reply") == "If it makes things up, check the reply"
    # Acronyms: the everyday ones are fine, others are jargon, a tool's own name is never jargon.
    assert not plain.jargon_in("Export a PDF for your CRM, only in the US and EU")
    assert plain.jargon_in("Connects to your MCP server and RAG index") == ["MCP", "RAG"] or \
        set(plain.jargon_in("Connects to your MCP server and RAG index")) >= {"MCP", "RAG"}
    assert not plain.jargon_in("Run keyword research in Semrush MCP", ["Semrush MCP"])
    # Talking down is out too.
    assert not plain.plain_enough("Don't worry, it simply writes your posts")


# ---------------------------------------------------------------------------- readability

def test_the_readability_check():
    easy = plain.readability("Answer customer emails in half the time.")
    assert easy["grade"] <= plain.READ_MAX_GRADE and easy["longest"] == 7 and easy["sentences"] == 1
    hard = ("Organizational stakeholders operationalize comprehensive transformational methodologies, "
            "facilitating unprecedented interdisciplinary collaboration.")
    assert plain.readability(hard)["grade"] > 12 and plain.readability(hard)["long_share"] > 0.5
    assert not plain.readable(hard) and plain.readable("Draft customer emails in seconds, right inside Gmail.")
    # Long sentences fail even with short words.
    assert not plain.readable(" ".join(["we"] * 21) + ".")
    # Names are not what makes a line hard: "Microsoft Copilot" counts as short words.
    assert plain.readable("Summarize long emails in Microsoft Copilot Enterprise.", ["Microsoft Copilot Enterprise"])
    # A hard headline falls back to one built from the card's uses.
    out = work.clean_card(card(headline="Operationalize comprehensive omnichannel communication methodologies"))
    assert out["headline"] == "Draft a week of posts with Canva Magic Studio"


# ---------------------------------------------------------------------------- the headline

def test_the_headline_starts_with_a_verb_or_falls_back():
    good = "Reply to every Google review without writing each one"
    assert plain.headline_ok(good, NAMES, "Canva Magic Studio"), plain.headline_problems(good, NAMES)
    assert work.clean_card(card(headline=good))["headline"] == good
    for bad, why in [
        ("Google rolls out Gemini features across Workspace apps", "verb first"),
        ("Canva Magic Studio drafts a week of social posts for you", "verb first"),
        ("Draft posts", "length"),
        ("Why draft every social post by hand when AI can do it?", "verb first"),
        ("Draft these ten social posts before your coffee gets cold", "teaser"),
        ("Draft a Week Of Social Posts In One Sitting With Canva", "title case"),
        ("Draft AI social posts for your shop in one sitting", "AI"),
        ("drafting social posts for your shop in one short sitting", "verb first"),
    ]:
        assert why in plain.headline_problems(bad, NAMES, "Canva Magic Studio"), (bad, plain.headline_problems(bad, NAMES))
        assert work.clean_card(card(headline=bad))["headline"] == "Draft a week of posts with Canva Magic Studio", bad
    # "AI" inside the product's own name is fine.
    assert plain.headline_ok("Turn one photo into a week of posts with Pika AI", ["Pika AI"], "Pika AI")
    # A gerund or third-person use still builds a verb-first headline.
    assert plain.fallback_headline(card(use_for=["Answering customer product questions"], tool="Sponsored Agents")) \
        == "Answer customer product questions with Sponsored Agents"
    # No use to build one from: no headline, off the hub.
    assert plain.fallback_headline(card(use_for=["Product captions"])) == ""


# ---------------------------------------------------------------------------- the collapsed card's labels

def test_labels_button_and_example_line():
    out = work.card_out(work.clean_card(card()))
    assert [l["text"] for l in out["labels"]] == ["Free to try", "5 minutes"]
    assert work.cost_label({"cost": "free"}) == "Free"
    assert work.cost_label({"cost": "paid from $20 per month"}) == "Paid: from $20/mo"
    assert work.cost_label({"cost": "paid"}) == "Paid"
    assert work.cost_label({"cost": "not stated"}) == "Price not stated"
    assert work.cost_label({"cost": "included in a tool you already have", "included_in": "Google Workspace"}) == "Included in Google Workspace"
    # The third label only when the article says so.
    article = "Canva says the feature needs no credit card and no design skills. " * 5
    grounded = work.card_out(work.ground_card(work.clean_card(card()), article))
    assert [l["text"] for l in grounded["labels"]] == ["Free to try", "5 minutes", "No card needed"]
    assert len(work.card_out(work.clean_card(card(effort="depends")))["labels"]) == 1
    # One action, a verb and a place.
    assert out["action"] == "Open Canva Magic Studio"
    assert work.action_label({"tool": "Gemini in Gmail", "link": "https://workspace.google.com"}) == "Try it in Gmail"
    assert work.action_label({"tool": "AI Max for Search", "link": "https://ads.microsoft.com/x", "cost": "free"}) == "Try it free"
    assert work.action_label({"tool": "Recommendable AI Visibility Service", "link": "https://recommendable.ai/"}) == "Open recommendable.ai"
    assert work.action_label({"tool": "X", "link": None}) == ""
    # The example line: always "could", from the card's own use, the article's business when it names one.
    # (Its first job tile is "Get more customers": the stand-in for that tile is a café.)
    assert out["jobs"][0] == "customers" and out["scenario"] == "For example, a café could use it to draft a week of posts."
    bakery = work.ground_card(work.clean_card(card()), "A bakery in Leeds used it for a week, Canva said. " * 6)
    assert work.card_out(bakery)["scenario"] == "For example, a bakery could use it to draft a week of posts."
    assert work.card_out(work.clean_card(card(use_for=["Product captions"])))["scenario"] == ""


def test_a_developer_card_is_never_the_featured_pick():
    story = {"id": 1, "workCard": {**work.card_out(work.clean_card(card())), "effort": "needs a developer"},
             "articles": [{"id": 1, "domain": "techcrunch.com"}]}
    assert not work.featurable(story)
    story["workCard"]["effort"] = "minutes"
    assert work.featurable(story)
    off_hub = {**story, "workCard": {**story["workCard"], "hub": False}}
    assert not work.featurable(off_hub) and work.section_stories([off_hub, story]) == [story]


def test_plain_card_is_idempotent():
    once = work.clean_card(card(watch_out="Only in the US for now; the free tier watermarks every video export you make.",
                                what_it_does="Allows sellers to find prospects, enrich data, and launch outreach through chat."))
    twice = work.clean_card(once)
    for field in ("headline", "you_get", "what_it_does", "use_for", "watch_out"):
        assert once[field] == twice[field], field


# ---------------------------------------------------------------------------- the simplify pass

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
SUMMARY = ("Canva added review-to-ad writing to Magic Studio on Tuesday. Merchants paste up to 20 customer reviews "
           "and get three ad angles back. It is included in Canva Pro; the free tier gets 50 uses and watermarks "
           "video exports. Canva says a bakery cut a week of posts from 3 hours to 40 minutes.")


def _story(sid: int, days_ago: float, **card_over) -> dict:
    exported = work.card_out(work.clean_card(card(**card_over)))
    return {"id": sid, "firstPublishedAt": (NOW - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z"),
            "summaryMd": SUMMARY, "keyPoints": [], "workCard": exported,
            "articles": [{"id": sid * 10, "workCard": exported}, {"id": sid * 10 + 1, "workCard": None}]}


def _answer(prompt: str) -> dict:
    """A model that rewrites every card it is asked about, including one invented figure."""
    import json
    import re

    cards = json.loads(re.search(r"Cards:\n(.*)$", prompt, re.S).group(1))
    return {"cards": [{
        "id": c["id"],
        "headline": "Turn customer reviews into three ad angles in Canva",
        "you_get": "You get three ready ad angles from your own customer reviews in minutes.",
        "what_it_does": "Writes ad ideas from the customer reviews you paste in.",
        "use_for": ["Find ad angles in reviews", "Draft a week of posts", "Write product captions"],
        # Invented: the summary never says 500.
        "watch_out": "The free tier stops after 500 uses",
    } for c in cards]}


def test_the_simplify_pass_is_capped_and_newest_shown_first():
    stories = [_story(i, i * 0.5) for i in range(1, 16)] + [_story(99, 20)]  # one too old
    stories.append(_story(50, 1, simplified=True))  # already done
    stories[-1]["workCard"]["simplified"] = True
    briefing = {"featuredId": 7, "storyIds": [7, 3], "alsoIds": [12]}
    order = [s["id"] for s, _c, _i in simplify.candidates(stories, briefing, NOW)]
    assert order[:3] == [7, 3, 12], "the featured pick and the briefing first"
    assert 99 not in order and 50 not in order, "only the last 14 days, and never twice"
    asked, stored = [], {}

    def ask(prompt):
        asked.append(prompt)
        return _answer(prompt)

    stats = simplify.run(stories, briefing, ask=ask, store=lambda u: stored.update(u) or len(u), now=NOW, cache_data={})
    assert stats["asked"] == simplify.MAX_CARDS == 10 and len(asked) == 2, (stats, len(asked))
    assert stats["candidates"] == 15 and stats["cards"] == 10
    # Written only to the articles whose own card is the story's, and every one is marked done.
    assert set(stored) == {sid * 10 for sid in order[:10]}
    assert all(f["simplified"] is True for f in stored.values())


def test_the_simplify_pass_keeps_only_grounded_rule_keeping_fields():
    story = _story(1, 1)
    before = story["workCard"]
    new, changed = simplify.rewrite(simplify._stored_shape(before), _answer(simplify.build_prompt([(story, before, [10])]))["cards"][0],
                                    simplify.source_of(story, before))
    assert new["simplified"] is True
    assert new["headline"] == "Turn customer reviews into three ad angles in Canva" and "headline" in changed
    assert new["you_get"].startswith("You get three ready ad angles")
    assert "watch_out" not in changed and new["watch_out"] == before["watchOut"], "the invented 500 is refused"
    # A name or a figure the card and the summary do not have is refused; the old text stays.
    bad = {"headline": "Turn Instagram reviews into 12 ad angles in Canva today", "you_get": "You get ads for TikTok and Instagram in one place.",
           "use_for": ["Post to TikTok"], "what_it_does": "Writes 12 ads at once.", "watch_out": ""}
    kept, changed = simplify.rewrite(simplify._stored_shape(before), bad, simplify.source_of(story, before))
    assert changed == [] and kept["headline"] == before["headline"] and kept["simplified"] is True
    # A rewrite that breaks the rules (jargon, not a verb first) is refused the same way.
    rules = {"headline": "Canva leverages agentic workflows for your reviews", "you_get": "Seamless API access for all.",
             "use_for": [], "what_it_does": "", "watch_out": ""}
    assert simplify.rewrite(simplify._stored_shape(before), rules, simplify.source_of(story, before))[1] == []


def test_a_failed_batch_is_not_retried_every_run():
    stories = [_story(i, 1) for i in range(1, 4)]
    cache: dict = {}

    def down(_prompt):
        raise RuntimeError("no model")

    stats = simplify.run(stories, None, ask=down, store=lambda u: 0, now=NOW, cache_data=cache)
    assert stats["failed"] == 3 and len(cache) == 3
    again = simplify.run(stories, None, ask=down, store=lambda u: 0, now=NOW + timedelta(hours=1), cache_data=cache)
    assert again["candidates"] == 0, "waits RETRY_HOURS"
    later = simplify.run(stories, None, ask=_answer, store=lambda u: len(u), now=NOW + timedelta(hours=13), cache_data=cache)
    assert later["cards"] == 3 and later["written"] == 3


def test_the_simplify_prompt_is_small_and_names_the_rules():
    from digest import enrich

    stories = [_story(i, 1) for i in range(1, 6)]
    p = simplify.build_prompt([(s, s["workCard"], [s["id"] * 10]) for s in stories])
    assert "45-70 characters" in p and "no new numbers" in p and '"cards"' in p
    assert enrich.estimated_tokens(p) < 4000, enrich.estimated_tokens(p)


if __name__ == "__main__":
    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                import traceback
                print("FAIL", name, type(exc).__name__, exc)
                traceback.print_exc()
    sys.exit(1 if failures else 0)
