"""Step: the spoken briefing. Once a day the five briefing stories are read aloud by an
open-source neural voice (Piper, on the runner's CPU), encoded to MP3, and published as
/audio/briefing-<date>.mp3 with a podcast feed (/podcast.xml) and a /listen page.

Episodes live in site/public/audio, which the workflow keeps between runs through the
Actions cache; the manifest next to them (episodes.json) is copied into site/src/data for
the build. Nothing here costs anything: no speech API, no hosting beyond the site itself.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from . import config

log = logging.getLogger("digest.audio")

AUDIO_DIR = config.ROOT / "site" / "public" / "audio"
VOICES_DIR = config.PIPELINE_DIR / "data" / "voices"
MANIFEST = AUDIO_DIR / "episodes.json"
KEEP = int(config.os.environ.get("AUDIO_KEEP_EPISODES") or 14)
BITRATE = 64  # kbps, mono speech


# ---- script ---------------------------------------------------------------------------

_ABBR = [
    (r"\$(\d[\d,]*(?:\.\d+)?)\s?(?:B|bn|billion)\b", r"\1 billion dollars"),
    (r"\$(\d[\d,]*(?:\.\d+)?)\s?(?:M|m|million)\b", r"\1 million dollars"),
    (r"\$(\d[\d,]*(?:\.\d+)?)\s?(?:K|k)\b", r"\1 thousand dollars"),
    (r"\$(\d[\d,]*(?:\.\d+)?)", r"\1 dollars"),
    (r"€(\d[\d,]*(?:\.\d+)?)\s?(?:bn|billion)\b", r"\1 billion euros"),
    (r"€(\d[\d,]*(?:\.\d+)?)", r"\1 euros"),
    (r"(\d)\s?%", r"\1 percent"),
    (r"\bAI\b", "A.I."),
    (r"\bLLMs?\b", "large language models"),
    (r"\bGPUs?\b", "G P Us"),
    (r"\bAPIs?\b", "A P I"),
    (r"\bCEO\b", "C E O"),
    (r"\bEU\b", "E U"),
    (r"\bUS\b", "U S"),
    (r"\bUK\b", "U K"),
    (r"\bvs\.?\b", "versus"),
    (r"\be\.g\.", "for example"),
    (r"\bi\.e\.", "that is"),
]


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


def spoken(text: str) -> str:
    for pat, rep in _ABBR:
        text = re.sub(pat, rep, text)
    text = text.replace("—", ", ").replace("–", " to ").replace("‑", "-").replace(" ", " ").replace("&", " and ")
    text = re.sub(r"[“”]", '"', text)
    text = re.sub(r"[‘’]", "'", text)
    return text


def build_script(briefing: dict, stories: dict[int, dict]) -> tuple[str, list[dict]]:
    date = datetime.fromisoformat(briefing["date"]).strftime("%A, %d %B %Y").replace(" 0", " ")
    picks = [stories[i] for i in briefing.get("storyIds", []) if i in stories]
    lines = [f"This is the Digest A.I. briefing for {date}. {len(picks)} stories that matter today, with sources for each one at digest a i dot news."]
    for n, s in enumerate(picks, 1):
        head = _plain(s.get("headline"))
        summary = _sentences(_plain(s.get("summaryMd")), 3)
        why = _sentences(_plain(s.get("whyItMatters")), 1)
        lines.append(f"Story {n}. {head}. {summary}" + (f" Why it matters: {why}" if why else ""))
    lines.append("That is the briefing. Every story, its sources and the discussion around it are on digest a i dot news, updated every thirty minutes. Back tomorrow.")
    return "\n\n".join(spoken(l) for l in lines), picks


# ---- synthesis ------------------------------------------------------------------------

def _voice_paths() -> tuple[Path, Path] | None:
    name = config.AUDIO_VOICE
    model = VOICES_DIR / f"{name}.onnx"
    cfg = VOICES_DIR / f"{name}.onnx.json"
    return (model, cfg) if model.exists() and cfg.exists() else None


def synthesize(text: str, out: Path) -> tuple[float, int]:
    """Text to MP3 with Piper + LAME. Returns (seconds, bytes)."""
    from piper import PiperVoice, SynthesisConfig
    import lameenc

    paths = _voice_paths()
    if not paths:
        raise RuntimeError(f"voice {config.AUDIO_VOICE} not found in {VOICES_DIR}")
    voice = PiperVoice.load(str(paths[0]), config_path=str(paths[1]))
    syn = SynthesisConfig(length_scale=1.05)
    enc = lameenc.Encoder()
    enc.set_bit_rate(BITRATE)
    enc.set_in_sample_rate(voice.config.sample_rate)
    enc.set_channels(1)
    enc.set_quality(2)
    samples = 0
    mp3 = bytearray()
    for para in [p for p in text.split("\n\n") if p.strip()]:
        for chunk in voice.synthesize(para, syn):
            pcm = chunk.audio_int16_bytes
            samples += len(pcm) // 2
            mp3 += enc.encode(pcm)
        # a beat between stories
        pause = b"\x00\x00" * int(voice.config.sample_rate * 0.6)
        samples += len(pause) // 2
        mp3 += enc.encode(pause)
    mp3 += enc.flush()
    out.write_bytes(bytes(mp3))
    return samples / voice.config.sample_rate, len(mp3)


# ---- step -----------------------------------------------------------------------------

def _load_manifest() -> list[dict]:
    if MANIFEST.exists():
        try:
            return json.loads(MANIFEST.read_text(encoding="utf-8"))
        except ValueError:
            return []
    return []


def _publish(episodes: list[dict]) -> None:
    episodes.sort(key=lambda e: e["date"], reverse=True)
    # keep only the newest KEEP episodes whose file still exists
    kept = []
    for e in episodes:
        f = AUDIO_DIR / e["file"]
        if not f.exists():
            continue
        if len(kept) >= KEEP:
            f.unlink(missing_ok=True)
            continue
        kept.append(e)
    MANIFEST.write_text(json.dumps(kept, ensure_ascii=False, indent=1), encoding="utf-8")
    config.SITE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(MANIFEST, config.SITE_DATA_DIR / "episodes.json")


def run() -> dict:
    stats = {"generated": 0, "episodes": 0, "reason": ""}
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
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
    if any(e["date"] == date for e in episodes) and not force:
        stats["reason"] = "episode exists"
    elif now.hour < config.AUDIO_HOUR_UTC and not force:
        stats["reason"] = f"before {config.AUDIO_HOUR_UTC}:00 UTC"
    elif not _voice_paths():
        stats["reason"] = "voice not installed"
    else:
        try:
            import lameenc  # noqa: F401
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
        try:
            seconds, size = synthesize(text, AUDIO_DIR / fname)
        except Exception as exc:  # noqa: BLE001
            log.warning("synthesis failed: %s", str(exc)[:200])
            stats["reason"] = "synthesis failed"
            _publish(episodes)
            return stats
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
        log.info("episode %s: %.0f s, %d KB", date, seconds, size // 1024)
    _publish(episodes)
    stats["episodes"] = len(_load_manifest())
    return stats
