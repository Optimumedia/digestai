import type { APIRoute } from "astro";
import { briefingDoc, json } from "../../lib/api";
import { briefing } from "../../lib/data";

// Today's briefing (/today): the five stories that matter and the also-list.
export const GET: APIRoute = () => json(briefingDoc(briefing.date));
