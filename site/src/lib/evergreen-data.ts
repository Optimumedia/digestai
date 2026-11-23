/* The evergreen pages' data for this build (lib/evergreen.mjs holds the rules): built once from the
   exported stories, the model tracker and the AI at Work tool directory, then shared by every page. */
import { stories, trackers } from "./data";
import { work } from "./work";
import { buildEvergreen } from "./evergreen.mjs";

export const evergreen = buildEvergreen({ stories, models: trackers.models, tools: work.tools });

/** Whether a path built by these pages is worth indexing (the same rule the sitemap uses). */
export const evergreenIndexable = (path: string): boolean => Boolean(evergreen.pages.get(path)?.indexable);

/** The comparisons a model appears in, for a link line on its page. */
export const comparisonsFor = (slug: string) =>
  evergreen.pairs.filter((p: any) => p.kind === "model" && (p.a.slug === slug || p.b.slug === slug));
