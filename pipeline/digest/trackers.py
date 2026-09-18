"""Rules for the Models tracker: which extracted model mentions are real releases, and when two
rows are the same model.

The summary model extracts a "model_release" from any article that talks about a model, so the raw
rows include comparisons ("GPT-5.6 Luna vs GPT-6 Astra"), passing mentions (a security story that
names GPT-6), research methods from papers, and the same launch spelled five ways
("DeepSeek-V4.1-Flash", "V4.1 Flash", "DeepSeek V4.1 Flash"...). The page should list models people
can use or download, once each.
"""
from __future__ import annotations

import re
import unicodedata

USABLE = {"api", "open_weights", "consumer"}
UNKNOWN = {"", "unknown", "none", "n/a", "null"}
# Not model makers: paper archives, hosting sites, and "the authors".
NOT_LABS = re.compile(r"^(?:arxiv|hugging ?face|github|papers with code|unknown lab)$|\bet al\b|\bresearch team\b|\bresearchers\b", re.I)
LAUNCH_WORDS = re.compile(
    r"\b(?:launch(?:es|ed)?|releas(?:e|es|ed)|unveil(?:s|ed)?|introduc(?:e|es|ed)|debut(?:s|ed)?|announc(?:e|es|ed)|"
    r"roll(?:s|ed)? out|ship(?:s|ped)?|open-sourc(?:e|es|ed)|drop(?:s|ped)?|adds|now available|available)\b", re.I)
COMPARISON = re.compile(r"\bvs\.?\b|\bversus\b", re.I)
FILE_SUFFIX = re.compile(r"(?:[-_. ]?gguf\b.*|\.(?:safetensors|bin|onnx))$", re.I)
ORG_WORDS = re.compile(r"\b(?:ai|inc|labs?|platforms|technologies|corp(?:oration)?|ltd|llc|research|machine learning|"
                       r"s\.?a\.?s?|gmbh|ag|plc|bv|nv|pte|pty|srl|holdings?)\b\.?", re.I)


def _plain(text: str) -> str:
    """Lowercase ASCII-ish text with every dash, hyphen variant and odd space turned into a space."""
    text = unicodedata.normalize("NFKC", text or "").lower()
    return re.sub(r"[^a-z0-9.]+", " ", text).strip()


def org_key(lab: str | None) -> str:
    s = _plain(lab or "").replace("google deepmind", "google").replace("deepmind", "google")
    return re.sub(r"[^a-z0-9]", "", ORG_WORDS.sub(" ", s))


def _tokens(name: str, lab: str | None) -> list[str]:
    """The distinctive words of a model name: without the lab's own name, file suffixes or filler."""
    org = org_key(lab)
    words = [w.strip(".") for w in _plain(FILE_SUFFIX.sub("", name or "")).split()]
    return [w for w in words if w and w != org and (len(w) >= 3 or any(c.isdigit() for c in w))]


def model_key(name: str, lab: str | None) -> str:
    """Same model, same key: "DeepSeek-V4.1-Flash" (DeepSeek AI) and "V4.1 Flash" (Deepseek)."""
    return "".join(_tokens(name, lab)).replace(".", "") + "|" + org_key(lab)


def is_release(release: dict, headline: str) -> bool:
    """True when this looks like a model people can use or download, launched in this story."""
    name, lab = release.get("name") or "", release.get("lab") or ""
    if not name.strip() or lab.strip().lower() in UNKNOWN or NOT_LABS.search(lab.strip()):
        return False
    if "," in name or COMPARISON.search(name) or COMPARISON.search(headline or ""):
        return False  # "GPT-5.6 Luna, GPT-6 Astra": a comparison, not a launch
    availability = str(release.get("availability") or "").strip().lower()
    if availability == "research":
        return False
    if availability not in USABLE and not LAUNCH_WORDS.search(headline or ""):
        return False
    # The story must be about this model: its first distinctive word and most of the rest appear in
    # the headline ("Claude Fable 5" extracted from a story about misuse of Claude is not a launch).
    tokens = _tokens(name, lab)
    if not tokens:
        return False
    text = _plain(headline).replace(" ", "")
    found = [t for t in tokens if t in text]
    return tokens[0] in found and len(found) >= 0.6 * len(tokens)


def fold_versions(rows: dict[str, dict]) -> dict[str, dict]:
    """Fold "WeatherNext" into "WeatherNext 3" from the same lab: a key that is another key minus a
    trailing version number is the same launch named without its version."""
    keys = sorted(rows, key=len)
    out = dict(rows)
    for short in keys:
        name, org = short.split("|", 1)
        longer = [k for k in keys if k != short and k.endswith("|" + org) and k.startswith(name)
                  and re.fullmatch(r"v?\d[\d.]*", k.split("|", 1)[0][len(name):] or "x")]
        if name and longer and short in out:
            target = out[longer[0]]
            target["sources"] = target.get("sources", 0) + out[short].get("sources", 0)
            if (out[short].get("date") or "") < (target.get("date") or "~"):
                target["date"] = out[short]["date"]
            del out[short]
    return out
