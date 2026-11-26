/* Writing other people's words into our pages safely. Headlines, summaries and the publishers' text
   come from feeds, web pages and free AI models, so none of it is trusted: one bad string on any page
   could run a script on digestai.news, where /admin keeps Martin's GitHub token in the browser.

   jsonForScript: JSON for a <script> element (JSON-LD, window.DIGEST). JSON.stringify leaves "<" as
   it is, so a headline holding "</script>" would end the element and the rest would run as HTML.
   markdownToHtml: Markdown to HTML with raw HTML shown as text (as before) and only web, mail and
   on-site addresses kept in links and images: a "javascript:" link or a "data:" picture is dropped,
   its words kept. Plain Node (no Astro), so the tests can run it. */
import { Marked } from "marked";

export function jsonForScript(value) {
  return JSON.stringify(value)
    .replace(/</g, "\\u003c")
    .replace(/>/g, "\\u003e")
    .replace(/&/g, "\\u0026")
    .replace(/\u2028/g, "\\u2028")
    .replace(/\u2029/g, "\\u2029");
}

/* Browsers ignore spaces and control characters inside an address and read entities (&#58;) and,
   in some places, percent escapes before the scheme, so those are cleaned or refused first. An
   address with no scheme (/story/x, #top, ?q=1, page.html) is on this site. */
export function safeUrl(href, schemes = ["http", "https", "mailto"]) {
  const s = String(href ?? "").replace(/[\x00-\x20\x7f-\x9f]/g, "");
  if (!s) return false;
  const head = s.split(/[/?#]/, 1)[0];
  if (!/[:&%]/.test(head)) return true;
  const m = /^([a-z][a-z0-9+.-]*):/i.exec(s);
  return Boolean(m) && schemes.includes(m[1].toLowerCase());
}

function escapeHtmlInMarkdown(md) {
  return md.replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

const escapeText = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const md = new Marked({ async: false, gfm: true, breaks: false });
md.use({
  renderer: {
    link(token) {
      // false: marked's own rendering of the link.
      return safeUrl(token.href) ? false : this.parser.parseInline(token.tokens);
    },
    image(token) {
      return safeUrl(token.href, ["http", "https"]) ? false : escapeText(token.text);
    },
  },
});

export function markdownToHtml(source) {
  if (!source) return "";
  return md.parse(escapeHtmlInMarkdown(String(source)));
}
