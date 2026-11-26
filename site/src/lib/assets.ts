/* Version tags for the hand-written files in public/ that pages link with ?v= (layouts/Base.astro).
   The tag is a hash of the file's own content, so it changes only when the file does: a returning
   reader's browser keeps its copy of app.js across the site's deploys (one every run) instead of
   downloading it again after each one, as it did when the tag was the build time. */
import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";

export function fileVersion(file: string, fallback: string): string {
  try {
    return crypto.createHash("sha256").update(fs.readFileSync(path.resolve("public", file))).digest("hex").slice(0, 10);
  } catch {
    return fallback;
  }
}

/** Built once per build: every page asks for the same app.js?v=<hash>. */
export const appJsVersion: string = fileVersion("app.js", String(Date.now()));
