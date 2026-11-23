import type { APIRoute } from "astro";
import { stories, fullTextArticle, renderMarkdown, formatDate } from "../../lib/data";

/* /ft/<slug>.json: the publisher's article a story page shows under "Full story from", as rendered
   HTML, with its title, publisher, author, date and address for the heading and the attribution note
   around it. It lives here instead of in the story page so search engines index only our own digest:
   robots.txt disallows /ft/, and Google does not index text a page fetches from a blocked address.
   public/app.js loads it into the page's [data-fulltext] box when the reader nears it, and builds the
   heading and the note only then, so a page whose text did not load never talks about it.

   Not part of the public API (/api): the story JSON and Markdown never carry publishers' text. */
export function getStaticPaths() {
  return stories.flatMap((story) => {
    const full = fullTextArticle(story);
    return full
      ? [{
          params: { slug: story.slug },
          props: {
            doc: {
              articleId: full.id,
              html: renderMarkdown(full.contentMd),
              title: full.title,
              source: full.source || full.domain,
              author: full.author || null,
              date: formatDate(full.publishedAt),
              url: full.url,
            },
          },
        }]
      : [];
  });
}

export const GET: APIRoute = ({ props }) =>
  new Response(JSON.stringify(props.doc), {
    headers: { "Content-Type": "application/json; charset=utf-8" },
  });
