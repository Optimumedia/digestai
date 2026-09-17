"""Step: the spoken briefing. Once a day the briefing stories are read aloud by an open-source
neural voice on the runner's CPU (Kokoro, with Piper as a fallback), encoded to MP3, and published
as /audio/briefing-<date>.mp3 with a podcast feed (/podcast.xml) and a /listen page.

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
lives with the store's other files in the read cache, and the newest KEEP go to site/src/data. Nothing here costs anything: no speech API, no hosting beyond the site itself.
"""
from __future__ import annotations

import json
import logging
import random
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config, media

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


def manifest_path() -> Path:
    return media.store_dir() / "episodes.json"


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


# Only what the voice gets wrong on its own. It already says AI, CEO, API, US, UK and percentages
# correctly (checked against the phonemes kokoro-onnx produces), but it reads a decimal point and a
# thousands separator as a pause, and puts a currency symbol before the number it belongs to.
_SPOKEN = [
    (r"\bdigestai\.news\b", "Digest AI dot news"),
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


def synthesize(text: str, out: Path, work: Path | None = None,
               budget: float | None = None) -> tuple[float, int] | None:
    """Read the script aloud into an MP3. Returns (seconds, bytes), or None when a work directory
    and a time budget were given and there are parts left for the next run."""
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
    started, read_now = time.time(), 0
    engine = None
    while plan["done"] < len(paragraphs):
        if budget is not None and read_now and time.time() - started >= budget:
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
    """Parts of episodes that were never finished (an old date, a changed briefing)."""
    for d in WORK_DIR.glob("*"):
        if d.is_dir() and d.name != keep:
            shutil.rmtree(d, ignore_errors=True)


# ---- step -----------------------------------------------------------------------------

def _read(path: Path) -> list[dict] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else None
    except (OSError, ValueError):
        return None


def _load_manifest() -> list[dict]:
    """Every episode. The first run after the move to the media store takes over the manifest and
    the files that the old cache kept in site/public/audio."""
    episodes = _read(manifest_path())
    if episodes is not None:
        return episodes
    legacy = _read(AUDIO_DIR / "episodes.json") or []
    for e in legacy:
        f = AUDIO_DIR / e["file"]
        if f.exists() and not media.has(e["file"]):
            media.queue(e["file"], f.read_bytes())
    if legacy:
        log.info("took over %d episodes from site/public/audio", len(legacy))
    return legacy


def _publish(episodes: list[dict]) -> None:
    episodes.sort(key=lambda e: e["date"], reverse=True)
    # Everything the store knows or still holds; the newest KEEP go to the site.
    episodes = [e for e in episodes if media.has(e["file"]) or (AUDIO_DIR / e["file"]).exists()]
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    served = {e["file"] for e in episodes[:PAGES_EPISODES]}
    for f in AUDIO_DIR.glob("*.mp3"):
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
    manifest_path().parent.mkdir(parents=True, exist_ok=True)
    manifest_path().write_text(json.dumps(episodes, ensure_ascii=False, indent=1), encoding="utf-8")
    config.SITE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    (config.SITE_DATA_DIR / "episodes.json").write_text(json.dumps(episodes[:KEEP], ensure_ascii=False, indent=1), encoding="utf-8")
    (AUDIO_DIR / "episodes.json").unlink(missing_ok=True)  # the old manifest, taken over


def refresh_urls() -> int:
    """After the media step uploaded episodes: point the feed at the store. Returns the episode count."""
    episodes = _read(manifest_path())
    if episodes is None:
        return 0
    _publish(episodes)
    return len(episodes)


def run() -> dict:
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
    now = datetime.now(timezone.utc)
    force = config.os.environ.get("AUDIO_FORCE") == "1"
    engine = engine_name()
    if any(e["date"] == date for e in episodes) and not force:
        stats["reason"] = "episode exists"
    elif now.hour < config.AUDIO_HOUR_UTC and not force:
        stats["reason"] = f"before {config.AUDIO_HOUR_UTC}:00 UTC"
    elif not engine:
        stats["reason"] = "voice not installed"
    else:
        try:
            import lameenc  # noqa: F401
            if engine == "kokoro":
                import kokoro_onnx  # noqa: F401
            else:
                import piper  # noqa: F401
        except ImportError as exc:
            stats["reason"] = f"missing dependency: {exc.name}"
            _publish(episodes)
            return stats
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
            made = synthesize(text, AUDIO_DIR / fname, work=WORK_DIR / date,
                              budget=config.AUDIO_TIME_BUDGET_SECONDS)
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
