import type { APIRoute } from "astro";
import { todayMarkdown, text } from "../lib/machine";

// /today.md: today's briefing as Markdown, dated, for assistants that read plain text best.
export const GET: APIRoute = () => text(todayMarkdown(), "text/markdown");
