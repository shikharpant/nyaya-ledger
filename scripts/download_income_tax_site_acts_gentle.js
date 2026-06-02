#!/usr/bin/env node
const fs = require("fs");
const path = require("path");

const OUTPUT_DIR = path.join("data", "Law", "base_laws");
const CATALOG_URL =
  "https://www.incometaxindia.gov.in/o/c/actassetcategories/?fields=nameOfAct,pageURL,assetCategoryID,id&filter=status/any(x:(x%20eq%200))%20and%20isInactive%20eq%20false&pageSize=-1&restrictFields=actions&sort=nameOfAct:asc";
const SEARCH_URL = "https://www.incometaxindia.gov.in/o/search/v1.0/search";
const YEAR_ID = 13590315;
const PAGE_SIZE = 100;
const MAX_PAGES_PER_ACT = Number(process.env.MAX_PAGES_PER_ACT || "100");
const FORCE_SLUGS = new Set(
  (process.env.FORCE_SLUGS || "")
    .split(",")
    .map((value) => value.trim())
    .filter(Boolean)
);
const PAGE_DELAY_MS = 6500;
const ACT_DELAY_MS = 9000;
const RETRY_DELAY_MS = 25000;
const MAX_ATTEMPTS = 2;

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function slugify(value) {
  return String(value || "")
    .toLowerCase()
    .replace(/&/g, " and ")
    .replace(/[^0-9a-z]+/g, "_")
    .replace(/_+/g, "_")
    .replace(/^_|_$/g, "");
}

function outputPathForAct(act) {
  if (act.nameOfAct === "Income-tax Act, 1961") {
    return path.join(OUTPUT_DIR, "income_tax_act_1961.json");
  }
  if (act.nameOfAct === "Income-tax Act, 2025") {
    return path.join(OUTPUT_DIR, "income_tax_act_2025.json");
  }
  const pageSlug = slugify(act.pageURL || "");
  const nameSlug = slugify(act.nameOfAct || "");
  return path.join(OUTPUT_DIR, `${pageSlug || nameSlug || act.assetCategoryID}.json`);
}

function textFromHtml(html) {
  return String(html || "")
    .replace(/<script[\s\S]*?<\/script>/gi, "")
    .replace(/<style[\s\S]*?<\/style>/gi, "")
    .replace(/<br\s*\/?\s*>/gi, "\n")
    .replace(/<\/(?:p|tr|div|li)>/gi, "\n")
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/\s+\n/g, "\n")
    .replace(/\n\s+/g, "\n")
    .replace(/[ \t]{2,}/g, " ")
    .trim();
}

function contentFields(item) {
  return item?.embedded?.contentFields || [];
}

function fieldMap(item) {
  const fields = {};
  for (const field of contentFields(item)) {
    if (field.contentFieldValue) {
      fields[field.name] = field.contentFieldValue.data || "";
    }
  }
  return fields;
}

function htmlField(item) {
  for (const field of contentFields(item)) {
    const value = field.contentFieldValue?.data || "";
    if (value.includes("<!DOCTYPE") || value.includes("<html") || value.includes("<p") || value.includes("<table")) {
      return value;
    }
  }
  return "";
}

function compareItems(a, b) {
  const aNumber = String(a.section_number || "");
  const bNumber = String(b.section_number || "");
  const aMatch = aNumber.match(/^(\d+)(.*)/);
  const bMatch = bNumber.match(/^(\d+)(.*)/);
  if (aMatch && bMatch) {
    const diff = Number(aMatch[1]) - Number(bMatch[1]);
    return diff || aMatch[2].localeCompare(bMatch[2]);
  }
  return aNumber.localeCompare(bNumber);
}

async function fetchJson(url, options, label) {
  for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt += 1) {
    const response = await fetch(url, options);
    if (response.ok) {
      return response.json();
    }
    console.error(`${label}: HTTP ${response.status} on attempt ${attempt}`);
    if (attempt < MAX_ATTEMPTS) {
      await sleep(RETRY_DELAY_MS);
    }
  }
  throw new Error(`${label}: failed after ${MAX_ATTEMPTS} attempts`);
}

async function fetchCatalog() {
  const data = await fetchJson(CATALOG_URL, { method: "GET", headers: { Accept: "application/json" } }, "catalog");
  return data.items || [];
}

function yearIdForAct(act) {
  return act.hasAmendments ? YEAR_ID : "";
}

async function fetchActSections(act) {
  const rows = [];
  const body = JSON.stringify({
    attributes: {
      "search.empty.search": true,
      "search.experiences.blueprint.external.reference.code": "ACT_SECTIONS_BP_ERC",
      "search.experiences.act_id": Number(act.assetCategoryID),
      "search.experiences.year_id": yearIdForAct(act),
      "search.experiences.free_text": "",
    },
  });

  for (let page = 1; page <= MAX_PAGES_PER_ACT; page += 1) {
    const url = `${SEARCH_URL}?nestedFields=embedded&page=${page}&pageSize=${PAGE_SIZE}&restrictFields=embedded.actions%2Cembedded.creator`;
    const data = await fetchJson(
      url,
      { method: "POST", headers: { Accept: "application/json", "Content-Type": "application/json" }, body },
      `${act.nameOfAct} page ${page}`
    );
    const items = data.items || [];
    for (const item of items) {
      const fields = fieldMap(item);
      const html = htmlField(item);
      rows.push({
        title: item.title || "",
        item_url: item.itemURL || "",
        section_number: fields.sectionNumber || "",
        description: fields.sectionShortDescription || "",
        full_text: textFromHtml(html),
        html_content: html,
      });
    }
    console.log(`  page ${page}: +${items.length}`);
    if (items.length < PAGE_SIZE) {
      break;
    }
    await sleep(PAGE_DELAY_MS);
  }

  const seen = new Set();
  const unique = [];
  for (let index = rows.length - 1; index >= 0; index -= 1) {
    const key =
      rows[index].item_url ||
      [rows[index].section_number, rows[index].description, rows[index].full_text.slice(0, 120)].join("|");
    if (!seen.has(key)) {
      seen.add(key);
      unique.unshift(rows[index]);
    }
  }
  unique.sort(compareItems);
  return unique;
}

async function main() {
  fs.mkdirSync(OUTPUT_DIR, { recursive: true });
  const acts = await fetchCatalog();
  console.log(`Catalog acts: ${acts.length}`);

  let downloaded = 0;
  let skipped = 0;
  let failed = 0;
  for (const act of acts) {
    const outputPath = outputPathForAct(act);
    const outputSlug = path.basename(outputPath, ".json");
    if (FORCE_SLUGS.size && !FORCE_SLUGS.has(outputSlug)) {
      skipped += 1;
      continue;
    }
    if (fs.existsSync(outputPath) && !FORCE_SLUGS.has(outputSlug)) {
      skipped += 1;
      console.log(`SKIP ${act.nameOfAct} -> ${outputPath}`);
      continue;
    }

    console.log(`FETCH ${act.nameOfAct} (${act.assetCategoryID})`);
    try {
      const sections = await fetchActSections(act);
      if (sections.length === 0) {
        skipped += 1;
        console.log(`  no sections returned; not writing ${outputPath}`);
        await sleep(ACT_DELAY_MS);
        continue;
      }
      const payload = {
        source: `https://www.incometaxindia.gov.in${act.pageURL || ""}`,
        act: act.nameOfAct,
        assetCategoryID: act.assetCategoryID,
        id: act.id,
        pageURL: act.pageURL,
        total_sections: sections.length,
        scraped_at: new Date().toISOString(),
        throttle: {
          page_delay_ms: PAGE_DELAY_MS,
          act_delay_ms: ACT_DELAY_MS,
          retry_delay_ms: RETRY_DELAY_MS,
          max_attempts: MAX_ATTEMPTS,
          max_pages_per_act: MAX_PAGES_PER_ACT,
        },
        sections,
      };
      fs.writeFileSync(outputPath, JSON.stringify(payload, null, 2));
      downloaded += 1;
      console.log(`  wrote ${sections.length} sections -> ${outputPath}`);
    } catch (error) {
      failed += 1;
      console.error(`  failed ${act.nameOfAct}: ${error.message}`);
    }
    await sleep(ACT_DELAY_MS);
  }

  console.log(`DONE downloaded=${downloaded} skipped=${skipped} failed=${failed}`);
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
