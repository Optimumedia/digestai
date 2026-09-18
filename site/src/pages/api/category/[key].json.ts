import type { APIRoute } from "astro";
import { categoryDoc, json } from "../../../lib/api";
import { categories } from "../../../lib/data";

// The newest 50 stories in one category.
export function getStaticPaths() {
  return Object.keys(categories).map((key) => ({ params: { key } }));
}

export const GET: APIRoute = ({ params }) => json(categoryDoc(params.key!));
