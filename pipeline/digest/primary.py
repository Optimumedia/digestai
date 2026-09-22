"""Which primary sources are a company speaking, and which are a paper.

config.PRIMARY_DOMAINS and the sources.yaml type "primary" mark everything a story can be *about*:
a lab's post, but also an arXiv preprint, a Nature paper, a GitHub repository or a government PDF.
That is right for the coverage bar, but not for the briefing's confirmed-first rule: a preprint
nobody else has written about is one author's word, and on 21 Sep one (DischargeBench, arXiv cs.CL)
led the briefing because it counted as "the primary source". The rule, the morning note and the
share shortlist now ask the narrower question here: is this the company's or lab's own announcement?

The same test picks the company's own share image (images.primary_images), so both use one list.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from . import config

# The makers' own sites. The primary domains that are not a company (arXiv, GitHub, governments) are
# left out, and a few company sites the feeds do not tag as primary yet are added, so they count
# the day they are. An article qualifies only when the export also marked it primary.
NOT_COMPANY = frozenset({"arxiv.org", "github.com", "qwenlm.github.io", "europa.eu", "whitehouse.gov",
                         "gov.uk", "nist.gov", "ftc.gov", "sec.gov"})
COMPANY_DOMAINS = frozenset((config.PRIMARY_DOMAINS - NOT_COMPANY) | {
    "google", "amazon.com", "aboutamazon.com", "canva.com", "hubspot.com", "shopify.com", "adobe.com",
    "salesforce.com", "ibm.com", "intel.com", "amd.com", "samsung.com", "qualcomm.com", "oracle.com",
})
# Hosts on a company domain whose pages are not the company speaking: model cards, Spaces and
# datasets on Hugging Face are uploaded by anyone, so only its blog counts.
COMPANY_PATHS = {"huggingface.co": "/blog/"}

# Papers and research write-ups: preprint servers, journals and university news offices. A story
# whose only primary source is one of these is a paper, however it arrived.
PAPER_DOMAINS = frozenset({
    "arxiv.org", "nature.com", "science.org", "cell.com", "biorxiv.org", "medrxiv.org", "openreview.net",
    "aclanthology.org", "proceedings.neurips.cc", "papers.nips.cc", "proceedings.mlr.press", "ssrn.com",
    "pnas.org", "ieee.org", "acm.org", "springer.com", "sciencedirect.com", "news.mit.edu", "mit.edu",
    "stanford.edu", "berkeley.edu", "cmu.edu", "ox.ac.uk", "cam.ac.uk",
})
# The research feeds in sources.yaml ("Universities and research"): what they publish is a paper or
# a university's account of one, even when sources.yaml types them "primary" for the coverage bar.
PAPER_SOURCES = frozenset({"arxiv-cs-ai", "arxiv-cs-cl", "nature-ml", "mit-news-ai", "bair"})


def on_domain(host: str, domains) -> str | None:
    host = (host or "").lower().removeprefix("www.")
    for d in domains:
        if host == d or host.endswith("." + d):
            return d
    return None


def _host(article: dict) -> str:
    return (article.get("domain") or urlsplit(article.get("url") or "").netloc or "").lower()


def company_announcement(article: dict) -> bool:
    """True when the article is the company's own post: marked primary by the export and on one of
    the makers' own domains (not a preprint, a repository, a government or a news outlet)."""
    if article.get("sourceType") != "primary":
        return False
    d = on_domain(_host(article), COMPANY_DOMAINS)
    if not d:
        return False
    need = COMPANY_PATHS.get(d)
    return not need or need in urlsplit(article.get("url") or "").path


def is_paper(article: dict) -> bool:
    """A paper or a research write-up: on a journal, preprint or university site, from one of the
    research feeds, a PDF, or an article the model classed as research."""
    if on_domain(_host(article), PAPER_DOMAINS) or article.get("sourceKey") in PAPER_SOURCES:
        return True
    if urlsplit(article.get("url") or "").path.lower().endswith(".pdf"):
        return True
    return article.get("contentType") == "research"


def announcement(article: dict) -> bool:
    """The lab's or company's own announcement, the only primary source that confirms a story on
    its own: a company post that is not a research paper (openai.com/index/<paper> is still a paper)."""
    return company_announcement(article) and not is_paper(article)


def announced(story: dict) -> bool:
    """The story carries the company's own announcement. Uses the export's flag when it is there
    (stories.json from this version on), else works it out from the articles."""
    if "hasAnnouncement" in story:
        return bool(story["hasAnnouncement"])
    return any(announcement(a) for a in story.get("articles") or [])


def paper_only(story: dict) -> bool:
    """The story's primary sources are all papers: it has one, and no company announcement."""
    primaries = [a for a in story.get("articles") or [] if a.get("sourceType") == "primary"]
    return bool(primaries) and not announced(story) and all(is_paper(a) for a in primaries)


def research_story(story: dict) -> bool:
    """In the research category, or resting on a paper alone: may be in the briefing, never lead it."""
    return story.get("category") == "research" or paper_only(story)
