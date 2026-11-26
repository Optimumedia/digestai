"""Funding tracker rules: python tests/test_funding.py (offline). The examples are the rows the live
/funding page showed on 16 September 2026: one Mistral round as 74 rows, a $1B fund as $10B, a robot
launch as an acquisition, and a 2025 acquisition as news."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from digest import funding  # noqa: E402

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)


def deal(company, amount=None, rnd="other", valuation=None, investors=()):
    return {"company": company, "amount_usd": amount, "round": rnd, "investors": list(investors), "valuation_usd": valuation}


def article(aid, title, f, points=(), summary="", date="2026-09-15T08:00:00Z", lead=False):
    return {"id": aid, "title": title, "headline": title, "funding": f, "keyPoints": list(points), "summaryMd": summary,
            "description": None, "publishedAt": date, "isLead": lead}


def story(slug, headline, articles, first="2026-09-15T08:00:00Z"):
    return {"slug": slug, "headline": headline, "firstPublishedAt": first, "articles": articles}


def test_company_names_share_a_key():
    assert funding.company_key("Mistral AI") == funding.company_key("Mistral") == funding.company_key("MISTRAL AI Inc.")
    assert funding.company_key("Mistral AI SAS") == funding.company_key("Mistral AI S.A.S.") == funding.company_key("Mistral AI")  # two rows on 8 Sept
    assert funding.company_key("Aleph Alpha GmbH") == funding.company_key("Aleph Alpha") and funding.company_key("Sakana AI") != funding.company_key("Sana")
    assert funding.company_key("xAI") != funding.company_key("Anthropic")
    assert funding.company_key("Humane") != funding.company_key("Human")


def test_money_in_text_reads_currencies_and_scales():
    assert any(funding.close(v, 3.3e9) for v in funding.money_in("Mistral raises €3B"))
    assert any(funding.close(v, 3e9) for v in funding.money_in("Mistral raises €3B"))  # stored unconverted
    assert funding.money_in("$116M") == [116e6]
    assert funding.money_in("a $1.2 trillion market") == [1.2e12]
    assert funding.money_in("3 billion euros")[0] > 3e9
    assert funding.money_in("116 million") == [116e6]
    assert funding.money_in("1,500 million dollars") == [1.5e9]
    assert funding.money_in("version 3 and a 4B model with 3 tests") == []  # sizes and counts are not money
    assert funding.amount_in_text(1e9, "a $1 billion fund") and not funding.amount_in_text(10e9, "a $1 billion fund")


def test_real_deals_are_kept():
    assert funding.is_deal(deal("Mistral AI", 3.3e9, "series_c", 24e9), "Mistral AI raises €3B at $24B valuation",
                           "The round values Mistral at $24B.", NOW) is None
    assert funding.is_deal(deal("Groq", 20e9, "acquisition"), "Nvidia acquires Groq for $20B",
                           "Nvidia is paying $20 billion.", NOW) is None
    # Undisclosed amounts are real deals too.
    assert funding.is_deal(deal("Acme", None, "seed", investors=["a16z"]), "Acme raises seed round led by a16z", "", NOW) is None
    # The row links to its story, so the story headline must be about the deal: a funding verb in the
    # text no longer carries a neutral headline (26 Nov: "Osmo claims its AI smell model matches human
    # detection ability" was listed as a $9M round).
    assert funding.is_deal(deal("Acme", 50e6, "series_a"), "Acme's next act", "Acme raised $50M in a Series A.", NOW) \
        == "nothing about a deal in the headline"


def test_mistakes_from_the_live_page_are_dropped():
    # Radical Ventures: the headline said $1B, the row $10B.
    why = funding.is_deal(deal("Radical Ventures", 10e9), "Radical Ventures raises $1B fund for AI startups", "A $1 billion fund.", NOW)
    assert why and "amount" in why, why
    # Agility Robotics: a product launch labelled an acquisition.
    why = funding.is_deal(deal("Agility Robotics", 620e6, "acquisition"), "Agility Robotics launches Digit humanoid for warehouses",
                          "Digit costs $620M to develop.", NOW)
    assert why and "product" in why, why
    # An "acquisition" whose headline does not say so.
    why = funding.is_deal(deal("Acme", 100e6, "acquisition"), "Acme raises $100M", "$100M round.", NOW)
    assert why and "acquisition" in why, why
    # Humane: a 2025 deal retold in 2026.
    why = funding.is_deal(deal("Humane", 116e6, "acquisition"), "HP bought Humane for $116M, a look back",
                          "HP acquired Humane for $116M in 2025.", NOW)
    assert why and "earlier year" in why, why
    assert "earlier year" in funding.is_deal(deal("Humane", 116e6, "acquisition"), "HP bought Humane for $116M last year", "", NOW)
    # An article from weeks before the story is not that story's news.
    why = funding.is_deal(deal("Acme", 50e6, "series_a"), "Acme raises $50M", "", NOW, "2026-08-10T00:00:00Z", "2026-09-15T00:00:00Z")
    assert why and "story's time" in why, why
    # The valuation stored as the amount.
    why = funding.is_deal(deal("Mistral", 24e9, "series_c", 24e9), "Mistral raises €3B at $24B valuation", "", NOW)
    assert why and "valuation" in why, why
    # 17 Sep live page: a company the story is not about, and deals not done yet.
    assert funding.is_deal(deal("Anthropic", 100e9, "ipo"), "OpenAI delays IPO citing security risks after model escapes",
                           "Anthropic could raise $100B in an IPO.", NOW) == "company not named in the headline"
    assert funding.is_deal(deal("Anthropic", 10e9), "Nvidia considers $10B investment in Anthropic to secure AI chips",
                           "$10B investment.", NOW) == "not a done deal"
    assert funding.is_deal(deal("Nscale", 3.5e9), "Nscale appoints Fidji Simo to board ahead of potential $3.5B round",
                           "$3.5B round.", NOW) == "not a done deal"
    assert "IPO" in funding.is_deal(deal("Acme", 1e9, "ipo"), "Acme raises $1B from investors", "$1B.", NOW)
    assert funding.is_deal(deal("OpenAI", 110e9, "series_d_plus"), "SoftBank secures $12B loan to fund massive OpenAI investment",
                           "OpenAI's $110B round.", NOW) is None
    assert funding.is_deal(deal("unknown", 1e9), "Someone raises $1B", "", NOW) == "no company"
    assert "implausible" in funding.is_deal(deal("Acme", 5e12), "Acme raises $5 trillion", "", NOW)


def test_one_mistral_round_is_one_row():
    text = "Mistral AI raised €3B in a Series C led by ASML, valuing the company at $24B."
    arts = [
        article(1, "Mistral AI raises €3B Series C at $24B valuation", deal("Mistral AI", 3.3e9, "series_c", 24e9, ["ASML"]), [text], lead=True),
        article(2, "Mistral raises €3B", deal("Mistral", 3.3e9, "series_c", 24e9, ["ASML", "a16z"]), [text]),
        article(3, "Mistral closes $3.5 billion round", deal("Mistral AI", 3.5e9, "series_c"), ["Mistral closed a $3.5 billion round."]),
        # Wrong figures the coverage produced: the valuation as the amount, a stray number, a wrong round.
        article(4, "Mistral is now worth $30B after raising €3B", deal("Mistral AI", 30e9, "series_a"), ["Mistral is now worth $30B."]),
        article(5, "Mistral raises €3B", deal("Mistral AI", 441e6, "series_a"), ["Its revenue is $441M."]),
        article(6, "Mistral raises €3B", deal("Mistral", 3.3e9, "acquisition"), [text]),
        article(7, "Mistral raises €3B", deal("Mistral", 2.1e9, "series_c", 2.1e9), ["Mistral raised $2.1B."]),
        article(8, "Mistral raises new round", deal("Mistral", None, "series_c", investors=["ASML", "a16z", "Nvidia"]), ["Terms were not disclosed."]),
    ]
    later = [article(9, "Mistral adds investors to its €3B round", deal("Mistral AI", 3.4e9, "series_c", 24e9, ["ASML", "a16z"]),
                     ["Mistral's €3B round gains investors."], date="2026-09-17T08:00:00Z", lead=True)]
    rows, dropped = funding.build([story("mistral", "Mistral AI raises €3B at $24B valuation", arts),
                                   story("mistral-2", "Mistral adds investors to its €3B round", later, first="2026-09-17T08:00:00Z")], NOW)
    assert len(rows) == 1, rows
    row = rows[0]
    # The figure as the headline states it, €3B, with its dollar value (an approximate conversion).
    assert (row["company"], row["amount_stated"], row["round"], row["valuation_usd"]) == ("Mistral AI", "€3B", "series_c", 24e9), row
    assert funding.close(row["amount_usd"], 3.3e9, 0.06), row
    assert row["investors"] == ["ASML", "a16z", "Nvidia"] and row["date"] == "2026-09-15T08:00:00Z"
    # 1, 2, 3, 8 and 9, and 4 and 5 too: their stray figures give way to the one the story headline
    # states (€3B). 6 and 7 are dropped by the rules.
    assert row["sources"] == 7, row["sources"]
    assert dropped == {"called an acquisition, but the headline does not say so": 1, "amount is the valuation": 1}, dropped
    assert funding.total(rows) == row["amount_usd"]


def test_different_deals_of_one_company_stay_apart():
    arts = [article(1, "Acme raises $50M Series A", deal("Acme", 50e6, "series_a"), ["Acme raised $50M."], date="2026-06-01T00:00:00Z", lead=True)]
    later = [article(2, "Acme raises $200M Series B", deal("Acme", 200e6, "series_b"), ["Acme raised $200M."], date="2026-09-10T00:00:00Z", lead=True)]
    rows, _ = funding.build([story("acme-a", "Acme raises $50M Series A", arts, first="2026-06-01T00:00:00Z"),
                             story("acme-b", "Acme raises $200M Series B", later, first="2026-09-10T00:00:00Z")], NOW)
    assert [(r["amount_usd"], r["round"]) for r in rows] == [(200e6, "series_b"), (50e6, "series_a")]
    assert funding.total(rows) == 250e6


def test_page_total_adds_only_kept_rows():
    stories = [
        story("groq", "Nvidia acquires Groq for $20B", [article(1, "Nvidia acquires Groq for $20B", deal("Groq", 20e9, "acquisition"), ["$20 billion deal."], lead=True)]),
        story("radical", "Radical Ventures raises $1B fund", [article(2, "Radical Ventures raises $1B fund", deal("Radical Ventures", 10e9), ["A $1 billion fund."], lead=True)]),
        story("agility", "Agility Robotics launches Digit", [article(3, "Agility Robotics launches Digit", deal("Agility Robotics", 620e6, "acquisition"), [], lead=True)]),
        story("seed", "Acme raises seed round", [article(4, "Acme raises seed round", deal("Acme", None, "seed"), [], lead=True)]),
    ]
    rows, dropped = funding.build(stories, NOW)
    # Radical Ventures stays, with the $1B its headline states instead of the extraction's $10B.
    assert [(r["company"], r["amount_usd"]) for r in rows] == [("Groq", 20e9), ("Radical Ventures", 1e9), ("Acme", None)], rows
    assert funding.total(rows) == 21e9
    assert sum(dropped.values()) == 1


# ---- 26 Nov 2026 audit: rows the live /funding page showed (one in four sampled, 23 of 96 wrong) ----

NOV = datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)


def check(f, headline, text=""):
    return funding.review(f, headline, text, NOV, check_amount=False)


def test_headlines_about_something_else_are_not_deals():
    cases = [
        (deal("Prometheus", 18e9), "Sources say Bezos' Prometheus is building agentic AI engineering system"),
        (deal("Databricks", 100e6), "Databricks outlines three ways to use OpenAI tools with company data"),
        (deal("Meta Platforms", 9e9), "Meta re-evaluates 2030 net zero goal as AI emissions rise 45%"),
        (deal("Meta", 145e9), "Meta forms sales team to sell AI tools to businesses amid $145 billion infrastructure spend"),
        (deal("Anthropic", 10e9, "ipo"), "Nvidia CEO Jensen Huang resigns amid affairs and espionage allegations, shaking AI industry"),
        (deal("Every", 2e6, "seed"), "Axios and Every restructure content for AI agents as primary audience"),
        (deal("Eclypsium", 100e6), "Eclypsium partners with Nvidia to secure AI infrastructure against hardware-level attacks"),
        (deal("Blomma for Organizations"), "AMD acquires World Labs to boost AI hardware-software collaboration"),
        (deal("Nvidia-backed UK startups (five unnamed)", 1.2e9), "Nvidia invests $30B in OpenAI and guarantees $105B for its Ohio data center"),
    ]
    for f, head in cases:
        row, why = check(f, head)
        assert row is None and why, (head, row)


def test_results_contracts_bonds_and_steps_around_old_deals_are_not_deals():
    cases = [
        (deal("CoreWeave", 4.2e9), "CoreWeave Q3 revenue climbs 168% to $3.661 billion as backlog more than doubles"),
        (deal("SoftBank Group", 3.7e9), "SoftBank reports $7 billion net loss in second quarter"),
        (deal("Corvex", 103e6), "Corvex signs $103M three-year deal for NVIDIA Blackwell inference platform"),
        (deal("ChronoScale", 750e6), "ChronoScale signs 350 MW AI data center deal with STN in Texas"),
        (deal("Helm.ai", 70e6), "Helm.ai secures $70M in commercial contracts for physical AI models"),
        (deal("Microsoft", 80e9), "Microsoft commits to funding its own data center grid upgrades in Wisconsin"),
        (deal("Alphabet", 6.7e9), "Alphabet sells €6B in bonds to fund AI infrastructure amid market uncertainty"),
        (deal("AMD", 1.07e9), "AMD takes $1.07 billion charge for OpenAI and Meta chip warrants"),
        (deal("Agility Robotics", 465e6, "acquisition"), "Agility Robotics shareholders approve $2.5B SPAC merger with Churchill Capital Corp XI"),
        (deal("Hugging Face", 12.93e9, "acquisition"), "US DOJ inquires about Nvidia's $12.93 billion Hugging Face deal over AI competition concerns"),
        (deal("ABB Robotics", 5.375e9, "acquisition"), "China blocks SoftBank's $5.375B ABB Robotics acquisition"),
        (deal("Character.ai", 2.7e9), "Character.AI files for Chapter 11 bankruptcy, Disney offers $20 million for assets"),
        (deal("Brookfield Asset Management", 25e9), "Brookfield raises $25B in Q3, details AI factory pipeline and $9B Korea AI deal"),
    ]
    for f, head in cases:
        row, why = check(f, head)
        assert row is None and why, (head, row)
    # A round whose headline also mentions revenue is still a round.
    row, why = check(deal("Fal", 1.3e9, "series_c", 16e9), "AI inference startup Fal raises $1.3B led by Bessemer at $16B valuation, sources say $800M annualized revenue")
    assert row and row["amount_usd"] == 1.3e9, why
    # A contract platform is not a contract: "DocuSign buys Suplari to add AI procurement tools to its contract platform".
    row, why = check(deal("Suplari", None, "acquisition"), "DocuSign buys Suplari to add AI procurement tools to its contract platform")
    assert row and row["company"] == "Suplari", why
    # The main clause decides: what follows "as" is background.
    row, why = check(deal("Palo Alto Networks", 4.5e9, "acquisition"), "Palo Alto Networks buys Applied Compute for $4.5B as earnings top estimates")
    assert row and row["company"] == "Applied Compute", why


def test_plans_filings_and_talks_are_not_done_deals():
    for f, head in [
        (deal("ElevenLabs", None, "series_d_plus", 33e9), "ElevenLabs in early talks for 2027 funding round at $33B valuation"),
        (deal("Anthropic PBC", 100e9, "ipo", 1.5e12), "Anthropic files IPO with $1.5T–$2T valuation amid market jitters"),
        (deal("Anthropic", 141e9, "ipo"), "Anthropic files S-1 with SEC for Nasdaq IPO"),
        (deal("Oracle", 10e9), "Oracle files to sell $10B in stock and $10B in bonds to fund AI buildout"),
        (deal("OpenAI", 11e9), "OpenAI hints at GPT-6.1 Astra release amid funding and model speculation"),
        (deal("Decagon", None, "acquisition"), "Decagon in acquisition talks with ServiceNow after hiring bank"),
        (deal("Anthropic", 5e9, "ipo", 1.4e12), "Altimeter Capital commits $5B to Anthropic's $1.4T IPO"),
    ]:
        row, why = check(f, head)
        assert row is None and why, (head, row)


def test_an_acquisition_is_listed_under_the_company_bought():
    row, why = check(deal("Thrive Holdings", 15e9, "acquisition", investors=["OpenAI"]), "Thrive Holdings buys ADT for $7.6B, backed by OpenAI")
    assert (row["company"], row["amount_usd"], row["round"]) == ("ADT", 7.6e9, "acquisition") and row["investors"][0] == "Thrive Holdings", row
    row, _ = check(deal("Omen AI", 16e6, "acquisition"), "Omen AI acquires Aquatic Labs for $16M to improve data center cooling sensors")
    assert (row["company"], row["amount_usd"]) == ("Aquatic Labs", 16e6), row
    row, _ = check(deal("Meta", 22e6, "acquisition"), "Meta acquires AI coding startup Conductor, team to join Meta Superintelligence Labs")
    assert (row["company"], row["amount_usd"]) == ("Conductor", None), row
    # Two companies for two prices: the row keeps the sum the headline states.
    row, _ = check(deal("CrowdStrike", 495e6, "acquisition"), "CrowdStrike buys Fiddler AI and Dreadnode for $400M and $95M in cash")
    assert (row["company"], row["amount_usd"]) == ("Fiddler AI and Dreadnode", 495e6), row
    # The company bought, with its earlier round in the row: the row is the purchase, price unstated.
    row, _ = check(deal("Opaque Systems", 24e6, "series_b", 300e6), "Anthropic acquires Opaque Systems, a confidential AI startup")
    assert (row["company"], row["amount_usd"], row["round"], row["valuation_usd"]) == ("Opaque Systems", None, "acquisition", None), row
    # Sites, units and stakes are not companies; the seller is not the deal's company.
    for f, head in [
        (deal("WhiteFiber", 60e6, "acquisition"), "WhiteFiber buys two North Carolina sites for $60 million"),
        (deal("Nauticus Robotics", 8e6, "acquisition"), "Nauticus Robotics to acquire Ocean Power's WAM-V USVs for $8M stock plus cash"),
        (deal("Faraday Future Intelligent Electric Inc.", 200e6, "acquisition"), "FF EAI Robotics acquires Faraday Future's robotics unit for $200M in all-stock deal"),
        (deal("Crusoe", 3.9e9, "series_d_plus"), "Google acquires Wyoming data center site from Crusoe after $17 billion investment"),
    ]:
        row, why = check(f, head)
        assert row is None and why, (head, row)
    # A stake bought in a named company stays that company's row.
    row, _ = check(deal("Humain", 2.6e9, "acquisition"), "Saudi Aramco acquires a 49% stake in AI startup Humain for $2.6 billion")
    assert row["company"] == "Humain", row
    # A raise to buy something is the raise, not a purchase by its investors.
    row, _ = check(deal("Shield AI", 2.5e9, "series_d_plus"), "Shield AI raises $2.5B to acquire radar firm Echodyne")
    assert (row["company"], row["amount_usd"]) == ("Shield AI", 2.5e9), row


def test_an_investor_is_not_the_company_that_raised():
    row, why = check(deal("DeepSeek", 7.4e9, valuation=59e9), "DeepSeek leads $400 million funding round for Chinese robot AI startup")
    assert row is None and why == "the company is the investor", why
    row, _ = check(deal("Nvidia", 250e6), "Nvidia invests $30B in OpenAI and guarantees $105B for its Ohio data center")
    assert (row["company"], row["amount_usd"], row["investors"][0]) == ("OpenAI", 30e9, "Nvidia"), row
    row, _ = check(deal("Bending Spoons", 900e6), "Berkshire Hathaway invests over $900 million in Bending Spoons")
    assert (row["company"], row["amount_usd"]) == ("Bending Spoons", 900e6), row
    # "led by" is not the company leading: a round led by an investor stays the company's.
    row, _ = check(deal("Remanence", 30e6, "seed"), "Remanence raises $30m seed led by EQT Ventures and FirstMark")
    assert (row["company"], row["amount_usd"]) == ("Remanence", 30e6), row


def test_the_headline_figure_wins_and_other_currencies_are_kept_as_stated():
    row, _ = check(deal("Sierra", 18e9, valuation=30e9), "Sierra finalizes $1.75B Series F at $30B valuation")
    assert (row["amount_usd"], row["round"]) == (1.75e9, "series_d_plus"), row
    row, _ = check(deal("Coframe", 35e6), "Coframe raises $25M Series A: AI makes websites personalized")
    assert (row["amount_usd"], row["round"]) == (25e6, "series_a"), row
    row, _ = check(deal("XEBO.ai", 11e6, "series_a"), "XEBO.ai raises $10.5M Series A from Emirates Growth Fund")
    assert row["amount_usd"] == 10.5e6, row
    row, _ = check(deal("Lute", None, "seed"), "Lute raises $20M across pre-seed and seed")
    assert row["amount_usd"] == 20e6, row
    # Two figures: the headline does not say which one is the row's, so the extraction's stays.
    row, _ = check(deal("TypeSafe AI", 870e6, "series_d_plus", 7.5e9), "TypeSafe raises $40M seed then $870M at $7.5B valuation for Jev decision model")
    assert row["amount_usd"] == 870e6, row
    row, _ = check(deal("CloudBackend"), "CloudBackend raises €6.6M to expand AI cloud infrastructure for creators")
    assert row["amount_stated"] == "€6.6M" and 7e6 < row["amount_usd"] < 8e6, row
    row, _ = check(deal("Chenghengwei", None, "series_a"), "Chenghengwei secures RMB 200M Series A to scale edge AI SoC production")
    assert row["amount_stated"] == "RMB 200M" and 2e7 < row["amount_usd"] < 4e7, row
    row, _ = check(deal("DeepSeek", 15e9, investors=["CATL"]), "DeepSeek raises about 100 billion yuan in funding round")
    assert row["amount_stated"] == "RMB 100B" and row["amount_usd"] == 15e9, row  # the extraction's conversion is close enough
    row, _ = check(deal("MountAIn", 8.4e6, "seed"), "MountAIn raises €7.3M seed to commercialize MountainCore")
    assert row["amount_stated"] == "€7.3M" and row["amount_usd"] == 8.4e6, row
    row, _ = check(deal("Amazon", 80e9), "Amazon secures $80B loan for AI and automation push")
    assert row["round"] == "debt", row


def test_names_are_written_as_the_headline_writes_them():
    for company, head, shown in [
        ("Zipher Technologies Ltd.", "Zipher raises $53M for AI agents managing enterprise data workloads", "Zipher"),
        ("Trustly Holding AB", "Trustly raises $60M to build AI intelligence on its payments network", "Trustly"),
        ("unsiloed ai", "Felicis leads $7.5M seed round for Unsiloed AI to unlock enterprise documents for AI agents", "Unsiloed AI"),
        ("e2e", "E2e raises $8 million seed to build verification layer for agentic coding", "e2e"),
        ("me&u", "OpenTable agrees to acquire me&u for voice AI and venue software", "me&u"),
        ("Amazon.com Inc.", "Amazon sets up $80 billion supplier loan facility to fund AI infrastructure", "Amazon"),
    ]:
        row, why = check(deal(company, None), head)
        assert row and row["company"] == shown, (company, why, row)
    assert funding.company_key("Amazon.com Inc.") == funding.company_key("Amazon")
    assert funding.company_key("Pierre Computer Company, Inc.") == funding.company_key("Pierre Computer")


def test_two_spellings_and_a_retold_deal_are_one_row():
    arts = [article(1, "Tutor Intelligence raises $75M", deal("Tutor Intelligence", 75e6, "series_b"), ["$75M Series B."], date="2026-11-02T08:00:00Z", lead=True),
            article(2, "Tutor raises $75M", deal("Tutor", 75e6, "series_b"), ["$75M."], date="2026-11-02T09:00:00Z")]
    rows, _ = funding.build([story("tutor", "Tutor Intelligence raises $75M Series B to expand robotics training", arts, first="2026-11-02T08:00:00Z")], NOV)
    assert [(r["company"], r["sources"]) for r in rows] == [("Tutor Intelligence", 2)], rows
    # The buyer's row and the target's row of one purchase, a day apart, are one deal.
    a = [article(3, "Palo Alto Networks buys Applied Compute", deal("Applied Compute", 4.5e9, "acquisition"), ["$4.5B."], date="2026-11-18T08:00:00Z", lead=True)]
    b = [article(4, "Palo Alto Networks agrees to buy Applied Compute", deal("Palo Alto Networks", 4.5e9, "acquisition"), ["$4.5B."], date="2026-11-19T08:00:00Z", lead=True)]
    rows, _ = funding.build([story("pan-1", "Palo Alto Networks buys Applied Compute for $4.5B as earnings top estimates", a, first="2026-11-18T08:00:00Z"),
                             story("pan-2", "Palo Alto Networks agrees to buy Applied Compute for $4.5B in stock", b, first="2026-11-19T08:00:00Z")], NOV)
    assert [(r["company"], r["amount_usd"], r["date"][:10]) for r in rows] == [("Applied Compute", 4.5e9, "2026-11-18")], rows
    # The same figure for the same company six weeks later is the deal retold, not a second one;
    # "Warp" and "Warp Sciences" with different figures are two companies.
    c = [article(5, "Nvidia invests $30B in OpenAI", deal("OpenAI", 30e9), ["$30B."], date="2026-10-13T08:00:00Z", lead=True)]
    d = [article(6, "Nvidia invests $30 billion in OpenAI", deal("OpenAI", 30e9), ["$30 billion."], date="2026-11-24T08:00:00Z", lead=True)]
    e = [article(7, "Warp raises $60M", deal("Warp", 60e6, "series_b"), ["$60M."], date="2026-10-22T08:00:00Z", lead=True)]
    g = [article(8, "Warp Sciences raises $215M", deal("Warp Sciences", 215e6, "series_a"), ["$215M."], date="2026-10-22T09:00:00Z", lead=True)]
    rows, _ = funding.build([story("o1", "Nvidia invests $30B in OpenAI and guarantees $105B for its Ohio data center", c, first="2026-10-13T08:00:00Z"),
                             story("o2", "Nvidia invests $30 billion in OpenAI, boosting GPU sales", d, first="2026-11-24T08:00:00Z"),
                             story("w1", "Warp raises $60M to automate HR workflows with AI agents", e, first="2026-10-22T08:00:00Z"),
                             story("w2", "Warp Sciences raises $215M Series A for AI science", g, first="2026-10-22T09:00:00Z")], NOV)
    assert sorted((r["company"], r["date"][:10]) for r in rows) == [("OpenAI", "2026-10-13"), ("Warp", "2026-10-22"), ("Warp Sciences", "2026-10-22")], rows


def test_a_deal_no_extraction_caught_is_read_from_the_headline():
    # 25-26 Nov: two deal stories had no row because no article's extraction produced one.
    bare = [{"id": 1, "title": "t", "headline": "t", "funding": None, "keyPoints": [], "summaryMd": "", "description": None,
             "publishedAt": "2026-11-25T11:00:00Z", "isLead": True}]
    stories = [
        story("cournot", "Cournot raises $5M to build verifiable AI reasoning for onchain applications", bare, first="2026-11-26T09:03:05Z"),
        story("blacksky", "BlackSky acquires EpochGeo to add AI tools for fusing defense data", bare, first="2026-11-25T11:20:02Z"),
        # Not deals: no figure, a price, a plan, a forecast.
        story("prices", "OpenAI raises prices for ChatGPT Pro subscribers", bare),
        story("concern", "Researchers raise concerns about AI agents in hospitals", bare),
        story("plan", "Acme plans to acquire Foo for $20M", bare),
        story("forecast", "Nvidia raises $5B revenue forecast as AI demand grows", bare),
    ]
    rows, dropped = funding.build(stories, NOV)
    got = sorted((r["company"], r["amount_usd"], r["round"], tuple(r["investors"])) for r in rows)
    assert got == [("Cournot", 5e6, "other", ()), ("EpochGeo", None, "acquisition", ("BlackSky",))], got
    assert dropped == {"(added) read from the headline alone": 2}, dropped


# ---- The kept ledger: deals stay after their stories leave the export window ----

import shutil  # noqa: E402
import tempfile  # noqa: E402
from contextlib import contextmanager  # noqa: E402

from digest import config  # noqa: E402


@contextmanager
def cache_dir():
    tmp = Path(tempfile.mkdtemp())
    saved = config.CACHE_DIR
    config.CACHE_DIR = tmp / "cache"
    try:
        yield tmp
    finally:
        config.CACHE_DIR = saved
        shutil.rmtree(tmp, ignore_errors=True)


def kept_row(company, amount, rnd, date, slug, headline, **extra):
    return {"company": company, "amount_usd": amount, "round": rnd, "investors": extra.pop("investors", []), "valuation_usd": None,
            "date": date, "storySlug": slug, "storyHeadline": headline, "sources": extra.pop("sources", 1), **extra}


def test_a_deal_outlives_the_window_and_a_rule_fixed_today_cleans_kept_rows():
    with cache_dir():
        mistral = kept_row("Mistral AI", 3.3e9, "series_c", "2026-09-15T08:00:00Z", "mistral", "Mistral AI raises €3B at $24B valuation", sources=5)
        first = funding.remember([mistral], {"mistral"}, [], NOV)
        assert len(first) == 1  # this run's rows are listed as built
        # A row kept under the earlier rules, which listed it (it carries no mark of today's rules).
        bogus = kept_row("Meta", 145e9, "other", "2026-09-20T08:00:00Z", "meta-sales", "Meta forms sales team amid $145 billion spend")
        funding.save(funding.read_file() + [bogus])
        # Weeks later both stories have left the window: Mistral stays, the row today's rules reject goes.
        acme = kept_row("Acme", 50e6, "series_a", "2026-11-20T08:00:00Z", "acme", "Acme raises $50M Series A")
        out = funding.remember([acme], {"acme"}, [], NOV)
        assert [(r["company"], r["date"][:10]) for r in out] == [("Acme", "2026-11-20"), ("Mistral AI", "2026-09-15")], out
        assert out[1]["sources"] == 5 and out[1]["storySlug"] == "mistral"
        # A run whose window holds none of them still lists both.
        assert [r["company"] for r in funding.remember([], set(), [], NOV)] == ["Acme", "Mistral AI"]


def test_a_kept_deal_and_a_later_report_of_it_are_one_row():
    with cache_dir():
        launch = kept_row("Acme", 50e6, "other", "2026-10-01T08:00:00Z", "acme-1", "Acme raises $50M")
        funding.remember([launch], {"acme-1"}, [], NOV)
        # The first story left; a story within the month reports the same round with its series and investors.
        later = kept_row("Acme", 50e6, "series_a", "2026-10-20T08:00:00Z", "acme-2", "Acme raises $50M Series A led by Foo", investors=["Foo"], sources=3)
        out = funding.remember([later], {"acme-2"}, [], NOV)
        assert len(out) == 1, out
        row = out[0]
        assert (row["date"][:10], row["storySlug"], row["round"], row["investors"], row["sources"]) == ("2026-10-01", "acme-1", "series_a", ["Foo"], 3), row


def test_unpublished_stories_leave_the_ledger_and_a_lost_cache_comes_back_from_the_archive():
    import gzip
    from digest import archive
    with cache_dir():
        row = kept_row("Acme", 50e6, "series_a", "2026-10-01T08:00:00Z", "acme", "Acme raises $50M Series A")
        funding.remember([row], {"acme"}, [], NOV)
        assert funding.remember([], set(), ["acme"], NOV) == []
        funding.remember([row], {"acme"}, [], NOV)
        rows = funding.read_file()
        config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with gzip.open(archive.path(), "wt", encoding="utf-8") as f:
            json.dump({"format": archive.FORMAT, "windowStart": "2026-10-01T00:00:00+00:00", "stories": {}, funding.ARCHIVE_KEY: rows}, f)
        funding.path().write_text("{not json", encoding="utf-8")
        assert funding.read_file() is None
        assert [r["company"] for r in funding.load()] == ["Acme"]
        funding.path().unlink()
        assert [r["company"] for r in funding.remember([], set(), [], NOV)] == ["Acme"]


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
