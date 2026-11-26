import type { APIRoute } from "astro";
import { json } from "../../lib/api";
import { fundingDoc } from "../../lib/machine";

// /api/funding.json: every deal the funding tracker has listed, with the figures as the sources state them.
export const GET: APIRoute = () => json(fundingDoc());
