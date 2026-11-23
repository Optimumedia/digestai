import type { APIRoute } from "astro";
import { json } from "../../lib/api";
import { modelsDoc } from "../../lib/machine";

// /api/models.json: the newest model from each lab, the months, and every launch the answers state.
export const GET: APIRoute = () => json(modelsDoc());
