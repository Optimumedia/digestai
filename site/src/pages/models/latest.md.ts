import type { APIRoute } from "astro";
import { modelsMarkdown, text } from "../../lib/machine";

// /models/latest.md: the newest model from each lab as Markdown.
export const GET: APIRoute = () => text(modelsMarkdown(), "text/markdown");
