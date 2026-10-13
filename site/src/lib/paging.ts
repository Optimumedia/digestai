/* Long lists in pages of LIST_PAGE_SIZE rows (the rule and the reason are in indexing.mjs).
   Page 1 is the hub itself (/topic/openai); page n is /topic/openai/<n>, built by the [page].astro
   file next to the hub's template and always "noindex, follow". */
import { LIST_PAGE_SIZE, listPageCount } from "./indexing.mjs";

export { LIST_PAGE_SIZE, listPageCount };

export interface PageSlice<T> {
  items: T[];
  /** This page's number, from 1. */
  page: number;
  /** How many pages the whole list takes. */
  pages: number;
  /** Rows in the whole list. */
  total: number;
  /** Position of this page's first and last row in the whole list, from 1 (0 and 0 when empty). */
  from: number;
  to: number;
}

/** The rows of one page of a list. */
export function pageSlice<T>(list: T[], page = 1, size = LIST_PAGE_SIZE): PageSlice<T> {
  const pages = listPageCount(list.length, size);
  const start = (page - 1) * size;
  const items = list.slice(start, start + size);
  return { items, page, pages, total: list.length, from: items.length ? start + 1 : 0, to: start + items.length };
}

/** The numbers of the older pages a list needs: [2, 3, …], empty when it fits on one page. */
export function olderPages(total: number, size = LIST_PAGE_SIZE): number[] {
  return Array.from({ length: listPageCount(total, size) - 1 }, (_, i) => i + 2);
}

/** Where page n of a list lives: the hub itself for page 1, /<hub>/<n> after that. */
export function pageHref(base: string, n: number): string {
  return n <= 1 ? base : `${base}/${n}`;
}

/** The page numbers a pager shows: all of them up to sixteen pages (so every older page is one link
    from the hub), otherwise the first two, the last two and the ones around the current page, with
    null where numbers are skipped. */
export function pagerNumbers(page: number, pages: number): (number | null)[] {
  if (pages <= 16) return Array.from({ length: pages }, (_, i) => i + 1);
  const keep = new Set([1, 2, pages - 1, pages, page - 1, page, page + 1].filter((n) => n >= 1 && n <= pages));
  const out: (number | null)[] = [];
  let last = 0;
  for (const n of [...keep].sort((a, b) => a - b)) {
    if (n - last > 1) out.push(null);
    out.push(n);
    last = n;
  }
  return out;
}
