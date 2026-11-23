import type { APIRoute } from "astro";
import { llmsFull, text } from "../lib/machine";

// /llms-full.txt: the long index for AI assistants, with today's answers in it (llms.txt is the short one).
export const GET: APIRoute = () => text(llmsFull());
