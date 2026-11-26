/* Related reading (data.ts related(), used by every story page and StoryNext).

   Each other story scores three per shared name, one for the same category, less the days between
   the two (up to 30) over ten; the best scores above zero come first. Only a story that shares a name
   or the category can score above zero, so each story's names, time and category are indexed once and
   a page scores just those candidates. Scoring all 7,000 stories on each of the 7,000 story pages
   (twice: the page and StoryNext) was most of the site build, 205 of its 263 seconds on 26 Nov 2026.
   The answers are the ones the full scan gave, in the same order: candidates are scored in the stories'
   own order and the sort is stable (tests/test_site_related.py compares the two). */

const namesOf = (s) => Object.values(s.entities || {}).flat().map((n) => n.toLowerCase());

/** The full scan, kept as the reference the index must agree with. */
export function relatedByScan(stories, story, limit = 5) {
  const names = new Set(namesOf(story));
  const scored = stories
    .filter((s) => s.id !== story.id)
    .map((s) => {
      const shared = namesOf(s).filter((n) => names.has(n)).length;
      const sameCat = s.category === story.category ? 1 : 0;
      const age = Math.abs(Date.parse(s.updatedAt || "0") - Date.parse(story.updatedAt || "0")) / 864e5;
      return { s, w: shared * 3 + sameCat - Math.min(age, 30) / 10 };
    })
    .filter((x) => x.w > 0)
    .sort((a, b) => b.w - a.w);
  return scored.slice(0, limit).map((x) => x.s);
}

/** related(story, limit) over `stories`, indexed on first use. */
export function relatedFinder(stories) {
  let index = null;
  const build = () => {
    const names = [];
    const time = [];
    const byName = new Map();
    const byCategory = new Map();
    stories.forEach((s, i) => {
      const own = namesOf(s);
      names.push(own); // repeats kept: a name listed twice counts twice, as in the scan
      time.push(Date.parse(s.updatedAt || "0"));
      for (const n of new Set(own)) {
        if (!byName.has(n)) byName.set(n, []);
        byName.get(n).push(i);
      }
      if (!byCategory.has(s.category)) byCategory.set(s.category, []);
      byCategory.get(s.category).push(i);
    });
    return { names, time, byName, byCategory };
  };
  return (story, limit = 5) => {
    index ??= build();
    const names = new Set(namesOf(story));
    const at = Date.parse(story.updatedAt || "0");
    const candidates = new Set(index.byCategory.get(story.category) || []);
    for (const n of names) for (const i of index.byName.get(n) || []) candidates.add(i);
    const scored = [];
    for (const i of [...candidates].sort((a, b) => a - b)) {
      const s = stories[i];
      if (s.id === story.id) continue;
      let shared = 0;
      for (const n of index.names[i]) if (names.has(n)) shared += 1;
      const sameCat = s.category === story.category ? 1 : 0;
      const age = Math.abs(index.time[i] - at) / 864e5;
      const w = shared * 3 + sameCat - Math.min(age, 30) / 10;
      if (w > 0) scored.push({ s, w });
    }
    scored.sort((a, b) => b.w - a.w);
    return scored.slice(0, limit).map((x) => x.s);
  };
}
