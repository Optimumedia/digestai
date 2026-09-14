"""Small text helpers shared across steps: normalisation, slugs, simhash, keyword overlap."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

STOPWORDS = set(
    """a an the and or but if then of to in on at by for with from as is are was were be been being
    this that these those it its into over under about after before between during without within
    how why what when where which who whom whose will would can could should may might must shall
    has have had do does did not no nor so than too very just also more most such own same other
    new says said say here there their they them our your you we he she his her i me my us up out
    than via amid among around""".split()
)

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
    "fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "igshid", "ref", "ref_src",
    "source", "cmpid", "ncid", "sr_share", "guccounter", "guce_referrer", "guce_referrer_sig",
    "_hsenc", "_hsmi", "smid", "smtyp", "ocid", "trk", "s", "si", "feature",
}

SOURCE_SUFFIX = re.compile(r"\s+[-|–—:]\s+[A-Z][\w.&' ]{1,40}$")


# Aggregator click-tracking links that carry the real article URL in a query parameter.
# Bing News RSS items look like http://bing.com/news/apiclick.aspx?...&tid=<random>&url=<encoded>&c=<n>;
# tid and c change on every fetch, so without unwrapping every repeat looked like a new article.
REDIRECT_LINKS = {"bing.com": ("/news/apiclick.aspx", "url")}

# Never fetched: social networks and aggregators (no article text), msn.com (script-only pages
# with no extractable text), and a redirect host whose link could not be unwrapped.
SKIP_DOMAINS = {"news.google.com", "google.com", "youtube.com", "youtu.be", "x.com", "twitter.com",
                "facebook.com", "instagram.com", "tiktok.com", "linkedin.com", "bing.com"}
SKIP_DOMAIN_SUFFIXES = {"msn.com"}  # the domain and every subdomain


def unwrap_redirect(url: str) -> str:
    parts = urlsplit((url or "").strip())
    host = parts.netloc.lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    rule = REDIRECT_LINKS.get(host)
    if rule and parts.path.lower() == rule[0]:
        target = (parse_qs(parts.query).get(rule[1]) or [""])[0].strip()
        if target.lower().startswith(("http://", "https://")):
            return target
    return url


def is_skipped_domain(dom: str) -> bool:
    dom = (dom or "").lower()
    return dom in SKIP_DOMAINS or any(dom == s or dom.endswith("." + s) for s in SKIP_DOMAIN_SUFFIXES)


def normalize_url(url: str) -> str:
    url = unwrap_redirect(url.strip())
    parts = urlsplit(url)
    scheme = parts.scheme.lower() or "https"
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    if netloc.startswith("m.") and netloc.count(".") >= 2:
        netloc = netloc[2:]
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False) if k.lower() not in TRACKING_PARAMS]
    path = re.sub(r"/+$", "", parts.path) or "/"
    if path.endswith("/amp"):
        path = path[:-4] or "/"
    return urlunsplit((scheme, netloc, path, urlencode(query), ""))


def domain_of(url: str) -> str:
    netloc = urlsplit(url).netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


# Trailing tags such as "(2019)", "[pdf]", "[video] (2021)" that community sites append to titles.
TRAILING_TAGS = re.compile(r"(?:\s*[\(\[][^()\[\]]{1,12}[\)\]])+\s*$")
YEAR_TAG = re.compile(r"[\(\[]\s*((?:19|20)\d{2})\s*[\)\]]")


def title_year(title: str) -> int | None:
    """The year in a trailing "(2019)" / "[2021]" tag: Hacker News marks old pieces this way."""
    m = TRAILING_TAGS.search(title or "")
    years = [int(y) for y in YEAR_TAG.findall(m.group(0))] if m else []
    return min(years) if years else None


def strip_title_year(title: str) -> str:
    m = TRAILING_TAGS.search(title or "")
    if not m or not YEAR_TAG.search(m.group(0)):
        return title
    tail = re.sub(r"\s+", " ", YEAR_TAG.sub("", m.group(0))).strip()
    head = title[: m.start()].rstrip()
    return f"{head} {tail}" if tail else head


def clean_title(title: str) -> str:
    title = unicodedata.normalize("NFKC", title or "").strip()
    title = re.sub(r"\s+", " ", title)
    title = strip_title_year(title)  # never shown in a headline; fetch and the gate read raw_title
    title = re.sub(r"\s*(\.\.\.|…)$", "", title)
    stripped = SOURCE_SUFFIX.sub("", title)
    # Only strip a " - Source" suffix when what remains is still a full title.
    if len(stripped) >= 25 and len(stripped) < len(title):
        title = stripped
    return title.strip()


def slugify(value: str, max_len: int = 80) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    value = re.sub(r"-{2,}", "-", value)
    if len(value) > max_len:
        value = value[:max_len].rsplit("-", 1)[0]
    return value or "story"


def short_hash(value: str, n: int = 6) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:n]


def tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9][a-z0-9'+.-]*", (text or "").lower())]


def word_count(text: str) -> int:
    return len(re.findall(r"\S+", text or ""))


def keywords(text: str, n: int = 5) -> list[str]:
    """Most distinctive words of a title: long, non-stopword, in order of length."""
    seen: list[str] = []
    for tok in tokens(text.replace("’", "'")):
        tok = tok.strip(".'")
        if tok.endswith("'s"):
            tok = tok[:-2]
        if len(tok) >= 4 and tok not in STOPWORDS and tok not in seen:
            seen.append(tok)
    seen.sort(key=len, reverse=True)
    return seen[:n]


def simhash(text: str) -> int:
    """64-bit simhash over word shingles of a normalised title."""
    toks = [t for t in tokens(text) if t not in STOPWORDS]
    shingles = [" ".join(toks[i : i + 2]) for i in range(max(1, len(toks) - 1))] or toks
    v = [0] * 64
    for sh in shingles:
        h = int(hashlib.md5(sh.encode("utf-8")).hexdigest(), 16) & ((1 << 64) - 1)
        for i in range(64):
            v[i] += 1 if (h >> i) & 1 else -1
    out = 0
    for i in range(64):
        if v[i] > 0:
            out |= 1 << i
    return out


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def first_sentences(text: str, n: int = 3) -> list[str]:
    sents = re.split(r"(?<=[.!?])\s+", (text or "").strip())
    return [s.strip() for s in sents if len(s.strip()) > 20][:n]
