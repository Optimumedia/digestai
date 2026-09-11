import { defineConfig } from "astro/config";
import sitemap from "@astrojs/sitemap";

export default defineConfig({
  site: process.env.SITE_URL || "https://digestai.news",
  output: "static",
  trailingSlash: "never",
  build: { format: "file" },
  integrations: [
    sitemap({
      filter: (page) => !page.includes("/search"),
    }),
  ],
});
