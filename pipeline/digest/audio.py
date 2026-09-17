"""Step: the spoken briefing, and the spoken week of AI at Work. Both are read aloud by an
open-source neural voice on the runner's CPU (Kokoro, with Piper as a fallback), encoded to MP3 and
published from the media store.

There are two kinds of episode (KINDS), and they differ only in what the script says and how often
one appears:

- "briefing": the daily news briefing, /audio/briefing-<date>.mp3, in the podcast feed at
  /podcast.xml and on /listen. Once a day from AUDIO_HOUR_UTC.
- "work": the AI at Work section (/work), /audio/work-<year>-W<week>.mp3, in its own feed at
  /work/podcast.xml, with a player on /work and its own heading on /listen. Once a week, on
  WORK_AUDIO_WEEKDAY from WORK_AUDIO_HOUR_UTC, reading the ISO week that ended the day before. The
  section produces about six practical items a week, so a daily section episode would be two
  sentences long on most days and four items long on one; a weekly one is a show. It is built from
  what the export already wrote (site/src/data/work.json and stories.json) and reads no database.

Both use the same engine, voice, part cache and time budget. The briefing has first call on the
budget: the section starts a part only while WORK_AUDIO_MIN_BUDGET_SECONDS of the run's budget is
still unspent, so a briefing in the middle of being read is never slowed down and the section's
parts simply wait for the next run.

Two things decide whether it sounds like a person. The script (build_script) is written the way a
presenter reads: a greeting and the date, the news itself in a sentence or two, varying transitions
instead of "Story 1", and a close that says where the sources are. The voice is Kokoro (82M
parameters, Apache-2.0), which reads at about one second of CPU per second of speech, so an episode
costs minutes rather than seconds.

That cost is why synthesis is resumable. The script is split into parts (the opening, one per story,
the close); each run encodes parts until AUDIO_TIME_BUDGET_SECONDS is spent and leaves the rest,
with the finished parts, for the next run. Parts wait in the read cache (pipeline/data/cache/audio),
which the workflow already keeps between runs, and the episode is published only once every part is
done. So a slow synthesis can never eat the run's time limit, and nothing is ever synthesised twice.

Every episode goes to the media store (media.py) once, as a release asset. The store serves
files as downloads without an audio type, which some podcast apps and Safari handle badly, so
the newest PAGES_EPISODES episodes are also kept in site/public/audio and served by Pages; the
feed and player link there, and to the store for older ones. site/public/audio is kept between
runs by a cache the workflow saves when a new episode appears (once a day); a missing file is
downloaded back from the store. The manifest (episodes.json, every episode with its transcript)
lives with the store's other files in the read cache, and the newest KEEP go to site/src/data. Each kind has its own
manifest (episodes.json, work-episodes.json) so a podcast app subscribed to the daily news show never
sees the weekly section show, and the other way round. Nothing here costs anything: no speech API, no
hosting beyond the site itself.
"""
from __future__ import annotations

import json
import logging
import random
import re
import shutil
import time
from datetime import date as _date, datetime, timedelta, timezone
from pathlib import Path

from . import config, media, work as work_rules

log = logging.getLogger("digest.audio")

AUDIO_DIR = config.ROOT / "site" / "public" / "audio"   # the newest episodes, served by Pages
PAGES_EPISODES = int(config.os.environ.get("AUDIO_PAGES_EPISODES") or 7)
VOICES_DIR = config.PIPELINE_DIR / "data" / "voices"    # Piper voices (the fallback engine)
KOKORO_DIR = config.PIPELINE_DIR / "data" / "kokoro"    # kokoro-onnx model + voice pack
KOKORO_MODEL = "kokoro-v1.0.int8.onnx"
KOKORO_VOICES = "voices-v1.0.bin"
WORK_DIR = config.CACHE_DIR / "audio"                   # parts of an episode still being read
KEEP = int(config.os.environ.get("AUDIO_KEEP_EPISODES") or 14)
BITRATE = 64  # kbps, mono speech
PARA_PAUSE = 0.6   # seconds of silence between stories
CHUNK_PAUSE = 0.1  # seconds between the sentence-sized chunks inside one story
CHUNK_CHARS = 280  # a chunk is one or two sentences, never longer than this

# The two kinds of episode. `prefix` names the files and tells the kinds apart in site/public/audio,
# `manifest` is the file in the media store (and the copy in site/src/data), `pages` is how many of
# the newest are served by Pages and `keep` how many the site's copy of the manifest lists.
KINDS: dict[str, dict] = {
    "briefing": {"prefix": "briefing-", "manifest": "episodes.json", "pages": PAGES_EPISODES, "keep": KEEP},
    "work": {"prefix": "work-", "manifest": "work-episodes.json",
             "pages": int(config.os.environ.get("WORK_AUDIO_PAGES_EPISODES") or 4),
             "keep": int(config.os.environ.get("WORK_AUDIO_KEEP_EPISODES") or 26)},
}


def manifest_path(kind: str = "briefing") -> Path:
    return media.store_dir() / KINDS[kind]["manifest"]


# ---- script ---------------------------------------------------------------------------
# What a listener hears is what /listen shows: build_script writes ordinary prose, and spoken()
# rewrites only what the voice would otherwise get wrong, just before synthesis.

_ONES = ["zeroth", "first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth",
         "eleventh", "twelfth", "thirteenth", "fourteenth", "fifteenth", "sixteenth", "seventeenth", "eighteenth",
         "nineteenth"]
_TENS = {20: "twentieth", 30: "thirtieth"}
_TENS_PREFIX = {20: "twenty", 30: "thirty"}
_COUNT = ["no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"]

# Lead-ins, so no two stories in an episode open the same way and no two days sound alike.
_FIRST = ("We start here.", "First today.", "Top of the list.", "We begin with this.", "First up.")
_MIDDLE = ("Next,", "Also today,", "Then this.", "Elsewhere,", "Next up,", "Also in the news,",
           "Moving on,", "There is more.", "Then,", "Also worth knowing,")
_LAST = ("And finally,", "One last story.", "And to finish,", "Last today,")
_WHY = ("Why that matters:", "Why it matters:", "What that means:", "The point of it:")
# Words that may be lowercased when a comma lead-in runs into them; never a name.
_LOWERCASE_AFTER_COMMA = {
    "a", "an", "the", "this", "that", "these", "those", "it", "its", "they", "their", "there", "he", "she", "his",
    "her", "in", "on", "at", "for", "after", "before", "with", "one", "two", "three", "four", "five", "more",
    "most", "new", "another", "following", "now", "both", "all", "some", "many", "almost", "nearly", "up", "over",
    "under", "about", "as", "if", "when", "while", "researchers", "regulators", "lawmakers", "investors",
}


def _ordinal(n: int) -> str:
    if n < 20:
        return _ONES[n]
    if n in _TENS:
        return _TENS[n]
    return f"{_TENS_PREFIX[n // 10 * 10]}-{_ONES[n % 10]}"


def _count(n: int) -> str:
    return _COUNT[n] if 0 <= n < len(_COUNT) else str(n)


def _plain(md: str | None) -> str:
    s = md or ""
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)     # links
    s = re.sub(r"[*_`#>]+", "", s)                      # markdown marks
    s = re.sub(r"https?://\S+", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _sentences(text: str, n: int) -> str:
    parts = re.split(r"(?<=[.!?])\s+", text)
    return " ".join(parts[:n]).strip()


def _unmeta(text: str) -> str:
    """A summary that opens by pointing at its own source ("In this piece, ...") is read as news."""
    text = re.sub(r"^In th(?:is|e) [a-z ]{0,24}?(?:piece|article|post|essay|report|analysis|paper|"
                  r"newsletter|video|interview|study),\s*", "", text)
    return text[0].upper() + text[1:] if text else text


def _as_sentence(text: str) -> str:
    text = text.strip().rstrip(" .;:,")
    return f"{text}." if text else ""


def _join(lead: str, body: str) -> str:
    """A lead-in and the sentence after it. A comma lead-in runs into the sentence, so its first
    word loses its capital when it is an ordinary word (never a name)."""
    if lead.endswith(",") and body:
        first = re.match(r"[A-Za-z]+", body)
        if first and first.group(0).lower() in _LOWERCASE_AFTER_COMMA:
            body = body[0].lower() + body[1:]
    return f"{lead} {body}".strip()


def build_script(briefing: dict, stories: dict[int, dict]) -> tuple[str, list[dict]]:
    """The episode as a presenter would read it. Paragraphs are the parts synthesis works in:
    the opening, one per story, the close."""
    day = datetime.fromisoformat(briefing["date"])
    picks = [stories[i] for i in briefing.get("storyIds", []) if i in stories]
    rnd = random.Random(f"digest-audio-{briefing['date']}")
    middles = list(_MIDDLE)
    rnd.shuffle(middles)
    whys = list(_WHY)
    rnd.shuffle(whys)

    date = f"{day.strftime('%A')} the {_ordinal(day.day)} of {day.strftime('%B')}"
    lines = [f"Good morning. This is the Digest AI briefing for {date}. "
             f"{_count(len(picks)).capitalize()} {'story' if len(picks) == 1 else 'stories'} today."]
    for n, s in enumerate(picks):
        if n == 0:
            lead = rnd.choice(_FIRST)
        elif n == len(picks) - 1:
            lead = rnd.choice(_LAST)
        else:
            lead = middles[(n - 1) % len(middles)]
        body = _unmeta(_sentences(_plain(s.get("summaryMd")), 2)) or _as_sentence(_plain(s.get("headline")))
        why = _sentences(_plain(s.get("whyItMatters")), 1)
        line = _join(lead, body)
        if why:
            line += f" {whys[n % len(whys)]} {_as_sentence(why)}"
        lines.append(line.strip())
    lines.append("That's the briefing. Every story, with its sources and the discussion around it, "
                 "is at digestai.news, and the site updates every hour. Back tomorrow.")
    return "\n\n".join(lines), picks


# ---- the week of AI at Work -----------------------------------------------------------
# The section's cards are a form, not prose: tool, what it does, who for, cost, effort, the catch.
# Read out field by field they sound like a spreadsheet, so each one becomes a short spoken
# paragraph instead: what it is, who it helps, what it costs and how long it takes in one sentence,
# and the caveat last, because that is the sentence a listener acts on.

_WORK_FIRST = ("Start with this one.", "First up.", "Top of the list.", "We begin here.")
_WORK_MIDDLE = ("Next,", "Then this.", "Also this week,", "Moving on,", "There is more.",
                "Next up,", "Elsewhere,", "Also worth your time,")
_WORK_LAST = ("And the last one.", "One more.", "And finally,", "Last this week,")
_WORK_USE = ("Use it to", "Handy for when you need to", "Worth it if you have to", "Good for when you want to")
_WORK_WATCH = ("One thing to watch:", "The catch:", "What to know first:", "Before you start:")
_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September",
           "October", "November", "December")
# How the cost and the setting-up are said, from the card's own costKind and effort.
_COST_SAID = {
    "free": "It is free",
    "free tier": "There is a free tier",
    "included": "It is already included in something most teams pay for",
    "paid": "It is paid",
    "unknown": "The price is not stated",
}
_EFFORT_SAID = {
    "minutes": "and you can be using it in minutes.",
    "an afternoon": "and setting it up is an afternoon's work.",
    "needs a developer": "but someone will have to write code to make it work.",
}


def week_range(week: str) -> tuple[_date, _date]:
    """Monday and Sunday of an ISO week key like 2026-W38."""
    year, number = int(week[:4]), int(week.split("W")[1])
    fourth = _date(year, 1, 4)
    monday = fourth - timedelta(days=fourth.isoweekday() - 1) + timedelta(weeks=number - 1)
    return monday, monday + timedelta(days=6)


def _spoken_day(day: _date) -> str:
    return f"the {_ordinal(day.day)} of {_MONTHS[day.month - 1]}"


def _who_said(who: list[str]) -> str:
    names = [work_rules.WHO_LABELS.get(w, w) for w in who] or ["small teams"]
    if len(names) == 1:
        return names[0]
    return f"{', '.join(names[:-1])} and {names[-1]}"


def _cost_said(card: dict) -> str:
    kind = card.get("costKind") or work_rules.cost_kind(card.get("cost") or "")
    said = _COST_SAID.get(kind, _COST_SAID["unknown"])
    # A price the card states in words ("starting at $2.99/month") is more use than "it is paid".
    cost = re.sub(r"^(?:starting|starts) (?:at|from)\s+", "from ", _plain(card.get("cost") or ""), flags=re.I)
    if kind == "paid" and cost and not cost.lower().startswith("paid"):
        said = f"It costs {cost.rstrip('.')}"
    effort = _EFFORT_SAID.get(card.get("effort") or "")
    return f"{said}, {effort}" if effort else f"{said}."


def _tool_said(card: dict) -> str:
    """"Gemini for Workspace, from Google, lets Gemini read your CRM." The card's "what it does" is
    always a third-person verb phrase, so the tool's name is simply the subject in front of it."""
    tool = _plain(card.get("tool") or "").rstrip(".")
    maker = _plain(card.get("maker") or "").rstrip(".")
    what = _as_sentence(_plain(card.get("whatItDoes") or ""))
    what = what[0].lower() + what[1:] if what else ""
    if maker and maker.lower() not in tool.lower():
        return f"{tool}, from {maker}, {what}"
    return f"{tool} {what}"


def build_work_script(week: str, items: list[dict], skips: list[dict] | None = None) -> tuple[str, list[dict]]:
    """The section's week as a presenter would read it. Paragraphs are the parts synthesis works in:
    the opening, one per item, what to leave for now (when there is any), the close."""
    picks = items[: config.WORK_AUDIO_ITEMS]
    skips = skips or []
    monday, sunday = week_range(week)
    rnd = random.Random(f"digest-work-audio-{week}")
    middles = list(_WORK_MIDDLE)
    rnd.shuffle(middles)
    uses = list(_WORK_USE)
    rnd.shuffle(uses)
    watches = list(_WORK_WATCH)
    rnd.shuffle(watches)

    free = sum(1 for s in picks if (s["workCard"].get("costKind") or "") in ("free", "free tier", "included"))
    # "the week of the eighth to the fourteenth of September", or both months when the week straddles two.
    first = f"the {_ordinal(monday.day)}" if monday.month == sunday.month else _spoken_day(monday)
    when = f"the week of {first} to {_spoken_day(sunday)}"
    opening = (f"This is AI at Work from Digest AI: a five-minute run through what changed for marketers "
               f"and small teams {when}. {_count(len(picks)).capitalize()} "
               f"{'thing' if len(picks) == 1 else 'things'} to know")
    if free:
        opening += f", and {_count(free)} of them {'costs' if free == 1 else 'cost'} nothing to start"
    lines = [opening + "."]

    for n, story in enumerate(picks):
        card = story["workCard"]
        if n == 0:
            lead = rnd.choice(_WORK_FIRST)
        elif n == len(picks) - 1:
            lead = rnd.choice(_WORK_LAST)
        else:
            lead = middles[(n - 1) % len(middles)]
        parts = [_join(lead, _tool_said(card)), f"It is for {_who_said(card.get('whoFor') or [])}.",
                 _cost_said(card)]
        # One or two of the card's concrete uses: a listener needs an example, not the whole list.
        said_uses = [_plain(u).rstrip(".") for u in (card.get("useFor") or [])[:2] if _plain(u)]
        if said_uses:
            lower = [u[0].lower() + u[1:] for u in said_uses]
            parts.append(f"{uses[n % len(uses)]} {lower[0]}"
                         + (f", or to {lower[1]}." if len(lower) > 1 else "."))
        watch = _plain(card.get("watchOut") or "")
        if watch:
            parts.append(f"{watches[n % len(watches)]} {_as_sentence(watch)}")
        lines.append(" ".join(p for p in parts if p).strip())

    if skips:
        named = [_plain(s["workCard"]["tool"]).rstrip(".") for s in skips[:3]]
        reasons = [f"{name}, {(s['workCard'].get('skip') or 'not open to everyone yet')}"
                   for name, s in zip(named, skips[:3])]
        lines.append(f"{_count(len(reasons)).capitalize()} to leave for now: "
                     + "; ".join(reasons) + ". Not a verdict on any of them, only on this week.")

    lines.append("That is AI at Work for this week. Every item, with what it costs, how long it takes "
                 "and where to get it, is at digestai.news/work, and there is a new one next Monday.")
    return "\n\n".join(lines), picks


# Only what the voice gets wrong on its own. It already says AI, CEO, API, US, UK and percentages
# correctly (checked against the phonemes kokoro-onnx produces), but it reads a decimal point and a
# thousands separator as a pause, and puts a currency symbol before the number it belongs to.
_SPOKEN = [
    (r"\bdigestai\.news/work\b", "Digest AI dot news, slash work"),
    (r"\bdigestai\.news\b", "Digest AI dot news"),
    (r"(?<=\d)/month\b", " a month"),     # "$2.99/month", as a card states a price
    (r"(?<=\d)/mo\b", " a month"),
    (r"(?<=\d)/yr\b", " a year"),
    (r"(?<=\d)/seat\b", " per seat"),
    (r"\$(\d[\d,]*(?:\.\d+)?)\s?(?:B|bn|billion)\b", r"\1 billion dollars"),
    (r"\$(\d[\d,]*(?:\.\d+)?)\s?(?:M|m|million)\b", r"\1 million dollars"),
    (r"\$(\d[\d,]*(?:\.\d+)?)\s?(?:K|k)\b", r"\1 thousand dollars"),
    (r"\$(\d[\d,]*(?:\.\d+)?)", r"\1 dollars"),
    (r"€(\d[\d,]*(?:\.\d+)?)\s?(?:B|bn|billion)\b", r"\1 billion euros"),
    (r"€(\d[\d,]*(?:\.\d+)?)\s?(?:M|m|million)\b", r"\1 million euros"),
    (r"€(\d[\d,]*(?:\.\d+)?)", r"\1 euros"),
    (r"£(\d[\d,]*(?:\.\d+)?)\s?(?:B|bn|billion)\b", r"\1 billion pounds"),
    (r"£(\d[\d,]*(?:\.\d+)?)", r"\1 pounds"),
    (r"(\d)\s?%", r"\1 percent"),
    (r"\bLLMs\b", "large language models"),
    (r"\bLLM\b", "large language model"),
    (r"\bvs\.?\b", "versus"),
    (r"\be\.g\.", "for example"),
    (r"\bi\.e\.", "that is"),
    (r"(?<=\d),(?=\d\d\d\b)", ""),        # 1,200 -> 1200, or it is read as "one, two hundred"
    (r"(?<=\d)\.(?=\d)", " point "),      # 3.3 -> 3 point 3, or the point is silent
    (r"(?<=\d)\s?-\s?(?=\d)", " to "),    # 2-3 years, or the hyphen is read as "dash"
]


def spoken(text: str) -> str:
    """The script as the voice should receive it. Never shown to a reader."""
    text = text.replace("—", ", ").replace("–", " to ").replace("‑", "-").replace(" ", " ").replace("&", " and ")
    for pat, rep in _SPOKEN:
        text = re.sub(pat, rep, text)
    text = re.sub(r"[“”]", '"', text)
    text = re.sub(r"[‘’]", "'", text)
    return re.sub(r"[ \t]+", " ", text)


def _chunks(paragraph: str) -> list[str]:
    """Sentence-sized pieces. The voice plans its pitch over one call, so short pieces keep the
    pace even and stay well inside the model's token limit."""
    out: list[str] = []
    for sentence in re.split(r"(?<=[.!?:])\s+", paragraph.strip()):
        if not sentence:
            continue
        while len(sentence) > CHUNK_CHARS:                       # a very long sentence: break at a comma
            cut = sentence.rfind(", ", 0, CHUNK_CHARS)
            if cut < CHUNK_CHARS // 3:
                cut = sentence.rfind(" ", 0, CHUNK_CHARS)
            if cut <= 0:
                break
            out.append(sentence[:cut + 1].strip())
            sentence = sentence[cut + 1:].strip()
        if out and len(out[-1]) + len(sentence) + 1 <= CHUNK_CHARS and not out[-1].endswith(":"):
            out[-1] = f"{out[-1]} {sentence}"
        else:
            out.append(sentence)
    return out


# ---- synthesis ------------------------------------------------------------------------

def _piper_paths() -> tuple[Path, Path] | None:
    name = config.AUDIO_PIPER_VOICE if config.AUDIO_ENGINE == "kokoro" else config.AUDIO_VOICE
    model = VOICES_DIR / f"{name}.onnx"
    cfg = VOICES_DIR / f"{name}.onnx.json"
    return (model, cfg) if model.exists() and cfg.exists() else None


def _kokoro_paths() -> tuple[Path, Path] | None:
    model, voices = KOKORO_DIR / KOKORO_MODEL, KOKORO_DIR / KOKORO_VOICES
    return (model, voices) if model.exists() and voices.exists() else None


def engine_name() -> str | None:
    """Which engine this run can use, or None if neither is installed."""
    if config.AUDIO_ENGINE != "kokoro":
        return "piper" if _piper_paths() else None
    if _kokoro_paths():
        return "kokoro"
    if _piper_paths():
        log.warning("Kokoro model files missing in %s; falling back to the Piper voice %s",
                    KOKORO_DIR, config.AUDIO_PIPER_VOICE)
        return "piper"
    return None


class _Kokoro:
    """kokoro-onnx: one call gives float32 samples at 24 kHz."""

    def __init__(self, paths: tuple[Path, Path]):
        import numpy
        from kokoro_onnx import Kokoro

        self._np = numpy
        self.k = Kokoro(str(paths[0]), str(paths[1]))
        self.rate = 24000

    def pcm(self, text: str) -> bytes:
        samples, rate = self.k.create(text, voice=config.AUDIO_VOICE, speed=config.AUDIO_SPEED,
                                      lang=config.AUDIO_LANG)
        self.rate = rate
        return (self._np.clip(samples, -1.0, 1.0) * 32767).astype(self._np.int16).tobytes()


class _Piper:
    def __init__(self, paths: tuple[Path, Path]):
        from piper import PiperVoice, SynthesisConfig
        self.voice = PiperVoice.load(str(paths[0]), config_path=str(paths[1]))
        self.syn = SynthesisConfig(length_scale=1.05)
        self.rate = self.voice.config.sample_rate

    def pcm(self, text: str) -> bytes:
        return b"".join(c.audio_int16_bytes for c in self.voice.synthesize(text, self.syn))


def _open_engine():
    name = engine_name()
    if name == "kokoro":
        return _Kokoro(_kokoro_paths())
    if name == "piper":
        return _Piper(_piper_paths())
    raise RuntimeError(f"no voice installed for engine {config.AUDIO_ENGINE}")


def _encode(engine, paragraph: str) -> tuple[bytes, float]:
    """One paragraph as a complete little MP3. Parts are joined by concatenation, which works
    because every part is a whole number of constant-bitrate frames."""
    import lameenc

    enc = lameenc.Encoder()
    enc.set_bit_rate(BITRATE)
    enc.set_in_sample_rate(engine.rate)
    enc.set_channels(1)
    enc.set_quality(2)
    mp3 = bytearray()
    samples = 0
    pieces = _chunks(paragraph)
    for i, chunk in enumerate(pieces):
        pcm = engine.pcm(chunk)
        samples += len(pcm) // 2
        mp3 += enc.encode(pcm)
        gap = PARA_PAUSE if i == len(pieces) - 1 else CHUNK_PAUSE
        silence = b"\x00\x00" * int(engine.rate * gap)
        samples += len(silence) // 2
        mp3 += enc.encode(silence)
    mp3 += enc.flush()
    return bytes(mp3), samples / engine.rate


def _paragraphs(text: str) -> list[str]:
    return [p.strip() for p in text.split("\n\n") if p.strip()]


def _plan_path(work: Path) -> Path:
    return work / "plan.json"


def _read_plan(work: Path, paragraphs: list[str]) -> dict:
    """What an earlier run already read aloud, if the script has not changed since."""
    try:
        plan = json.loads(_plan_path(work).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        plan = {}
    whole = (plan.get("parts") == paragraphs and plan.get("engine") == engine_name()
             and plan.get("voice") == config.AUDIO_VOICE
             and all((work / f"{i:03d}.mp3").exists() for i in range(plan.get("done", 0))))
    if whole:
        return plan
    if plan:
        log.info("the script or a finished part changed since the last run; starting the episode again")
    shutil.rmtree(work, ignore_errors=True)
    return {"parts": paragraphs, "engine": engine_name(), "voice": config.AUDIO_VOICE, "done": 0, "seconds": 0.0}


def synthesize(text: str, out: Path, work: Path | None = None, budget: float | None = None,
               at_least_one: bool = True) -> tuple[float, int] | None:
    """Read the script aloud into an MP3. Returns (seconds, bytes), or None when a work directory
    and a time budget were given and there are parts left for the next run.

    at_least_one reads one part however little budget is left, so an episode always moves forward.
    The daily briefing is read that way; the section episode is not, because it shares the run's
    budget with the briefing and must give way rather than push the run past it.
    """
    paragraphs = _paragraphs(spoken(text))
    if work is None:
        engine = _open_engine()
        mp3, seconds = bytearray(), 0.0
        for para in paragraphs:
            data, secs = _encode(engine, para)
            mp3 += data
            seconds += secs
        out.write_bytes(bytes(mp3))
        return seconds, len(mp3)

    plan = _read_plan(work, paragraphs)          # may clear the directory, so create it after
    work.mkdir(parents=True, exist_ok=True)
    _plan_path(work).write_text(json.dumps(plan), encoding="utf-8")   # a run that reads nothing still leaves its state
    started, read_now = time.time(), 0
    engine = None
    while plan["done"] < len(paragraphs):
        if budget is not None and (read_now or not at_least_one) and time.time() - started >= budget:
            log.info("read %d of %d parts in %.0f s; the rest follows on the next run",
                     plan["done"], len(paragraphs), time.time() - started)
            return None
        if engine is None:
            engine = _open_engine()
        data, secs = _encode(engine, paragraphs[plan["done"]])
        (work / f"{plan['done']:03d}.mp3").write_bytes(data)
        plan["done"] += 1
        read_now += 1
        plan["seconds"] = round(plan["seconds"] + secs, 3)
        _plan_path(work).write_text(json.dumps(plan), encoding="utf-8")
    mp3 = b"".join((work / f"{i:03d}.mp3").read_bytes() for i in range(len(paragraphs)))
    out.write_bytes(mp3)
    shutil.rmtree(work, ignore_errors=True)
    return plan["seconds"], len(mp3)


def _tidy_work(keep: str) -> None:
    """Parts of episodes that were never finished (an old date, a changed briefing, last week's
    section episode). Both kinds keep their parts here; a directory named "work-<week>" belongs to
    the section and any other to a briefing, so a kind only ever sweeps its own."""
    section = keep.startswith("work-")
    for d in WORK_DIR.glob("*"):
        if d.is_dir() and d.name.startswith("work-") == section and d.name != keep:
            shutil.rmtree(d, ignore_errors=True)


# ---- step -----------------------------------------------------------------------------

def _read(path: Path) -> list[dict] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else None
    except (OSError, ValueError):
        return None


def _load_manifest(kind: str = "briefing") -> list[dict]:
    """Every episode of one kind. The first run after the move to the media store takes over the
    briefing manifest and the files that the old cache kept in site/public/audio."""
    episodes = _read(manifest_path(kind))
    if episodes is not None:
        return episodes
    if kind != "briefing":
        return []
    legacy = _read(AUDIO_DIR / "episodes.json") or []
    for e in legacy:
        f = AUDIO_DIR / e["file"]
        if f.exists() and not media.has(e["file"]):
            media.queue(e["file"], f.read_bytes())
    if legacy:
        log.info("took over %d episodes from site/public/audio", len(legacy))
    return legacy


def _served(kind: str, episodes: list[dict] | None = None) -> set[str]:
    """The files of one kind that Pages serves: its newest few, newest first."""
    eps = episodes if episodes is not None else (_read(manifest_path(kind)) or [])
    return {e["file"] for e in sorted(eps, key=lambda e: e["date"], reverse=True)[: KINDS[kind]["pages"]]}


def _publish(episodes: list[dict], kind: str = "briefing") -> None:
    spec = KINDS[kind]
    episodes.sort(key=lambda e: e["date"], reverse=True)
    # Everything the store knows or still holds; the newest `keep` go to the site.
    episodes = [e for e in episodes if media.has(e["file"]) or (AUDIO_DIR / e["file"]).exists()]
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    served = _served(kind, episodes)
    # Only this kind's own older files are cleared: the other kind's episodes share the directory
    # and are published by its own call, which reads its own manifest.
    for f in AUDIO_DIR.glob(f"{spec['prefix']}*.mp3"):
        if f.name not in served:
            if not media.has(f.name):  # never drop the only copy
                media.queue(f.name, f.read_bytes())
            f.unlink(missing_ok=True)
    for e in episodes:
        f = AUDIO_DIR / e["file"]
        if e["file"] in served and not f.exists():
            raw = media.content(e["file"])
            if raw:
                f.write_bytes(raw)
        e["url"] = f"{config.SITE_URL}/audio/{e['file']}" if f.exists() else (media.url(e["file"]) or f"{config.SITE_URL}/audio/{e['file']}")
    manifest_path(kind).parent.mkdir(parents=True, exist_ok=True)
    manifest_path(kind).write_text(json.dumps(episodes, ensure_ascii=False, indent=1), encoding="utf-8")
    config.SITE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    (config.SITE_DATA_DIR / spec["manifest"]).write_text(
        json.dumps(episodes[: spec["keep"]], ensure_ascii=False, indent=1), encoding="utf-8")
    if kind == "briefing":
        (AUDIO_DIR / "episodes.json").unlink(missing_ok=True)  # the old manifest, taken over


def refresh_urls() -> int:
    """After the media step uploaded episodes: point both feeds at the store. Returns the number of
    episodes across the kinds."""
    total = 0
    for kind in KINDS:
        episodes = _read(manifest_path(kind))
        if episodes is None:
            continue
        _publish(episodes, kind)
        total += len(episodes)
    return total


def _engine_ready(engine: str | None) -> str | None:
    """The reason the run cannot synthesise anything, or None."""
    if not engine:
        return "voice not installed"
    try:
        import lameenc  # noqa: F401
        if engine == "kokoro":
            import kokoro_onnx  # noqa: F401
        else:
            import piper  # noqa: F401
    except ImportError as exc:
        return f"missing dependency: {exc.name}"
    return None


def _run_briefing(now: datetime, budget: float) -> dict:
    stats = {"generated": 0, "episodes": 0, "reason": ""}
    episodes = _load_manifest()
    briefing_file = config.SITE_DATA_DIR / "briefing.json"
    stories_file = config.SITE_DATA_DIR / "stories.json"
    if not briefing_file.exists() or not stories_file.exists():
        stats["reason"] = "no briefing"
        _publish(episodes)
        return stats
    briefing = json.loads(briefing_file.read_text(encoding="utf-8"))
    date = briefing["date"]
    force = config.os.environ.get("AUDIO_FORCE") == "1"
    engine = engine_name()
    if any(e["date"] == date for e in episodes) and not force:
        stats["reason"] = "episode exists"
    elif now.hour < config.AUDIO_HOUR_UTC and not force:
        stats["reason"] = f"before {config.AUDIO_HOUR_UTC}:00 UTC"
    elif _engine_ready(engine):
        stats["reason"] = _engine_ready(engine)
    else:
        stories = {s["id"]: s for s in json.loads(stories_file.read_text(encoding="utf-8"))}
        text, picks = build_script(briefing, stories)
        if len(picks) < 3:
            stats["reason"] = "briefing too thin"
            _publish(episodes)
            return stats
        fname = f"briefing-{date}.mp3"
        stats["engine"] = engine
        stats["voice"] = config.AUDIO_VOICE
        _tidy_work(date)
        try:
            AUDIO_DIR.mkdir(parents=True, exist_ok=True)
            made = synthesize(text, AUDIO_DIR / fname, work=WORK_DIR / date, budget=budget)
        except Exception as exc:  # noqa: BLE001
            log.warning("synthesis failed: %s", str(exc)[:200])
            stats["reason"] = "synthesis failed"
            _publish(episodes)
            return stats
        if made is None:
            plan = json.loads(_plan_path(WORK_DIR / date).read_text(encoding="utf-8"))
            stats["reason"] = "still reading"
            stats["parts"] = f"{plan['done']}/{len(plan['parts'])}"
            _publish(episodes)
            return stats
        seconds, size = made
        media.queue(fname, (AUDIO_DIR / fname).read_bytes())
        pretty = datetime.fromisoformat(date).strftime("%A %d %B %Y").replace(" 0", " ")
        episodes = [e for e in episodes if e["date"] != date] + [{
            "date": date,
            "title": f"AI briefing, {pretty}: {picks[0]['headline']}",
            "file": fname,
            "url": f"{config.SITE_URL}/audio/{fname}",
            "bytes": size,
            "seconds": round(seconds),
            "publishedAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "description": "; ".join(p["headline"] for p in picks),
            "stories": [{"slug": p["slug"], "headline": p["headline"]} for p in picks],
            "transcript": text,
        }]
        stats["generated"] = 1
        stats["seconds"] = round(seconds)
        log.info("episode %s: %.0f s, %d KB, %s voice %s", date, seconds, size // 1024, engine, config.AUDIO_VOICE)
    _publish(episodes)
    stats["episodes"] = len(_read(manifest_path()) or [])
    return stats


# ---- the section's weekly episode ------------------------------------------------------

def last_week(now: datetime) -> str:
    """The ISO week that ended before today: the week the Monday episode reads."""
    monday = now.date() - timedelta(days=now.weekday())
    y, w, _ = (monday - timedelta(days=1)).isocalendar()
    return f"{y}-W{w:02d}"


def _too_early(now: datetime) -> bool:
    """Before the publication day and hour of the week now runs in."""
    if now.weekday() < config.WORK_AUDIO_WEEKDAY:
        return True
    return now.weekday() == config.WORK_AUDIO_WEEKDAY and now.hour < config.WORK_AUDIO_HOUR_UTC


def week_items(week: str, work_data: dict, stories: dict[int, dict]) -> tuple[list[dict], list[dict]]:
    """That week's practical items from what the export already wrote: what to try (most useful
    first) and what to leave for now. No database read, and no ranking done again here."""
    bucket = (work_data.get("weeks") or {}).get(week) or {}

    def pick(key: str) -> list[dict]:
        return [stories[i] for i in bucket.get(key) or [] if i in stories and stories[i].get("workCard")]

    return pick("try"), pick("skip")


def _run_work(now: datetime, budget: float) -> dict:
    """The AI at Work episode: once a week, and only with what is left of the run's time budget."""
    stats = {"generated": 0, "episodes": 0, "reason": ""}
    episodes = _load_manifest("work")
    work_file = config.SITE_DATA_DIR / "work.json"
    stories_file = config.SITE_DATA_DIR / "stories.json"
    force = config.os.environ.get("WORK_AUDIO_FORCE") == "1"
    if not config.WORK_AUDIO:
        stats["reason"] = "off"
        return stats
    if not work_file.exists() or not stories_file.exists():
        stats["reason"] = "no section data"
        _publish(episodes, "work")
        return stats
    week = config.os.environ.get("WORK_AUDIO_WEEK") or last_week(now)
    stats["week"] = week
    engine = engine_name()
    if any(e.get("week") == week for e in episodes) and not force:
        stats["reason"] = "episode exists"
    elif _too_early(now) and not force:
        # On or after the chosen day, not only on it: a week whose episode could not be finished on
        # the Monday (a lost runner, a busy budget) is still published later that week, because
        # last_week() names the same week every day of it.
        stats["reason"] = f"before {_WEEKDAYS[config.WORK_AUDIO_WEEKDAY]} {config.WORK_AUDIO_HOUR_UTC}:00 UTC"
    elif _engine_ready(engine):
        stats["reason"] = _engine_ready(engine)
    elif budget < config.WORK_AUDIO_MIN_BUDGET_SECONDS and not force:
        # The briefing spent the run's budget. The section waits: the hourly runs give it many more
        # chances this Monday, and a delayed episode is cheaper than a delayed run.
        stats["reason"] = "no time left this run"
    else:
        work_data = json.loads(work_file.read_text(encoding="utf-8"))
        stories = {s["id"]: s for s in json.loads(stories_file.read_text(encoding="utf-8"))}
        items, skips = week_items(week, work_data, stories)
        if len(items) < config.WORK_AUDIO_MIN_ITEMS:
            stats["reason"] = "week too thin"
            stats["items"] = len(items)
            _publish(episodes, "work")
            return stats
        text, picks = build_work_script(week, items, skips)
        fname = f"work-{week}.mp3"
        stats["engine"], stats["voice"], stats["items"] = engine, config.AUDIO_VOICE, len(picks)
        _tidy_work(f"work-{week}")
        try:
            AUDIO_DIR.mkdir(parents=True, exist_ok=True)
            made = synthesize(text, AUDIO_DIR / fname, work=WORK_DIR / f"work-{week}",
                              budget=budget, at_least_one=force)
        except Exception as exc:  # noqa: BLE001
            log.warning("section synthesis failed: %s", str(exc)[:200])
            stats["reason"] = "synthesis failed"
            _publish(episodes, "work")
            return stats
        if made is None:
            plan = json.loads(_plan_path(WORK_DIR / f"work-{week}").read_text(encoding="utf-8"))
            stats["reason"] = "still reading"
            stats["parts"] = f"{plan['done']}/{len(plan['parts'])}"
            _publish(episodes, "work")
            return stats
        seconds, size = made
        media.queue(fname, (AUDIO_DIR / fname).read_bytes())
        monday, sunday = week_range(week)
        pretty = (f"{monday.day}-{sunday.strftime('%d %B %Y')}" if monday.month == sunday.month
                  else f"{monday.strftime('%d %B')} to {sunday.strftime('%d %B %Y')}").replace(" 0", " ")
        episodes = [e for e in episodes if e.get("week") != week] + [{
            # The episode is dated the Monday it comes out, which is the day after its week ends.
            "date": (sunday + timedelta(days=1)).isoformat(),
            "week": week,
            "title": f"AI at Work, {pretty}: {picks[0]['workCard']['tool']}",
            "file": fname,
            "url": f"{config.SITE_URL}/audio/{fname}",
            "bytes": size,
            "seconds": round(seconds),
            "publishedAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "description": "; ".join(f"{p['workCard']['tool']}: {p['workCard']['whatItDoes']}" for p in picks),
            "stories": [{"slug": p["slug"], "headline": f"{p['workCard']['tool']}: {p['workCard']['whatItDoes']}"}
                        for p in picks],
            "transcript": text,
        }]
        stats["generated"] = 1
        stats["seconds"] = round(seconds)
        log.info("section episode %s: %.0f s, %d KB, %d items", week, seconds, size // 1024, len(picks))
    _publish(episodes, "work")
    stats["episodes"] = len(_read(manifest_path("work")) or [])
    return stats


_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def run() -> dict:
    """Both episodes, inside one shared time budget. The briefing goes first and may use all of it;
    whatever is left goes to the section."""
    started = time.time()
    now = datetime.now(timezone.utc)
    stats = _run_briefing(now, config.AUDIO_TIME_BUDGET_SECONDS)
    left = config.AUDIO_TIME_BUDGET_SECONDS - (time.time() - started)
    stats["work"] = _run_work(now, left)
    stats["budgetLeft"] = round(max(0.0, left), 1)
    return stats
