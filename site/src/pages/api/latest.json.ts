import type { APIRoute } from "astro";
import { envelope, latestStories, storyItem, json } from "../../lib/api";

// The newest stories by first publication, light rows with a link to each story's JSON.
export const GET: APIRoute = () => json(envelope({ stories: latestStories(100).map(storyItem) }));
