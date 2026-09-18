"""Funding tracker rules: python tests/test_funding.py (offline). The examples are the rows the live
/funding page showed on 16 September 2026: one Mistral round as 74 rows, a $1B fund as $10B, a robot
launch as an acquisition, and a 2025 acquisition as news."""
from __future__ import annotations

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
    # A funding verb in the text carries a neutral headline.
    assert funding.is_deal(deal("Acme", 50e6, "series_a"), "Acme's next act", "Acme raised $50M in a Series A.", NOW) is None


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
    why = funding.is_deal(deal("Humane", 116e6, "acquisition"), "What happened to Humane after HP bought it",
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
    assert (row["company"], row["amount_usd"], row["round"], row["valuation_usd"]) == ("Mistral AI", 3.3e9, "series_c", 24e9), row
    assert row["investors"] == ["ASML", "a16z", "Nvidia"] and row["date"] == "2026-09-15T08:00:00Z"
    assert row["sources"] == 5, row["sources"]  # 1, 2, 3, 8 and 9; 4 and 5 are outvoted, 6 and 7 dropped by the rules
    assert dropped == {"called an acquisition, but the headline does not say so": 1, "amount is the valuation": 1}, dropped
    assert funding.total(rows) == 3.3e9


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
    assert [r["company"] for r in rows] == ["Groq", "Acme"], rows
    assert funding.total(rows) == 20e9
    assert sum(dropped.values()) == 2


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
