"""Trim the self-hosted Newsreader files to the weights the site uses.

Google Fonts serves Newsreader with its whole weight axis (200 to 800) and its optical-size axis
(6 to 72). The site's @font-face rules declare font-weight 400 600 (site/src/styles/global.css), so
a browser never draws Newsreader outside that range, yet every first visit downloaded the outlines
for 200 and 800 too. Limiting the weight axis to 400..600 keeps every weight and optical size the
site can show exactly as before (same outlines, advance widths and vertical metrics) and cuts the
latin file from 132 KB to about 92 KB. The optical-size axis is kept whole: clamping it would
tighten the spacing of the 15 to 17 px serif text.

Run from the repository root after replacing the files with fresh copies from Google Fonts:
    python scripts/trim_fonts.py
Needs fonttools and brotli (pip install fonttools brotli); a development step, not part of the
pipeline or the site build. Running it again on trimmed files changes nothing.
"""
from __future__ import annotations

import io
from pathlib import Path

from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

FONTS = Path(__file__).resolve().parents[1] / "site" / "public" / "fonts"
# File name prefix -> axis limits; the weight range matches the @font-face font-weight in global.css.
LIMITS = {
    "newsreader-normal-": {"wght": (400, 600)},
}


def trim(path: Path, limits: dict) -> tuple[int, int]:
    before = path.stat().st_size
    font = TTFont(path)
    axes = {a.axisTag: (a.minValue, a.maxValue) for a in font["fvar"].axes}
    if all(axes.get(tag) == (float(lo), float(hi)) for tag, (lo, hi) in limits.items()):
        return before, before
    out = instancer.instantiateVariableFont(font, limits, updateFontNames=False)
    out.flavor = "woff2"
    buf = io.BytesIO()
    out.save(buf)
    path.write_bytes(buf.getvalue())
    return before, path.stat().st_size


def main() -> None:
    for prefix, limits in LIMITS.items():
        for path in sorted(FONTS.glob(f"{prefix}*.woff2")):
            before, after = trim(path, limits)
            print(f"{path.name}: {before:,} -> {after:,} bytes")


if __name__ == "__main__":
    main()
