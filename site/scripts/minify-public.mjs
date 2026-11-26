/* Minifies the hand-written scripts in the built site (dist/app.js and dist/sw.js) after
   `astro build`. Astro copies public/ as it is, comments and all: app.js is 55 KB of source that
   every page loads, 29 KB once minified (about 10.6 KB instead of 17.7 KB gzipped on the wire).
   public/ keeps the readable source, which the tests run (pipeline/tests/app_gate_probe.js).

   esbuild comes with Astro (through Vite). Only whitespace, comments and local names change; the
   syntax is left as written (no target), so the browsers that ran the source run the result. If
   esbuild cannot be loaded or a file does not parse, that file is left as it was and the build goes
   on: a larger script is better than no site.

   Usage: node scripts/minify-public.mjs [distDir]   (default: dist) */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const FILES = ["app.js", "sw.js"];

export async function minifyPublic(distDir = "dist", files = FILES) {
  let esbuild;
  try {
    esbuild = await import("esbuild");
  } catch {
    console.warn("minify-public: esbuild not available, scripts left unminified");
    return [];
  }
  const done = [];
  for (const name of files) {
    const file = path.join(distDir, name);
    let src;
    try {
      src = fs.readFileSync(file, "utf8");
    } catch {
      continue;
    }
    try {
      const out = await esbuild.transform(src, { minify: true, loader: "js", legalComments: "none" });
      if (out.code.length < src.length) {
        fs.writeFileSync(file, out.code);
        done.push({ name, before: src.length, after: out.code.length });
      }
    } catch (err) {
      console.warn(`minify-public: ${name} left as it was (${String(err && err.message || err).slice(0, 200)})`);
    }
  }
  for (const d of done) console.log(`minify-public: ${d.name} ${d.before} -> ${d.after} bytes`);
  return done;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  await minifyPublic(process.argv[2] || "dist");
}
