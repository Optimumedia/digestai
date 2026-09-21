/* Category pages (/category/<slug>): the words and the week's picks that make each one a page worth
   indexing, not only a list. Built by rules from the exported JSON at build time: no database reads
   and no model calls. The introductions are fixed text written once per category; the "this week"
   paragraph uses only the stories' own headlines, key points and why-it-matters, so it can say
   nothing the stories do not. */
import { meta, storiesInCategory, hubHref, formatDate, storyConfirmed, noindex, type Story } from "./data";
import { registrable } from "./indexing.mjs";

export interface CategoryCopy {
  /** The page's H1. */
  h1: string;
  /** How the category reads in "<x> AI news this week" (the page title). */
  short: string;
  /** One line for the meta description. */
  blurb: string;
  /** 60 to 120 words: what the category covers and who it is for. */
  intro: string;
}

export const CATEGORY_COPY: Record<string, CategoryCopy> = {
  models: {
    h1: "AI model news",
    short: "AI model",
    blurb: "New models, releases and capabilities from the frontier labs and the open-source community.",
    intro:
      "New AI models land almost every week, and most launches come with claims that are hard to check. This section follows the releases that matter from OpenAI, Anthropic, Google DeepMind, Meta, Mistral, DeepSeek and the open-source community: what each model can do, how it is priced and licensed, how it scores on public benchmarks and what changed from the version before. It is written for developers choosing a model, product teams planning around new capabilities and anyone who wants to know which launches are real progress. Every story links to the lab's own announcement where there is one, and to the independent coverage that tested it.",
  },
  agents: {
    h1: "AI agents and developer tools news",
    short: "Agents & tools",
    blurb: "Agents, developer tools, coding assistants and the software being built on top of models.",
    intro:
      "AI agents are software that can plan and carry out tasks on their own: writing and running code, browsing the web, filling in forms and calling other tools. This section covers the agent platforms, coding assistants, developer tools and APIs being built on top of large language models, and how well they work outside a demo. It is for developers, engineering leads and technical founders deciding what to adopt, and for anyone curious where autonomous software is heading. Stories note what each tool costs, which models it runs on, what it is allowed to touch and where the reports disagree about how reliable it is.",
  },
  research: {
    h1: "AI research news",
    short: "AI research",
    blurb: "Papers, benchmarks and findings that change what we know AI systems can do.",
    intro:
      "The research section covers papers, benchmarks and findings that change what we know AI systems can do, and what they still cannot. It follows work from university labs and from the research teams at OpenAI, Google DeepMind, Anthropic, Meta and others: new training methods, evaluations, interpretability, safety results and surprising failures. It is for researchers, engineers and technically curious readers who want the substance of a paper without reading all of it. Each story explains the result in plain words, links to the paper or the lab's write-up, and says why it matters, including when a headline claim is stronger than the evidence behind it.",
  },
  business: {
    h1: "AI business and funding news",
    short: "Business & funding",
    blurb: "Funding rounds, valuations, deals and the economics of the AI industry.",
    intro:
      "Business & Funding tracks the money in artificial intelligence: funding rounds, valuations, acquisitions, partnerships, revenue figures and the deals between labs, cloud providers and chip makers. It is for founders, investors, analysts and operators who need to know who is raising, who is buying whom and what the numbers say about where the industry is going. Stories name the amounts and investors as reported, link to the company's announcement or filing where one exists, and say when a figure comes from a single unnamed source. Funding rounds also feed the funding tracker, which lists every deal we have covered.",
  },
  policy: {
    h1: "AI policy and regulation news",
    short: "Policy & regulation",
    blurb: "Regulation, safety, lawsuits and how governments respond to AI.",
    intro:
      "Policy & Regulation covers how governments, courts and regulators respond to artificial intelligence: the EU AI Act, US federal and state rules, the UK and China, copyright and privacy lawsuits, export controls on chips, and the safety commitments AI labs make and break. It is for policy professionals, lawyers, compliance teams and anyone who needs to know what is now allowed, required or contested. Stories say who decided what, when it takes effect and who it applies to, and link to the bill, ruling or official statement wherever one is published, alongside the reporting around it.",
  },
  hardware: {
    h1: "AI hardware and compute news",
    short: "Hardware & compute",
    blurb: "Chips, data centres, power and the compute race.",
    intro:
      "Hardware & Compute follows the physical side of AI: GPUs and custom chips from Nvidia, AMD, Intel, Google, Amazon and the start-ups, the data centres being built to run them, and the power, water and supply chains they depend on. It is for engineers, infrastructure buyers, investors and energy watchers who need to understand the compute race behind every model launch. Stories cover chip announcements, export rules, capacity deals, data-centre sites and prices, with the numbers as reported and links to the companies' own figures where they publish them.",
  },
  enterprise: {
    h1: "Enterprise AI news",
    short: "Enterprise",
    blurb: "How companies are actually deploying AI, and what it costs them.",
    intro:
      "Enterprise & Industry is about how companies actually put AI to work, beyond the pilot: which banks, retailers, manufacturers, hospitals and governments are deploying it, for what, at what cost and with what results. It covers enterprise products from the big platforms, adoption surveys, changes to jobs and workflows inside firms, and the deployments that went wrong. It is for executives, IT and operations leaders, consultants and buyers comparing vendors. Stories name the company and the use, give the figures reported, and separate what a business says it will do from what it has already shipped.",
  },
  robotics: {
    h1: "Robotics and physical AI news",
    short: "Robotics",
    blurb: "Humanoids, autonomy and AI in the physical world.",
    intro:
      "Robotics & Physical AI covers AI that moves in the real world: humanoid robots, warehouse and factory automation, self-driving cars and trucks, drones, and the foundation models being trained to control them. It follows companies such as Tesla, Figure, Boston Dynamics, Waymo and Nvidia, and the research labs behind new robot skills. It is for engineers, operators, investors and readers who want to know which machines are working in real deployments and which are still demos. Stories give deployment numbers, safety records and prices as reported, and link to the original sources.",
  },
  society: {
    h1: "AI, society and work news",
    short: "Society & work",
    blurb: "Jobs, education, culture and daily life in an AI-shaped world.",
    intro:
      "Society & Work looks at what AI is doing to jobs, education, culture, health and daily life. It covers layoffs and new roles put down to AI, how schools and universities are adapting, creative industries and copyright, misinformation and deepfakes, mental health, and the ways people use chatbots every day. It is for workers, managers, teachers, parents and anyone trying to separate real effects from predictions. Stories report surveys and studies with their sample and source, give the view of the people affected where the reporting has it, and link to the original research.",
  },
  marketing: {
    h1: "AI for marketing and small business",
    short: "Marketing & small business",
    blurb: "Practical AI for marketers and small businesses: tools, features and how-tos for content, ads, search, social, email and sales you can use this week.",
    intro:
      "Marketing & Small Business is practical AI news for marketers, small-business owners and small teams: the new features and tools for content, ads, search, social media, email, customer service and sales, and what they mean for your work this week. It covers Google, Meta, Microsoft, OpenAI, Canva, HubSpot, Shopify and the smaller tools built for people without a technical team. Stories say what changed, who it is for, what it costs and how to try it, and the AI at Work section turns the most useful ones into step-by-step playbooks.",
  },
};

export function categoryCopy(slug: string, name: string): CategoryCopy {
  return CATEGORY_COPY[slug] || { h1: `${name} AI news`, short: name, blurb: `${name}: the AI stories that matter, with their sources.`, intro: "" };
}

/** "<category> AI news this week", without saying AI twice ("AI research news this week"). */
export function newsLabel(copy: CategoryCopy): string {
  return /\bAI\b/.test(copy.short) ? `${copy.short} news` : `${copy.short} AI news`;
}

export const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

const WEEK_MS = 7 * 864e5;
/** The build's "now": the export time, so a page says the same thing however late it is built. */
const now = Date.parse(meta.generatedAt) || Date.now();

/** Most important first: importance, then confirmed before single-outlet, then the ranking score. */
export function byImportance(a: Story, b: Story): number {
  return (b.importance ?? 0) - (a.importance ?? 0) || Number(storyConfirmed(b)) - Number(storyConfirmed(a)) || (b.score ?? 0) - (a.score ?? 0);
}

/** A category's stories first published in the seven days to the export, most important first. */
export function categoryWeek(slug: string): Story[] {
  return storiesInCategory(slug)
    .filter((s) => {
      const t = Date.parse(s.firstPublishedAt || "");
      return !Number.isNaN(t) && t > now - WEEK_MS && t <= now + 36e5;
    })
    .sort(byImportance);
}

/** A sentence ends with a full stop (key points and why-it-matters usually do already). */
export function sentence(text: string | null | undefined): string {
  const t = (text || "").replace(/\s+/g, " ").trim();
  if (!t) return "";
  return /[.!?…]["”’)]?$/.test(t) ? t : `${t}.`;
}

export interface WeekSummary {
  count: number;
  publishers: number;
  until: string;
  lead: Story;
  leadPoint: string;
  leadWhy: string;
  also: Story[];
}

/** The parts of the "This week in <category>" paragraph: counts, the week's biggest story with its
    first key point and why it matters, and up to three more headlines. Null for an empty week. */
export function weekSummary(week: Story[]): WeekSummary | null {
  if (!week.length) return null;
  const lead = week[0];
  const publishers = new Set(week.flatMap((s) => s.articles.map((a) => registrable(a.domain || "")))).size;
  const leadPoint = sentence(lead.keyPoints?.[0]);
  const leadWhy = sentence(lead.whyItMatters);
  return {
    count: week.length,
    publishers,
    until: formatDate(meta.generatedAt, { day: "numeric", month: "long" }),
    lead,
    leadPoint,
    leadWhy: leadWhy && leadWhy !== leadPoint ? leadWhy : "",
    also: week.slice(1, 4),
  };
}

/** The names most mentioned across these stories (companies, models, people), with where each links:
    its model page or topic hub, or nowhere when it has neither. Names with a page come first. */
export function keyTopics(list: Story[], limit = 8): { name: string; count: number; href: string | null }[] {
  const counts = new Map<string, { name: string; count: number }>();
  for (const s of list) {
    const names = new Set(Object.values(s.entities || {}).flat());
    for (const n of names) {
      const key = n.toLowerCase();
      const row = counts.get(key) || { name: n, count: 0 };
      row.count += 1;
      counts.set(key, row);
    }
  }
  return [...counts.values()]
    .map((r) => {
      // Only a page worth indexing is linked: a thin topic hub (under three stories) is noindex.
      const href = hubHref(r.name);
      return { ...r, href: href && !noindex.has(href) ? href : null };
    })
    // A name with no page of its own is only worth showing when several stories mention it.
    .filter((r) => r.href || r.count >= 2)
    .sort((a, b) => Number(Boolean(b.href)) - Number(Boolean(a.href)) || b.count - a.count || a.name.localeCompare(b.name))
    .slice(0, limit);
}

/** A headline cut at a word to fit a page title. */
export function clip(text: string, max: number): string {
  if (text.length <= max) return text;
  const cut = text.slice(0, max - 1);
  return `${cut.slice(0, Math.max(cut.lastIndexOf(" "), max * 0.6)).replace(/[\s,;:–—-]+$/, "")}…`;
}
