// The person responsible for Digest AI, in one place: the About page, the person page, the line on
// story pages and the structured data read it from here.
export const PUBLISHER = {
  name: "Martin K.",
  role: "Publisher",
  photo: "/team/martin-400.jpg",
  page: "/about/martin",
} as const;

/** schema.org Person for JSON-LD, by reference to the person page. */
export function publisherPerson(site: string) {
  return {
    "@type": "Person",
    "@id": `${site}${PUBLISHER.page}#person`,
    name: PUBLISHER.name,
    jobTitle: PUBLISHER.role,
    url: `${site}${PUBLISHER.page}`,
    image: `${site}${PUBLISHER.photo}`,
  };
}
