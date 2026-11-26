"""Rules for the Models tracker: which extracted model mentions are real releases, and when two
rows are the same model.

The summary model extracts a "model_release" from any article that talks about a model, so the raw
rows include comparisons ("GPT-5.6 Luna vs GPT-6 Astra"), passing mentions (a security story that
names GPT-6), research methods from papers, and the same launch spelled five ways
("DeepSeek-V4.1-Flash", "V4.1 Flash", "DeepSeek V4.1 Flash"...). The page should list models people
can use or download, once each.

The audit of 26 Nov 2026 (the 150 launches /api/models.json listed) found 24 wrong rows: one model
under two makers ("MAI Code 2" from MAI and from Microsoft, "Inkling 2 Small" from Inkling and from
Thinking Machines), one model under two or three names ("Mistral Large 4 (Le Chonk)", "Le Chonk",
"Nemotron 3.5 Super VL 120B-A12B"), quantised copies of other labs' models, apps and agents ("Canva
Brush AI tool", "Muse AI agent in Mexico"), a model's arrival on a cloud marketplace dated as its
launch, and a story that only used a model ("healthcare agents with GPT-6 Astra"). The rules below
and fold_aliases are the fix; tests/test_trackers.py holds those rows.
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
ORG_WORDS = re.compile(r"\b(?:ai|inc|labs?|platforms|technologies|corp(?:oration)?|ltd|limited|llc|research|machine learning|"
                       r"s\.?a\.?s?|gmbh|ag|ab|plc|pbc|bv|nv|pte|pty|srl|holdings?|holding|group|co|company)\b\.?", re.I)
# A copy of someone's model in another format is not a new model: "Nari Labs releases Qwen3.8-27B
# quantized models", "The Kaitchup releases ... GGUF models".
QUANTIZED = re.compile(r"\b(?:quantiz\w*|quantis\w*|gguf|ggml|awq|gptq|exl2|[2-8](?:\.5)?-bit|quants?)\b", re.I)
# The model reached a cloud marketplace: news, but not its launch, and not its launch date.
ON_A_CLOUD = re.compile(
    r"\b(?:available|arrives?|lands?|comes?|now)\b[^.;]{0,20}?\b(?:on|in|via|through|to)\s+(?:the\s+)?"
    r"(?:amazon|aws|bedrock|sagemaker|azure|microsoft foundry|google cloud|vertex|snowflake|databricks|oracle|watsonx|"
    r"nvidia nim|openrouter|github models|cloudflare workers)\b", re.I)
# A launch in one more country: "Meta launches Muse AI agent in Mexico", "Alexa+ early access in Japan".
REGIONAL = re.compile(
    r"\b(?:early access|expands?|arrives?|comes to|rolls? out|launch(?:es|ed)?|available)\b[^.;]{0,60}?\bin\s+(?:the\s+)?"
    r"(?:japan|mexico|india|china|korea|south korea|brazil|europe|eu|uk|canada|australia|latin america|germany|france|"
    r"spain|italy|indonesia|africa|the middle east|saudi arabia|uae|singapore|taiwan)\b", re.I)
# The thing launched is a product built on a model, named right after it: "Brush AI tool", "Muse AI
# agent", "Minos, an AI playground". An agent or assistant with its own API or weights is still a model.
PRODUCT_AFTER = re.compile(r"^[\s,]*(?:(?:an?|the|its|new)\s+)?(?:[\w-]+\s+){0,2}?(?P<word>tool|app|playground|platform|feature|"
                           r"service|studio|plugin|extension|browser|agent|assistant|chatbot|device)s?\b", re.I)
ALWAYS_A_PRODUCT = {"tool", "app", "playground", "platform", "feature", "service", "studio", "plugin", "extension", "browser", "device"}
# Words right before a model's name that make the story about something that uses it.
USES = {"with", "using", "via", "on", "than", "for", "to", "like", "from", "into", "over", "beats", "rivals"}
# Name parts that say which build, not which model.
SIZE = re.compile(r"^(?:\d+(?:\.\d+)?[bmkt]|[ae]\d+(?:\.\d+)?b|\d+x\d+b|20\d{6}|\d{4}|[bqw]\d+\w*|fp\d+|int\d+)$", re.I)
BUILD_WORDS = {"instruct", "base", "chat", "it", "hf", "preview", "beta", "revision", "rev", "fp8", "bf16", "int4", "int8"}


def _plain(text: str) -> str:
    """Lowercase ASCII-ish text with every dash, hyphen variant and odd space turned into a space."""
    text = unicodedata.normalize("NFKC", text or "").lower()
    return re.sub(r"[^a-z0-9.]+", " ", text).strip()


def org_key(lab: str | None) -> str:
    s = _plain(lab or "").replace("google deepmind", "google").replace("deepmind", "google")
    s = re.sub(r"\.com\b", " ", s)  # "Amazon.com Inc." is Amazon
    return re.sub(r"[^a-z0-9]", "", ORG_WORDS.sub(" ", s))


def _bare(name: str) -> str:
    """The name without its parenthetical ("Mistral Large 4 (Le Chonk)") or file suffix."""
    return re.sub(r"\s*\([^)]*\)", " ", FILE_SUFFIX.sub("", name or "")).strip()


def _tokens(name: str, lab: str | None) -> list[str]:
    """The distinctive words of a model name: without the lab's own name, file suffixes or filler."""
    org = org_key(lab)
    words = [w.strip(".") for w in _plain(_bare(name)).split()]
    return [w for w in words if w and w != org and (len(w) >= 3 or any(c.isdigit() for c in w))]


def model_key(name: str, lab: str | None) -> str:
    """Same model, same key: "DeepSeek-V4.1-Flash" (DeepSeek AI) and "V4.1 Flash" (Deepseek)."""
    return "".join(_tokens(name, lab)).replace(".", "") + "|" + org_key(lab)


def _build_part(token: str) -> bool:
    # A version number ("4" in "Mistral Large 4") is part of the name; a date or a revision ("1017") is not.
    return bool(SIZE.match(token)) or token in BUILD_WORDS


def full_name(name: str, headline: str) -> str:
    """The name as the headline writes it when the extraction dropped its family prefix: "6.2 Astra"
    in "OpenAI releases GPT-6.2 Astra" is GPT-6.2 Astra."""
    name = (name or "").strip()
    if not name or not headline:
        return name
    m = re.search(rf"(?<![\w-])([A-Za-z][\w.]*-){re.escape(name)}(?![\w.])", headline, re.I)
    return m.group(0) if m else name


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
    if QUANTIZED.search(name) or QUANTIZED.search(headline or ""):
        return False
    story_head = (headline or "").split(" | ")[0]  # the export passes "<story headline> | <article title>"
    if ON_A_CLOUD.search(story_head) or REGIONAL.search(story_head):
        return False
    # The story must be about this model: its first distinctive word and most of the rest appear in the
    # headline ("Claude Fable 5" extracted from a story about misuse of Claude is not a launch).
    tokens = _tokens(name, lab)
    if not tokens:
        return False
    text = _plain(headline).replace(" ", "")
    found = [t for t in tokens if t in text]
    if tokens[0] not in found or len(found) < 0.6 * len(tokens):
        return False
    # A word of the name may be missing ("Marengo 3.0" for Marengo Embed 3.0), but not where the
    # headline names another model: "OpenAI rolls out GPT-6.2 Sol" does not launch GPT-6.2 Astra.
    # The story headline alone decides, unless the article's own title names the whole model (a story
    # first about Gemini 4 Carbon can carry Gemini 4 Neon's headline later).
    def names_all(part: str) -> bool:
        t = _plain(part).replace(" ", "")
        return all(tok in t or _build_part(tok) for tok in tokens)
    parts = (headline or "").split(" | ")
    if not names_all(parts[0]) and _other_variant(tokens, parts[0]) and not any(names_all(p) for p in parts[1:]):
        return False
    return not _used_or_a_product(tokens, availability, story_head)


def _other_variant(tokens: list[str], headline: str) -> bool:
    """True when the headline's words that match the name are followed by a capitalised word that is
    not part of it ("GPT-6.2 Sol" against GPT-6.2 Astra)."""
    raw = [w for w in re.split(r"[\s\-–—:;,|]+", headline or "") if w]
    for i, w in enumerate(raw):
        if _plain(w).strip(".") != tokens[0]:
            continue
        j = i + 1
        while j < len(raw) and _plain(raw[j]).strip(".") in tokens:
            j += 1
        nxt = raw[j] if j < len(raw) else ""
        if nxt[:1].isupper() and _plain(nxt).strip(".") not in tokens and nxt.lower() not in {"ai", "model", "models", "api"}:
            return True
    return False


def _used_or_a_product(tokens: list[str], availability: str, headline: str) -> bool:
    """True when the headline launches something else and names the model as what it uses ("launches
    healthcare agents with GPT-6 Astra"), or calls the named thing a tool, an app or an agent."""
    for part in [headline or ""]:
        words = _plain(part).split()
        at = next((i for i, w in enumerate(words) if w.strip(".") in tokens or tokens[0] in w), None)
        if at is None:
            continue
        verb = next((i for i, w in enumerate(words) if LAUNCH_WORDS.match(w) or w in ("open", "opens")), None)
        after_verb = verb is not None and at <= verb + 2
        if not after_verb and at > 0 and words[at - 1] in USES:
            return True
        # The words after the name, in the headline's own spelling.
        span = r"[\s_.\-]*".join(re.escape(t).replace(r"\.", r"[.\-]?") for t in tokens)
        m = re.search(span, part, re.I)
        if m:
            pm = PRODUCT_AFTER.match(part[m.end():])
            if pm and (pm.group("word").lower() in ALWAYS_A_PRODUCT or availability not in ("api", "open_weights")):
                return True
    return False


def fold_versions(rows: dict[str, dict]) -> dict[str, dict]:
    """Fold "WeatherNext" into "WeatherNext 3" from the same lab: a key that is another key minus a
    trailing version number is the same launch named without its version."""
    keys = sorted(rows, key=len)
    out = dict(rows)
    for short in keys:
        name, org = short.split("|", 1)
        longer = [k for k in keys if k != short and k.endswith("|" + org) and k.startswith(name)
                  and re.fullmatch(r"(?!\d{4,}$)v?\d[\d.]*", k.split("|", 1)[0][len(name):] or "x")]  # a date is not a version
        if name and longer and short in out:
            _join(out[longer[0]], out[short])
            del out[short]
    return out


def _join(target: dict, other: dict) -> None:
    """`other` is the same model as `target`: the earlier report is the launch, specs fill in, and
    the source count is the larger one (summing would count a story again every run: the ledger keeps
    the merged row and the window brings both rows back)."""
    if (other.get("date") or "~") < (target.get("date") or "~"):
        for f in ("date", "storySlug", "storyHeadline"):
            if other.get(f):
                target[f] = other[f]
    for f in ("license", "context", "link"):
        if not target.get(f) and other.get(f):
            target[f] = other[f]
    target["sources"] = max(int(target.get("sources") or 0), int(other.get("sources") or 0))


def _core(name: str, lab: str | None) -> tuple[str, str]:
    """(the name without build parts, the build parts): "Nemotron 3.5 Super VL 120B-A12B" is
    ("nemotron35supervl", "120ba12b"); "DeepSeek-V4.1-Flash-20261017" is ("v41flash", "20261017")."""
    toks = _tokens(name, lab)
    while toks and len(toks) > 1 and _build_part(toks[-1]):
        toks = toks[:-1]
    core = "".join(toks).replace(".", "")
    build = "".join(_tokens(name, lab)[len(toks):]).replace(".", "")
    return core, build


def _full(name: str) -> str:
    """The whole name, lab words and all, for matching one model across two makers."""
    return "".join(_plain(_bare(name)).split()).replace(".", "")


def _maker_named_after(name: str, lab: str | None) -> bool:
    """"Inkling" for Inkling 2 Small, "MAI" for MAI Code 2: the maker is the product's own first word."""
    first = (_plain(_bare(name)).split() or [""])[0]
    return bool(first) and org_key(lab) == first.replace(".", "")


def fold_aliases(rows: dict[str, dict]) -> dict[str, dict]:
    """One row per model across the spellings fold_versions leaves apart:
    - one maker, one name with and without its build detail ("Nemotron 3.5 Transcribe" and
      "NVIDIA-Nemotron-3.5-Transcribe-30B-A4B"; "120B" and "120B-A12B" of one model);
    - one maker, a name and its alias in brackets ("Mistral Large 4 (Le Chonk)" and "Le Chonk");
    - one maker, a name and the same name with its family prefix ("6.2 Astra", "GPT-6.2 Astra");
    - two makers, the very same name with a version in it ("Arete-27B" from Nous Research and from
      NousResearch; "MAI Code 2" from MAI and from Microsoft), kept under the maker that is not the
      product's own first word.
    The earliest report stays the row's launch."""
    keys = sorted(rows, key=lambda k: (rows[k].get("date") or "~", k))
    parent = {k: k for k in keys}

    def find(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            first, second = sorted((ra, rb), key=lambda k: (rows[k].get("date") or "~", k))
            parent[second] = first

    info = {}
    for k in keys:
        r = rows[k]
        org = k.split("|", 1)[1] if "|" in k else org_key(r.get("lab"))
        core, build = _core(r.get("name") or "", r.get("lab"))
        aliases = {model_key(a, r.get("lab")).split("|", 1)[0] for a in re.findall(r"\(([^)]+)\)", r.get("name") or "")}
        info[k] = (org, core, build, {a for a in aliases if len(a) >= 4}, _full(r.get("name") or ""))
    by_org: dict[str, list[str]] = {}
    for k in keys:
        by_org.setdefault(info[k][0], []).append(k)
    for org, ks in by_org.items():
        for i, a in enumerate(ks):
            _, ca, ba, aa, _ = info[a]
            for b in ks[i + 1:]:
                _, cb, bb, ab, _ = info[b]
                if not ca or not cb:
                    continue
                same_core = ca == cb and (not ba or not bb or ba.startswith(bb) or bb.startswith(ba))
                alias = ca in ab or cb in aa or (aa & ab)
                if same_core or alias:
                    union(a, b)
        # "6.2 Astra" is the tail of exactly one longer name with a version in it.
        for a in ks:
            ca = info[a][1]
            if not re.search(r"\d", ca) or len(ca) < 3:
                continue
            longer = [b for b in ks if b != a and info[b][1] != ca and info[b][1].endswith(ca)]
            if len(longer) == 1:
                union(a, longer[0])
    by_full: dict[str, list[str]] = {}
    for k in keys:
        full = info[k][4]
        if re.search(r"\d", full) and len(full) >= 4:
            by_full.setdefault(full, []).append(k)
    for ks in by_full.values():
        for b in ks[1:]:
            union(ks[0], b)
    groups: dict[str, list[str]] = {}
    for k in keys:
        groups.setdefault(find(k), []).append(k)
    out = {}
    for root, members in groups.items():
        if len(members) == 1:
            out[root] = rows[root]
            continue
        # The row to keep: a maker that is not the product's own name, then the earliest report.
        home = next((m for m in members if not _maker_named_after(rows[m].get("name") or "", rows[m].get("lab"))), members[0])
        row = dict(rows[home])
        # The fullest name without brackets: "Nemotron 3.5 Super VL" over "...120B-A12B", "GPT-6.2 Astra" over "6.2 Astra".
        names = [(_bare(rows[m].get("name") or ""), rows[m].get("lab")) for m in members]
        # A version in the name's core first ("Mistral Large 4" over "Le Chonk"), then the longest core
        # ("GPT-6.2 Astra" over "6.2 Astra"), then the shortest ("Phonon" over "Phonon 31m").
        best = max(names, key=lambda n: (bool(re.search(r"\d", _core(*n)[0])), len(_core(*n)[0]), -len(n[0])))
        if best[0]:
            row["name"] = best[0]
        for m in members:
            if m != home:
                _join(row, rows[m])
        out[model_key(row.get("name") or "", row.get("lab"))] = row
    return out


def fold_all(rows: dict[str, dict]) -> dict[str, dict]:
    """Every folding rule, in order: versions, then spellings."""
    return fold_aliases(fold_versions(rows))
