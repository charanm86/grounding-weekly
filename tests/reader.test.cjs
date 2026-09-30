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
