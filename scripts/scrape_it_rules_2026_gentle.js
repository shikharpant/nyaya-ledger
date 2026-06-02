#!/usr/bin/env node
const fs = require("fs");

const OUTPUT = "data/Law/base_laws/income_tax_rules_2026.json";
const BASE_URL = "https://www.incometaxindia.gov.in/o/search/v1.0/search";
const PAGE_COUNT = 7;
const PAGE_SIZE = 100;
const DELAY_MS = 6500;
const RETRY_DELAY_MS = 20000;

const BODY = JSON.stringify({
  attributes: {
    "search.empty.search": true,
    "search.experiences.blueprint.external.reference.code": "ACT_SECTIONS_BP_ERC",
    "search.experiences.act_id": 8325573,
    "search.experiences.year_id": 13590315,
    "search.experiences.free_text": "",
  },
});

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
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

function compareRules(a, b) {
  const aMatch = String(a.rule_number).match(/^(\d+)(.*)/);
  const bMatch = String(b.rule_number).match(/^(\d+)(.*)/);
  if (aMatch && bMatch) {
    const numberDiff = Number(aMatch[1]) - Number(bMatch[1]);
    return numberDiff || aMatch[2].localeCompare(bMatch[2]);
  }
  return String(a.rule_number).localeCompare(String(b.rule_number));
}

async function fetchPage(page) {
  const url = `${BASE_URL}?nestedFields=embedded&page=${page}&pageSize=${PAGE_SIZE}&restrictFields=embedded.actions%2Cembedded.creator`;
  for (let attempt = 1; attempt <= 2; attempt += 1) {
    const response = await fetch(url, {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
      },
      body: BODY,
    });
    if (response.ok) {
      return response.json();
    }
    console.error(`page ${page}: HTTP ${response.status} on attempt ${attempt}`);
    if (attempt < 2) {
      await sleep(RETRY_DELAY_MS);
    }
  }
  throw new Error(`page ${page}: failed after throttled retry`);
}

async function main() {
  const rows = [];
  for (let page = 1; page <= PAGE_COUNT; page += 1) {
    const data = await fetchPage(page);
    const items = data.items || [];
    for (const item of items) {
      const fields = fieldMap(item);
      const html = htmlField(item);
      rows.push({
        rule_number: fields.sectionNumber || "",
        description: fields.sectionShortDescription || "",
        full_text: textFromHtml(html),
        html_content: html,
      });
    }
    console.log(`page ${page}/${PAGE_COUNT}: +${items.length}, total ${rows.length}`);
    if (page < PAGE_COUNT) {
      await sleep(DELAY_MS);
    }
  }

  const seen = new Set();
  const unique = [];
  for (let index = rows.length - 1; index >= 0; index -= 1) {
    const key = rows[index].rule_number;
    if (!seen.has(key)) {
      seen.add(key);
      unique.unshift(rows[index]);
    }
  }
  unique.sort(compareRules);

  const payload = {
    source: "https://www.incometaxindia.gov.in/income-tax-rule-2026",
    act: "Income-tax Rules, 2026",
    total_rules: unique.length,
    scraped_at: new Date().toISOString(),
    throttle: {
      page_delay_ms: DELAY_MS,
      retry_delay_ms: RETRY_DELAY_MS,
      max_attempts_per_page: 2,
    },
    rules: unique,
  };
  fs.writeFileSync(OUTPUT, JSON.stringify(payload, null, 2));
  console.log(`DONE ${unique.length} rules -> ${OUTPUT}; with text ${unique.filter((rule) => rule.full_text.length > 50).length}`);
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
