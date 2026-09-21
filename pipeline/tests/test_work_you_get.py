"""AI at Work: the "What you get" line and setup steps from the maker's own page (howto.py).
Offline: python tests/test_work_you_get.py"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from digest import howto, work  # noqa: E402

ARTICLE = (
    "Canva said on Tuesday that Magic Studio can now turn customer reviews into ad copy. To try it, open "
    "Magic Studio from the Canva home page, paste up to 20 customer reviews into the Magic Write panel, and "
    "ask for ad angles. Canva says one bakery cut the time it spends writing a week of social posts from "
    "3 hours to 40 minutes. The feature is included in Canva Pro and in Canva Teams at no extra cost, and a "
    "free tier gets 50 uses. Exports from the free tier carry a watermark. The update rolls out worldwide "
    "this week, the company said, starting with English-language accounts."
)


def card(**over) -> dict:
    base = {
        "fits": True,
        "tool": "Canva Magic Studio",
        "maker": "Canva",
        "what_it_does": "Writes and lays out social posts from a short brief.",
        "who_for": ["marketer", "founder"],
        "use_for": ["Draft a week of posts", "Resize one ad for five places", "Write product captions"],
        "cost": "free tier",
        "effort": "minutes",
        "watch_out": "The free tier watermarks video exports.",
        "link": "https://www.canva.com/magic-studio/",
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------- what you get

def test_a_plain_line_is_kept_and_exported_as_you_get():
    line = "Get a week of social posts drafted in one sitting instead of writing one every morning."
    stored = work.ground_card(work.clean_card(card(you_get=line)), ARTICLE)
    assert stored["you_get"] == line
    out = work.card_out(stored)
    assert out["youGet"] == line, "exported under the name the site reads"


def test_an_invented_figure_is_dropped_and_the_fallback_takes_its_place():
    # The article says 3 hours to 40 minutes; "10 hours a week" is nowhere in it.
    made_up = work.ground_card(work.clean_card(card(you_get="Save 10 hours a week on social posts.")), ARTICLE)
    assert made_up["you_get"] == ""
    assert work.card_out(made_up)["youGet"] == "Lets marketers and founders draft a week of posts."
    # The article's own figure stays.
    real = work.ground_card(work.clean_card(card(you_get="Cut a week of social posts from 3 hours to 40 minutes.")), ARTICLE)
    assert real["you_get"] == "Cut a week of social posts from 3 hours to 40 minutes."
    # A name the article never mentions goes too.
    named = work.ground_card(work.clean_card(card(you_get="Post straight to Shopify and Instagram from one screen.")), ARTICLE)
    assert named["you_get"] == ""
    ok = work.ground_card(work.clean_card(card(you_get="Turn customer reviews into ad copy inside Canva.")), ARTICLE)
    assert ok["you_get"] == "Turn customer reviews into ad copy inside Canva."
    # Without enough article text, a line with a figure cannot be checked, so it goes.
    assert work.ground_card(work.clean_card(card(you_get="Save 3 hours on posts.")), "Short.")["you_get"] == ""


def test_banned_jargon_is_rewritten_or_dropped():
    assert work.clean_card(card(you_get="Leverage your reviews to write ads that sound like your customers."))["you_get"] \
        == "Use your reviews to write ads that sound like your customers."
    for bad in ("Streamline your content workflow with AI.",
                "Workflow automation for your whole marketing team, done for you.",
                "An LLM that writes your social posts for you.",
                "Agentic marketing that runs your campaigns for you.",
                "A seamless way to unlock more content for your shop.",
                "Supercharge your posts and wow your customers!"):
        assert work.clean_card(card(you_get=bad))["you_get"] == "", bad
        assert work.YOU_GET_BANNED.search(bad) or "!" in bad
    # The rules-built line never carries jargon either: a use that has some is skipped.
    fb = work.fallback_you_get(work.clean_card(card(use_for=["Streamline your posting", "Write product captions"])))
    assert fb == "Lets marketers and founders write product captions.", fb


def test_the_fallback_is_built_from_the_cards_own_fields():
    assert work.fallback_you_get(work.clean_card(card())) == "Lets marketers and founders draft a week of posts."
    # A use that is a thing rather than an action reads "Helps ... with".
    assert work.fallback_you_get(work.clean_card(card(who_for=["support"], use_for=["Customer replies after hours"]))) \
        == "Helps support teams with customer replies after hours."
    # "sales" alone would read as a number; the tool's own name keeps its case.
    assert work.fallback_you_get(work.clean_card(card(tool="Shopify Magic", maker="Shopify", who_for=["sales", "ecommerce"],
                                                      use_for=["Shopify product pages in bulk"]))) \
        == "Helps salespeople and shop owners with Shopify product pages in bulk."
    # Cards stored before the field existed export with the rules-built line, never an empty one.
    old = work.clean_card(card())
    old.pop("you_get")
    assert work.card_out(old)["youGet"] == "Lets marketers and founders draft a week of posts."
    # An empty or "none" answer from the model is the same as no answer.
    assert work.card_out(work.clean_card(card(you_get="None")))["youGet"].startswith("Lets marketers")


def test_the_line_is_clamped_to_its_length():
    long_line = ("Answer the same customer questions once, and let it reply for you after hours. "
                 "It also drafts follow-ups, tidies your inbox, writes your newsletter and plans your posts for the month ahead.")
    out = work.clean_card(card(you_get=long_line))["you_get"]
    assert out == "Answer the same customer questions once, and let it reply for you after hours.", out
    assert len(out) <= work.YOU_GET_MAX
    # One sentence too long to cut back to a whole sentence goes; so does a fragment.
    assert work.clean_card(card(you_get="Write " + "many " * 60 + "posts"))["you_get"] == ""
    assert work.clean_card(card(you_get="Faster posts"))["you_get"] == ""
    # A missing full stop is added; quotes are taken off.
    assert work.clean_card(card(you_get="“Get replies drafted before you open your inbox”"))["you_get"] \
        == "Get replies drafted before you open your inbox."
    # The rules-built line is clamped too.
    fb = work.fallback_you_get(work.clean_card(card(use_for=["Draft " + "long " * 20 + "posts"])))
    assert len(fb) <= work.YOU_GET_MAX


# ---------------------------------------------------------------------------- steps from the maker's page

PAGE = (
    "Get started with Magic Write. Magic Write is available in every Canva design. To start, open any design "
    "in the Canva editor and select the text box you want to work on. Click Magic Write in the toolbar above "
    "the text. Type a short brief, such as the product and who it is for, and select Generate. Choose Replace "
    "to keep the draft or Try again for another version. Free accounts get 50 uses. "
) * 2


def test_a_maker_page_step_not_in_the_page_is_dropped():
    good = ["Open any design in the Canva editor and select the text box",
            "Click Magic Write in the toolbar above the text",
            "Type a short brief and select Generate"]
    assert howto.ground_steps(good, PAGE) == good
    # One step the page never describes takes the rest with it.
    assert howto.ground_steps(good + ["Connect your Shopify store under Integrations and sync inventory"], PAGE) == []
    # A menu path the page does not write, and a figure it lacks, are invented.
    assert howto.ground_steps(["Go to Settings > Apps > Magic Write", "Type a short brief and select Generate"], PAGE) == []
    assert howto.ground_steps(["Open any design in the Canva editor", "Type a brief and select Generate 200 times"], PAGE) == []
    # Too long for a step, too few, or no page text: none.
    assert howto.ground_steps(["Open any design in the Canva editor and select the text box " * 3, good[1]], PAGE) == []
    assert howto.ground_steps(good[:1], PAGE) == []
    assert howto.ground_steps(good, "Short page.") == []


def exported(story_id: int, link: str, maker: str = "Canva", domain: str = "techcrunch.com",
             source_type: str = "press", steps=None) -> dict:
    c = work.card_out(work.clean_card(card(link=link, maker=maker, steps=steps or [])))
    return {"id": story_id, "firstPublishedAt": f"2026-09-{10 + story_id % 10:02d}T10:00:00Z",
            "workCard": c,
            "articles": [{"id": story_id * 10, "domain": domain, "sourceType": source_type, "workCard": c}]}


def test_a_forum_or_news_link_is_never_fetched():
    fetched = []

    def fetch(url):
        fetched.append(url)
        return PAGE

    stories = [
        exported(1, "https://www.reddit.com/r/marketing/comments/abc/canva/"),
        exported(2, "https://news.ycombinator.com/item?id=1"),
        exported(3, "https://github.com/canva/magic"),
        exported(4, "https://x.com/canva/status/1"),
        exported(5, "https://techcrunch.com/2026/09/10/canva-magic-write/"),
        exported(6, "https://www.searchenginejournal.com/canva-magic/"),
        # A link on a site that is not the maker's, even though it is not a known news site.
        exported(7, "https://www.someblog.example/canva-review"),
    ]
    stats = howto.run(stories=stories, fetch=fetch, ask=lambda p: {"steps": []}, store=lambda u: len(u),
                      now=1_800_000_000, cache_data={})
    assert fetched == [] and stats["candidates"] == 0, (fetched, stats)
    assert howto.fetchable(stories[4]["workCard"], stories[4]) == "news"
    assert howto.fetchable(stories[0]["workCard"], stories[0]) == "community"
    assert howto.fetchable(stories[6]["workCard"], stories[6]) == "not the maker"
    # The maker's own domain is fetched, including a subdomain and a maker the rules know by name.
    assert howto.fetchable(exported(8, "https://www.canva.com/help/magic-write/")["workCard"]) is None
    assert howto.on_maker_domain("Klaviyo", "https://help.klaviyo.com/hc/en-us/articles/1")
    assert howto.on_maker_domain("Google", "https://support.google.com/mail/answer/1")
    assert not howto.on_maker_domain("Google", "https://www.canva.com/")
    assert not howto.on_maker_domain("Zoom", "https://www.zoominfo.com/")
    # A card that already has steps from the article is left alone.
    assert howto.candidates([exported(9, "https://www.canva.com/help/", steps=["Open Magic Studio from the Canva home page",
                                                                               "Paste customer reviews into Magic Write"])]) == []


def test_the_per_run_cap_is_respected_and_each_page_is_fetched_once():
    fetched, stored = [], {}

    def fetch(url):
        fetched.append(url)
        return PAGE

    def ask(prompt):
        assert "Magic Write" in prompt  # the page text reaches the model
        return {"steps": ["Open any design in the Canva editor and select the text box",
                          "Click Magic Write in the toolbar above the text"]}

    def store(updates):
        stored.update(updates)
        return len(updates)

    stories = [exported(i, f"https://www.canva.com/help/page-{i}/") for i in range(1, 10)]
    data: dict = {}
    stats = howto.run(stories=stories, fetch=fetch, ask=ask, store=store, now=1_800_000_000, limit=3, cache_data=data)
    assert len(fetched) == 3 and stats["fetched"] == 3 and stats["found"] == 3, stats
    assert len(stored) == 3 and all(v[0].startswith("Open any design") for v in stored.values())
    # The next run fetches the next three and reuses what the first found, without fetching it again.
    fetched.clear()
    stats = howto.run(stories=stories, fetch=fetch, ask=ask, store=store, now=1_800_003_600, limit=3, cache_data=data)
    assert len(fetched) == 3 and stats["reused"] == 3, stats
    assert len(set(fetched)) == 3 and not set(fetched) & {f"https://www.canva.com/help/page-{i}/" for i in (7, 8, 9)}
    # A page that failed is not fetched again the next hour, only after RETRY_DAYS.
    data.clear()
    fetched.clear()
    howto.run(stories=stories[:1], fetch=lambda u: fetched.append(u), ask=ask, store=store, now=1_800_000_000, cache_data=data)
    howto.run(stories=stories[:1], fetch=lambda u: fetched.append(u), ask=ask, store=store, now=1_800_003_600, cache_data=data)
    assert len(fetched) == 1
    howto.run(stories=stories[:1], fetch=lambda u: fetched.append(u), ask=ask, store=store,
              now=1_800_000_000 + (howto.RETRY_DAYS + 1) * 86400, cache_data=data)
    assert len(fetched) == 2
    # A model that fails leaves the card without steps and the run carries on.
    data.clear()
    stored.clear()

    def broken(prompt):
        raise RuntimeError("quota")

    stats = howto.run(stories=stories[:2], fetch=fetch, ask=broken, store=store, now=1_800_000_000, cache_data=data)
    assert stats["failed"] == 2 and stored == {}


def test_steps_source_is_exported():
    maker = work.clean_card(card(steps=["Open any design in the Canva editor", "Click Magic Write in the toolbar"],
                                 steps_source="maker"))
    out = work.card_out(maker)
    assert out["stepsSource"] == "maker" and out["steps"][0] == "Open any design in the Canva editor"
    article = work.card_out(work.clean_card(card(steps=["Open Magic Studio", "Paste reviews"])))
    assert article["stepsSource"] == "article"
    # Stored before the field existed: steps came from the article.
    old = work.clean_card(card(steps=["Open Magic Studio", "Paste reviews"]))
    old.pop("steps_source")
    assert work.card_out(old)["stepsSource"] == "article"
    # No steps, no source.
    assert "stepsSource" not in work.card_out(work.clean_card(card()))
    # Anything but "maker" from a stored card is the article.
    assert work.clean_card(card(steps=["Open Magic Studio", "Paste reviews"], steps_source="blog"))["steps_source"] == "article"


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
