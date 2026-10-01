"use strict";
const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const reader = require("../web/app.js");
const root = path.resolve(__dirname, "..");
const read = file => JSON.parse(fs.readFileSync(path.join(root, file), "utf8"));
const data = {
  ...read("data/state.json"),
  site: read("config/site.json"),
  sources: read("config/sources.json"),
};
const curated = data.editions.find(edition => edition.origin === "curated");
const fixed = {
  ...data,
  currentEditionId: "2026-09-30",
  lastSuccessfulRefresh: "2026-09-30T03:30:00Z",
  editions: [{
    id: "2026-09-30", date: "2026-09-30", origin: "collected", items: [],
    coverage: [{ sourceId: "openai", status: "ok" }, { sourceId: "google", status: "failed" }],
    windowStart: "2026-09-23T03:30:00Z", windowEnd: "2026-09-30T03:30:00Z",
  }, curated],
};

test("source library and saved data are valid", () => {
  const sources = reader.validateData(data);
  assert.equal(sources.size, 24);
  assert.equal(sources.get("ahead-of-ai").url, "https://magazine.sebastianraschka.com/");
  assert.equal(data.site.audienceVerifiedAt, "2026-09-29");
});

test("archive routes are bookmarkable and home selects the actual current edition", () => {
  const selected = reader.route("#edition/" + curated.id, fixed);
  assert.equal(selected.edition.id, curated.id);
  assert.equal(reader.route("#edition", fixed).edition.id, fixed.currentEditionId);
  assert.equal(reader.route("#archive", fixed).view, "archive");
  assert.equal(reader.route("#sources", fixed).view, "sources");
  assert.equal(reader.route("#edition/unknown", fixed).missing, true);
});

test("story query and topic filtering preserve evidence", () => {
  const sources = new Map(data.sources.map(source => [source.id, source]));
  assert.equal(reader.filterStories(curated, sources, "exa", "all").length, 1);
  assert.equal(reader.filterStories(curated, sources, "", "Web access").length, 1);
  assert.equal(reader.filterStories(curated, sources, "no matching phrase", "all").length, 0);
  assert.equal(curated.items.find(item => item.sourceId === "parallel").publishedAt, null);
});

test("agent products have their own searchable topic, not a web-grounding label", () => {
  const sources = new Map(data.sources.map(source => [source.id, source]));
  const item = { ...curated.items[0], title: "Introducing Compass", topic: "Agent products",
    excerpt: "Compass is a proactive assistant for complex tasks.", matchedTerms: ["introducing", "proactive assistant"] };
  const edition = { ...curated, items: [...curated.items, item] };
  assert.deepEqual(reader.filterStories(edition, sources, "Compass", "Agent products"), [item]);
  assert.equal(reader.filterStories(edition, sources, "", "Agent products").length, 1);
  assert.equal(reader.filterStories(edition, sources, "Compass", "Deep research").length, 0);
  assert.equal(reader.filterStories(edition, sources, "Compass", "all").length, 1);
});

test("upstream infrastructure and downstream applications remain distinct filter topics", () => {
  const sources = new Map(data.sources.map(source => [source.id, source]));
  const upstream = { ...curated.items[0], title: "Fresher public data", topic: "Web infrastructure" };
  const downstream = { ...curated.items[1], title: "A due diligence workflow", topic: "Agentic applications" };
  const edition = { ...curated, items: [upstream, downstream] };
  assert.deepEqual(reader.filterStories(edition, sources, "", "Web infrastructure"), [upstream]);
  assert.deepEqual(reader.filterStories(edition, sources, "diligence", "Agentic applications"), [downstream]);
  assert.equal(reader.filterStories(edition, sources, "", "Agent products").length, 0);
});

test("article template separates rewritten Summary from disclosed original evidence", () => {
  const template = fs.readFileSync(path.join(root, "web/template.html"), "utf8");
  const story = template.match(/<template id="story-template">([\s\S]*?)<\/template>/u)[1];
  assert.equal((story.match(/class="summary-block"/gu) || []).length, 1);
  assert.equal((story.match(/<h4>Summary<\/h4>/gu) || []).length, 1);
  assert.equal((story.match(/data-field="excerpt"/gu) || []).length, 1);
  assert.equal((story.match(/data-field="excerptLabel"/gu) || []).length, 1);
  assert.equal((story.match(/data-field="summary"/gu) || []).length, 1);
  assert.match(story, /<details[\s\S]*data-field="excerpt"/u);
  const js = fs.readFileSync(path.join(root, "web/app.js"), "utf8");
  assert.doesNotMatch(template + js, /Web IQ|why it matters|Microsoft (?:impact|strategy)/iu);
});

test("rewritten-summary bases are explicit and missing or unknown summaries never use excerpts", () => {
  const item = { ...curated.items[0], excerpt: "The original publisher text must not be the summary.", summary: undefined };
  assert.equal(reader.summaryFor(item).ready, false);
  assert.match(reader.summaryFor(item).label, /unavailable/u);
  assert.notEqual(reader.summaryFor(item).text, item.excerpt);
  for (const basis of ["public-abstract", "publisher-feed", "editorial-seed"]) {
    const summary = { status: "ready", text: "A genuinely rewritten description.", basis };
    assert.equal(reader.summaryFor({ ...item, summary }).text, summary.text);
    assert.match(reader.summaryFor({ ...item, summary }).label, /^AI-rewritten/u);
  }
  for (const summary of [
    { status: "failed", text: item.excerpt, basis: "publisher-feed" },
    { status: "ready", text: item.excerpt, basis: "unknown" },
    { status: "ready", text: "", basis: "publisher-feed" },
  ]) {
    assert.equal(reader.summaryFor({ ...item, summary }).ready, false);
  }
});

test("search includes stored rewritten prose, not only source snippets", () => {
  const item = { ...curated.items[0], summary: { text: "A distinct paraphrase about verification." } };
  assert.equal(reader.filterStories({ ...curated, items: [item] },
    new Map(data.sources.map(source => [source.id, source])), "distinct paraphrase", "all").length, 1);
});

test("weekly status distinguishes partial, current, overdue and uncollected", () => {
  const partial = reader.freshness(fixed, new Date("2026-10-02T04:00:00Z"));
  assert.equal(partial.level, "partial");
  assert.equal(partial.due.toISOString(), "2026-10-02T03:30:00.000Z");
  assert.equal(reader.freshness(fixed, new Date("2026-10-03T03:30:01Z")).level, "overdue");
  const allOk = structuredClone(fixed);
  allOk.editions[0].coverage[1].status = "ok";
  assert.equal(reader.freshness(allOk, new Date("2026-09-30T05:00:00Z")).level, "current");
  assert.equal(reader.freshness({ ...fixed, lastSuccessfulRefresh: null }).level, "pending");
  assert.equal(reader.nextFriday("2026-10-02T03:30:00Z").toISOString(), "2026-10-09T03:30:00.000Z");
});

test("quiet, partial and curated copy never implies full catalog coverage", () => {
  assert.match(reader.editionDescription(fixed.editions[0]), /Quiet result/);
  assert.match(reader.editionDescription(fixed.editions[0]), /coverage is partial/);
  assert.match(reader.editionDescription(fixed.editions[0]), /Reference-only sources were not scanned/);
  assert.match(reader.editionDescription(curated), /not a claim of automated/);
});

test("unsafe links and broken data fail explicitly", () => {
  for (const url of ["javascript:alert(1)", "data:text/html,test", "https://user:pass" + "@" + "example.org/",
    "https://localhost/", "https://127.0.0.1/", "https://127.1/", "https://10.0.0.1/", "https://host.internal/",
    "https://example.org:9000/", "https://example.org\\path", "https://example.org/\npath"]) {
    assert.throws(() => reader.validateUrl(url), url);
  }
  assert.equal(reader.validateUrl("https://example.org/story"), "https://example.org/story");
  const invalid = structuredClone(data);
  invalid.editions[0].items = [{ sourceId: "not-a-source", url: "https://example.org/" }];
  assert.throws(() => reader.validateData(invalid), /Unknown story source/);
});

test("theme tokens and DOM rendering avoid unsafe HTML insertion", () => {
  const css = fs.readFileSync(path.join(root, "web/styles.css"), "utf8");
  const js = fs.readFileSync(path.join(root, "web/app.js"), "utf8");
  assert.match(css, /\.brand \.brand-accent/);
  assert.doesNotMatch(css, /\.brand span:last-child/);
  assert.match(css, /prefers-reduced-motion/);
  assert.doesNotMatch(js, /\.innerHTML|insertAdjacentHTML|document\.write|eval\(/);
  assert.match(js, /textContent = item\[key\]/);
  const withoutTokens = css.replace(/--cp-[\w-]+\s*:[^;]+;/gu, "");
  assert.doesNotMatch(withoutTokens, /#[0-9a-f]{3,8}\b|rgba?\(|hsla?\(/iu);
});
