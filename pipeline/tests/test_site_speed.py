"""Page weight, caching and accessibility rules of the site: python tests/test_site_speed.py

What a phone downloads on every page (the serif font, app.js), how the service worker stores it, and
the colour and keyboard rules the templates must keep. Runs without a built site; the Node parts skip
when node (or, for the minify comparison, esbuild from site/node_modules) is not installed."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SITE = Path(__file__).resolve().parents[2] / "site"
TESTS = Path(__file__).resolve().parent
CSS = SITE / "src" / "styles" / "global.css"
BASE = SITE / "src" / "layouts" / "Base.astro"


def _node():
    return shutil.which("node")


def test_newsreader_files_are_trimmed_to_the_declared_weights():
    # Every page preloads the latin file; the full Google Fonts file (200 to 800) was 132 KB.
    latin = (SITE / "public" / "fonts" / "newsreader-normal-latin.woff2").stat().st_size
    ext = (SITE / "public" / "fonts" / "newsreader-normal-latin-ext.woff2").stat().st_size
    assert latin < 100_000, f"newsreader latin is {latin} bytes: run scripts/trim_fonts.py"
    assert ext < 70_000, f"newsreader latin-ext is {ext} bytes: run scripts/trim_fonts.py"
    css = CSS.read_text(encoding="utf-8")
    faces = re.findall(r'@font-face \{ font-family: "Newsreader";[^}]*\}', css)
    assert len(faces) == 2 and all("font-weight: 400 600;" in f for f in faces), faces
    try:
        from fontTools.ttLib import TTFont  # a development tool, not a pipeline requirement
        import brotli  # noqa: F401
    except ImportError:
        print("SKIP axis check: fonttools or brotli not installed")
        return
    for name in ("newsreader-normal-latin.woff2", "newsreader-normal-latin-ext.woff2"):
        axes = {a.axisTag: (a.minValue, a.maxValue) for a in TTFont(SITE / "public" / "fonts" / name)["fvar"].axes}
        assert axes["wght"] == (400, 600), (name, axes)
        assert axes["opsz"] == (6, 72), (name, axes)  # optical sizes kept whole: same spacing at 15 to 17 px


def test_build_minifies_the_public_scripts():
    build = json.loads((SITE / "package.json").read_text(encoding="utf-8"))["scripts"]["build"]
    steps = [s.strip() for s in build.split("&&")]
    assert steps[0] == "astro build" and steps[-1].startswith("pagefind"), steps
    assert "node scripts/minify-public.mjs dist" in steps, steps


def test_minified_app_js_behaves_like_the_source():
    node = _node()
    if not node:
        print("SKIP node not installed")
        return
    if not (SITE / "node_modules" / "esbuild").exists():
        print("SKIP esbuild not installed (site/node_modules)")
        return
    dist = Path(tempfile.mkdtemp())
    for name in ("app.js", "sw.js"):
        shutil.copy(SITE / "public" / name, dist / name)
    res = subprocess.run([node, str(SITE / "scripts" / "minify-public.mjs"), str(dist)], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr[:500]
    for name in ("app.js", "sw.js"):
        before, after = (SITE / "public" / name).stat().st_size, (dist / name).stat().st_size
        assert after < before * 0.7, (name, before, after)
    # The reader-gate probe (test_units.py) gives the same answer for the minified file as for the source.
    probe = str(TESTS / "app_gate_probe.js")
    src = subprocess.run([node, probe, str(SITE / "public" / "app.js")], capture_output=True, text=True, timeout=60)
    mini = subprocess.run([node, probe, str(dist / "app.js")], capture_output=True, text=True, timeout=60)
    assert src.returncode == 0 and mini.returncode == 0, (src.stderr[:300], mini.stderr[:300])
    assert json.loads(src.stdout) == json.loads(mini.stdout)
    sw_src = subprocess.run([node, str(TESTS / "sw_probe.js"), str(SITE / "public" / "sw.js")], capture_output=True, text=True, timeout=60)
    sw_min = subprocess.run([node, str(TESTS / "sw_probe.js"), str(dist / "sw.js")], capture_output=True, text=True, timeout=60)
    assert sw_src.returncode == 0 and sw_min.returncode == 0, (sw_src.stderr[:300], sw_min.stderr[:300])
    assert json.loads(sw_src.stdout) == json.loads(sw_min.stdout)


def test_minify_without_esbuild_leaves_the_build_alone():
    node = _node()
    if not node:
        print("SKIP node not installed")
        return
    # A copy of the script outside site/ cannot resolve esbuild: the files stay as they are, exit 0.
    tmp = Path(tempfile.mkdtemp())
    script = tmp / "minify-public.mjs"
    shutil.copy(SITE / "scripts" / "minify-public.mjs", script)
    dist = tmp / "dist"
    dist.mkdir()
    (dist / "app.js").write_text("/* comment */\nconsole.log( 1 );\n", encoding="utf-8")
    res = subprocess.run([node, str(script), str(dist)], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr[:500]
    assert (dist / "app.js").read_text(encoding="utf-8") == "/* comment */\nconsole.log( 1 );\n"


def test_app_js_is_versioned_by_its_content():
    # A build-time tag made every deploy (one per run) send returning readers a fresh copy.
    base = BASE.read_text(encoding="utf-8")
    tag = re.search(r'<script src=\{`/app\.js\?v=\$\{(\w+)\}`\}', base)
    assert tag and tag.group(1) == "appJsVersion", tag
    assets = (SITE / "src" / "lib" / "assets.ts").read_text(encoding="utf-8")
    assert 'fileVersion("app.js"' in assets and "createHash" in assets


def test_service_worker_keeps_its_caches_bounded():
    node = _node()
    if not node:
        print("SKIP node not installed")
        return
    res = subprocess.run([node, str(TESTS / "sw_probe.js"), str(SITE / "public" / "sw.js")], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr[:500]
    out = json.loads(res.stdout)
    # 130 pages read: the latest 100 are kept, with the offline page, home and today always there.
    assert out["pages"] == {"count": 100, "hasOffline": True, "hasPinned": True, "hasNewest": True, "hasOldest": False}, out
    # app.js with its version tag is served from the cache the second time; without one it is not touched.
    assert out["app"] == {"handled": True, "networkFetches": 1, "bareHandled": False}, out
    assert out["assets"] == {"count": 40, "hasNewest": True}, out
    assert out["foreignHandled"] is False


def _tokens(block: str) -> dict:
    return dict(re.findall(r"(--[\w-]+):\s*(#[0-9a-fA-F]{6})", block))


def _contrast(a: str, b: str) -> float:
    def lum(h):
        c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        c = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    x, y = lum(a), lum(b)
    return (max(x, y) + 0.05) / (min(x, y) + 0.05)


def test_text_colours_meet_contrast_in_both_themes():
    css = CSS.read_text(encoding="utf-8")
    light = _tokens(re.search(r":root \{(.*?)\n\}", css, re.S).group(1))
    dark = _tokens(re.search(r':root\[data-theme="dark"\] \{(.*?)\n\}', css, re.S).group(1))
    os_dark = _tokens(re.search(r'@media \(prefers-color-scheme: dark\) \{\s*:root:not\(\[data-theme="light"\]\) \{(.*?)\}', css, re.S).group(1))
    assert dark == os_dark, "the two dark-mode blocks must match"
    for name, t in (("light", light), ("dark", dark)):
        theme = {**light, **t}
        # Muted text (times, source counts, labels) at 11 to 13 px: 4.5:1 on every ground it sits on,
        # the briefing's accent-soft panel included.
        for ground in ("--bg", "--surface", "--surface-2", "--accent-soft"):
            ratio = _contrast(theme["--ink-3"], theme[ground])
            assert ratio >= 4.5, (name, "--ink-3", ground, round(ratio, 2))
        pairs = [("--hot", "--hot-soft"),      # hot chips and tags
                 ("--bg", "--hot"),            # the ticker's Live label and the saved count
                 ("--accent-ink", "--accent"), # filled buttons, the consent banner's Accept
                 ("--ink-2", "--bg"), ("--accent", "--bg")]
        for fg, bg in pairs:
            ratio = _contrast(theme[fg], theme[bg])
            assert ratio >= 4.5, (name, fg, bg, round(ratio, 2))
    # The chips that sit on --hot and --accent use the theme's own inks, never a fixed white.
    for rule in (r"\.ticker \.live \{[^}]*\}", r"\.icon-btn \.count \{[^}]*\}", r"\.consent-btn\.primary \{[^}]*\}"):
        body = re.search(rule, css).group(0)
        assert "color: #fff" not in body, body


def test_keyboard_can_skip_the_masthead_and_scroll_wide_tables():
    base = BASE.read_text(encoding="utf-8")
    body = base[base.index("<body"):]
    assert body.index('class="skip-link" href="#main"') < body.index('<header class="masthead"'), "skip link first"
    assert re.search(r'<main class="wrap" id="main"', base)
    css = CSS.read_text(encoding="utf-8")
    assert re.search(r"\.skip-link:focus \{[^}]*top: 8px", css)
    app = (SITE / "public" / "app.js").read_text(encoding="utf-8")
    # A .tablewrap that scrolls sideways takes focus with a name; the theme button reports its state.
    assert 'querySelectorAll(".tablewrap")' in app and 'setAttribute("role", "region")' in app
    assert 'setAttribute("aria-pressed"' in app


def test_home_page_opens_the_picture_hosts_early():
    home = (SITE / "src" / "pages" / "index.astro").read_text(encoding="utf-8")
    assert 'rel="preconnect" href="https://github.com"' in home
    assert 'rel="preconnect" href="https://release-assets.githubusercontent.com"' in home


if __name__ == "__main__":
    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print("FAIL", name, type(exc).__name__, str(exc)[:300])
    sys.exit(1 if failures else 0)
