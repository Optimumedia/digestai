import type { APIRoute } from "astro";
import { topicPages, storiesForEntity, feedItem } from "../../../lib/data";

export function getStaticPaths() {
  return topicPages().map(({ slug, entity }) => ({ params: { slug }, props: { entity } }));
}

export const GET: APIRoute = ({ props }) => {
  const items = storiesForEntity(props.entity).slice(0, 12).map(feedItem);
  return new Response(JSON.stringify(items), { headers: { "Content-Type": "application/json; charset=utf-8" } });
};
