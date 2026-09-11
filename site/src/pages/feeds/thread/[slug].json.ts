import type { APIRoute } from "astro";
import { threads, threadStories, feedItem } from "../../../lib/data";

export function getStaticPaths() {
  return threads.map((thread) => ({ params: { slug: thread.slug }, props: { thread } }));
}

export const GET: APIRoute = ({ props }) => {
  const items = threadStories(props.thread).reverse().slice(0, 12).map(feedItem);
  return new Response(JSON.stringify(items), { headers: { "Content-Type": "application/json; charset=utf-8" } });
};
