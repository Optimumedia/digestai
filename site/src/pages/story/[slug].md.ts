import type { APIRoute } from "astro";
import { storyDocs, storyMarkdown } from "../../lib/api";

// /story/<slug>.md: the same story as plain Markdown, which many assistants fetch and quote best.
export function getStaticPaths() {
  return storyDocs().map(({ slug, doc }) => ({ params: { slug }, props: { doc } }));
}

export const GET: APIRoute = ({ props }) =>
  new Response(storyMarkdown(props.doc), { headers: { "Content-Type": "text/markdown; charset=utf-8" } });
