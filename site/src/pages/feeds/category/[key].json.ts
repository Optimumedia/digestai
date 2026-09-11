import type { APIRoute } from "astro";
import { categories, storiesInCategory, feedItem } from "../../../lib/data";

export function getStaticPaths() {
  return Object.keys(categories).map((key) => ({ params: { key } }));
}

export const GET: APIRoute = ({ params }) => {
  const items = storiesInCategory(params.key!).slice(0, 12).map(feedItem);
  return new Response(JSON.stringify(items), { headers: { "Content-Type": "application/json; charset=utf-8" } });
};
