import type { APIRoute } from "astro";
import { storyDocs, json } from "../../lib/api";

// /story/<slug>.json: the story as data, for assistants and apps that cite it (/api documents it).
export function getStaticPaths() {
  return storyDocs().map(({ slug, doc }) => ({ params: { slug }, props: { doc } }));
}

export const GET: APIRoute = ({ props }) => json(props.doc);
