"""The cheap check a summary passes before it is stored.

Every figure and every named party a summary states has to be in the article it summarises. The
check is rules only - no model call, no cost, no rate limit - and it runs on the model's answer
while the article text is still in hand, which is the only moment the source is free to compare
against. quality.py runs the same idea over the published site, on headlines only; this is the
per-article version at write time, so a wrong number never reaches a page in the first place.

What it does on a mismatch, in order:
1. ask the same provider once more, naming what was wrong (`stricter_prompt`);
2. if that answer is wrong too, keep the safe part: sentences and bullets carrying the unsupported
   figure or name are dropped, an unsupported headline falls back to the source's own title, and a
   summary left too short falls back to the article's opening sentences.

Everything here is a pure function over strings, so the rules are easy to test and impossible to
make expensive. `report()` returns what was caught, which the run's stats carry to the dashboard.
"""
from __future__ import annotations

import re

from .quality import numbers_in, same_figure
from .textutil import first_sentences, word_count

MIN_SOURCE_CHARS = 200   # less source text than this cannot support or contradict anything
MIN_SUMMARY_WORDS = 60   # a repaired summary shorter than this is not worth showing
YEAR = re.compile(r"^(?:19|20)\d{2}$")
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _flat(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def _digit_figures(text: str | None) -> list[tuple[str, float]]:
    """Figures written with digits. Spelled-out numbers ("three paragraphs") and bare years are
    left out: they say nothing about the facts and produce noise, and quality.py's site-side check
    already reads headlines more strictly."""
    out = []
    for written, value in numbers_in(text or ""):
        if not any(ch.isdigit() for ch in written):
            continue
        if YEAR.match(written.strip()):
            continue
        out.append((written, value))
    return out


def unsupported_figures(claim: str | None, source: str) -> list[str]:
    """Figures in the claim that no figure in the source matches."""
    found = [v for _w, v in numbers_in(source)]
    out = []
    for written, value in _digit_figures(claim):
        if not any(same_figure(value, f) for f in found):
            out.append(written)
    return list(dict.fromkeys(out))


def unsupported_names(names, claim: str | None, source: str) -> list[str]:
    """Named parties the claim uses that the source never names. Only names the claim actually
    uses are checked: a model listing an entity it did not write about is untidy, not wrong."""
    flat_source, low_claim = _flat(source), (claim or "").lower()
    out = []
    for name in names:
        name = str(name).strip()
        if len(name) < 3 or name.lower() not in low_claim:
            continue
        if _flat(name) not in flat_source:
            out.append(name)
    return list(dict.fromkeys(out))


def _names_of(clean: dict) -> list[str]:
    ents = clean.get("entities") or {}
    return [n for kind in ("companies", "models", "people") for n in (ents.get(kind) or [])]


def report(clean: dict, source: str) -> dict:
    """What the summary states that the article does not. Empty when the article gives too little
    text to judge, so a feed-only stub never blocks a summary."""
    out: dict = {"figures": [], "names": [], "where": []}
    if len(source or "") < MIN_SOURCE_CHARS:
        return out
    headline = clean.get("headline") or ""
    body = " ".join([clean.get("summary_md") or "", " ".join(clean.get("key_points") or []),
                     clean.get("why_it_matters") or ""])
    claim = f"{headline}\n{body}"
    out["figures"] = unsupported_figures(claim, source)
    out["names"] = unsupported_names(_names_of(clean), claim, source)
    bad = out["figures"] + out["names"]
    if bad:
        if any(b.lower() in headline.lower() for b in bad):
            out["where"].append("headline")
        if any(b.lower() in body.lower() for b in bad):
            out["where"].append("summary")
    return out


def items(rep: dict) -> list[str]:
    return list(dict.fromkeys(list(rep.get("figures") or []) + list(rep.get("names") or [])))


STRICTER = """
The answer you just gave used figures or names that the article does not contain: {items}.
Write the JSON object again, with the same fields, using only figures, dates and names that appear
in the article text above, exactly as the article writes them. Leave a fact out rather than guess
it, and do not convert or round any figure. Return only the JSON object.
"""


def stricter_prompt(prompt: str, rep: dict) -> str:
    """The same prompt with one correction appended, for a single retry."""
    return prompt + STRICTER.format(items=", ".join(f'"{i}"' for i in items(rep)[:6]))


def _drop_sentences(text: str, bad: list[str]) -> str:
    kept = []
    for para in (text or "").split("\n\n"):
        sentences = [s for s in SENTENCE_SPLIT.split(para) if s.strip()]
        keep = [s for s in sentences if not any(b.lower() in s.lower() for b in bad)]
        if keep:
            kept.append(" ".join(keep))
    return "\n\n".join(kept).strip()


def safer(clean: dict, rep: dict, row, source: str) -> tuple[dict, list[str]]:
    """The summary with the unsupported parts taken out. Returns the cleaned answer and what was
    changed, in plain words, for the run's stats."""
    bad = items(rep)
    if not bad:
        return clean, []
    clean = dict(clean)
    changed: list[str] = []
    headline = clean.get("headline") or ""
    if any(b.lower() in headline.lower() for b in bad):
        title = (getattr(row, "title", None) or headline).strip()[:160]
        if title and title != headline:
            clean["headline"] = title
            changed.append("headline replaced with the source's own title")
    summary = _drop_sentences(clean.get("summary_md") or "", bad)
    if summary != (clean.get("summary_md") or ""):
        changed.append("sentence dropped from the summary")
    if word_count(summary) < MIN_SUMMARY_WORDS:
        fallback = " ".join(first_sentences(source, 5))
        if word_count(fallback) >= word_count(summary):
            summary = fallback
            changed.append("summary replaced with the article's opening")
    clean["summary_md"] = summary
    points = [p for p in (clean.get("key_points") or []) if not any(b.lower() in p.lower() for b in bad)]
    if len(points) != len(clean.get("key_points") or []):
        changed.append("key point dropped")
    clean["key_points"] = points
    why = clean.get("why_it_matters") or ""
    if any(b.lower() in why.lower() for b in bad):
        clean["why_it_matters"] = _drop_sentences(why, bad)
        changed.append("line dropped from why it matters")
    lower_bad = {b.lower() for b in bad}
    ents = clean.get("entities") or {}
    clean["entities"] = {k: [n for n in (ents.get(k) or []) if str(n).lower() not in lower_bad]
                         for k in ("companies", "models", "people")}
    return clean, changed


def note(row, rep: dict, changed: list[str], retried: bool) -> dict:
    """One line for the dashboard: what was caught on which article, and what happened to it."""
    return {"id": getattr(row, "id", None),
            "headline": (getattr(row, "title", None) or "")[:120],
            "items": items(rep)[:4],
            "where": ", ".join(rep.get("where") or []) or "summary",
            "fixed": "retry" if retried and not changed else ("edited" if changed else "kept"),
            "changed": changed[:3]}
