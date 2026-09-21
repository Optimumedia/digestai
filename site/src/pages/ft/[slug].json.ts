import type { APIRoute } from "astro";
import { stories, fullTextArticle, renderMarkdown } from "../../lib/data";

/* /ft/<slug>.json: the publisher's article a story page shows under "Full story from", as rendered
   HTML. It lives here instead of in the story page so search engines index only our own digest:
   robots.txt disallows /ft/, and Google does not index text a page fetches from a blocked address.
   public/app.js loads it into the page's [data-fulltext] box when the reader nears it.

   Not part of the public API (/api): the story JSON and Markdown never carry publishers' text. */
export function getStaticPaths() {
  return stories.flatMap((story) => {
    const full = fullTextArticle(story);
    return full ? [{ params: { slug: story.slug }, props: { html: renderMarkdown(full.contentMd), articleId: full.id } }] : [];
  });
}

export const GET: APIRoute = ({ props }) =>
  new Response(JSON.stringify({ articleId: props.articleId, html: props.html }), {
    headers: { "Content-Type": "application/json; charset=utf-8" },
  });
