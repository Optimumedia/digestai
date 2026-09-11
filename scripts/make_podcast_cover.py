"""Render the 1400x1400 podcast artwork (Apple requires 1400-3000 px square) into site/public.
Run once from the repo root; the output is committed."""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

root = Path(__file__).resolve().parent.parent
fonts = root / "pipeline" / "assets" / "fonts"
out = root / "site" / "public" / "podcast-cover.png"


def font(name, size, weight=None):
    f = ImageFont.truetype(str(fonts / name), size)
    if weight is not None:
        try:
            f.set_variation_by_axes([weight])
        except Exception:  # noqa: BLE001
            pass
    return f


S = 1400
img = Image.new("RGB", (S, S), "#0e1116")
d = ImageDraw.Draw(img)
# accent bar
d.rectangle([0, 0, S, 28], fill="#e0961b")
# wordmark
d.text((110, 300), "Digest", font=font("Newsreader.ttf", 250, 600), fill="#f3f4f6")
d.text((110, 560), "AI", font=font("Newsreader.ttf", 250, 600), fill="#e0961b")
d.text((116, 860), "THE DAILY BRIEFING", font=font("JetBrainsMono.ttf", 54), fill="#9aa3b2")
d.text((116, 950), "Five AI stories that matter,\nread in five minutes.", font=font("SourceSans3.ttf", 66, 500), fill="#c9cfd8", spacing=14)
d.text((116, 1230), "digestai.news", font=font("JetBrainsMono.ttf", 48), fill="#e0961b")
img.save(out, optimize=True)
print("wrote", out, out.stat().st_size // 1024, "KB")
