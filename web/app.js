(() => {
  "use strict";
  const dateFormatter = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "long", year: "numeric", timeZone: "Asia/Kolkata" });
  const timeFormatter = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", timeZone: "Asia/Kolkata" });
  const formatDate = value => dateFormatter.format(new Date(value.length === 10 ? value + "T12:00:00Z" : value));
  const formatTime = value => timeFormatter.format(new Date(value)) + " IST";
  const clean = value => String(value ?? "").toLocaleLowerCase("en");
  const matches = (parts, query) => clean(parts.filter(Boolean).join(" ")).includes(clean(query.trim()));

  function validateUrl(value) {
    if (typeof value !== "string" || /[\u0000-\u0020\u007f\\]/u.test(value)) throw new Error("Invalid source URL.");
    const url = new URL(value);
    if (!["https:", "http:"].includes(url.protocol) || url.username || url.password) throw new Error("Only public HTTP(S) source links are allowed.");
    if (url.port && !["80", "443"].includes(url.port)) throw new Error("Nonstandard source port.");
    const host = url.hostname.toLowerCase().replace(/\.+$/u, "");
    if (!host.includes(".") || host.startsWith("[") || /^[\d.]+$/u.test(host) || /(?:\.local$|\.internal$|\.localhost$)/u.test(host)) throw new Error("Nonpublic source URL.");
    return url.href;
  }

  function route(hash, data) {
    const parts = hash.replace(/^#/u, "").split("/");
    const view = ["edition", "archive", "sources"].includes(parts[0]) ? parts[0] : "edition";
    const id = view === "edition" && parts[1] ? parts[1] : data.currentEditionId;
    const edition = data.editions.find(item => item.id === id);
    return { view, edition: edition || data.editions.find(item => item.id === data.currentEditionId), missing: !edition };
  }

  function coverage(edition) {
    const total = edition.coverage.length;
    const successful = edition.coverage.filter(item => item.status === "ok").length;
    return { total, successful, failed: total - successful };
  }

  function nextFriday(value) {
    const last = new Date(value);
    const due = new Date(last);
    due.setUTCHours(3, 30, 0, 0);
    due.setUTCDate(due.getUTCDate() + ((5 - due.getUTCDay() + 7) % 7));
    if (due <= last) due.setUTCDate(due.getUTCDate() + 7);
    return due;
  }

  function freshness(data, now = new Date()) {
    const current = data.editions.find(item => item.id === data.currentEditionId);
    const health = coverage(current);
    if (!data.lastSuccessfulRefresh) return { level: "pending", due: null, health, message: "No successful automatic collection has been recorded." };
    const due = nextFriday(data.lastSuccessfulRefresh);
    const overdue = now.getTime() > due.getTime() + data.site.schedule.graceHours * 3600000;
    const counts = `${health.successful}/${health.total} configured collectors succeeded.`;
    return {
      level: overdue ? "overdue" : health.failed ? "partial" : "current",
      due,
      health,
      message: `${overdue ? "Refresh overdue. " : health.failed ? "Partial source coverage. " : "Collection is current. "}${counts} Last successful refresh: ${formatTime(data.lastSuccessfulRefresh)}.`,
    };
  }

  function editionDescription(edition) {
    if (edition.origin === "curated") return `${edition.items.length} qualified seed items; a manually prepared snapshot, not a claim of automated source coverage.`;
    const health = coverage(edition);
    const lead = edition.items.length ? `${edition.items.length} new-to-this-archive items.` : "Quiet result: no new qualifying items from the successful feeds.";
    return `${lead} ${health.successful}/${health.total} collectors succeeded${health.failed ? "; coverage is partial" : ""}. Publication window: ${formatTime(edition.windowStart)} to ${formatTime(edition.windowEnd)}. Reference-only sources were not scanned.`;
  }

  function filterStories(edition, sources, query, filter) {
    return edition.items.filter(item => (filter === "all" || item.topic === filter) &&
      matches([item.title, item.summary?.text, item.excerpt, item.topic, item.kind, sources.get(item.sourceId).name], query));
  }

  function summaryFor(item) {
    const labels = {
      "public-abstract": "AI-rewritten from the public abstract in the arXiv feed",
      "publisher-feed": "AI-rewritten from the publisher feed description",
      "editorial-seed": "AI-rewritten from the preserved source-based editorial seed note",
    };
    const summary = item.summary;
    if (summary?.status === "ready" && typeof summary.text === "string" && summary.text.trim() && labels[summary.basis]) {
      return { text: summary.text, label: labels[summary.basis], ready: true };
    }
    return {
      text: "A rewritten summary is unavailable in this saved snapshot. The original short source text remains in the evidence disclosure.",
      label: "Rewritten summary unavailable",
      ready: false,
    };
  }

  function validateData(data) {
    if (data.schemaVersion !== 2 || !Array.isArray(data.sources) || !Array.isArray(data.editions) || !data.site) throw new Error("Unsupported or incomplete briefing data.");
    const sources = new Map(data.sources.map(source => [source.id, source]));
    if (sources.size !== data.sources.length) throw new Error("Duplicate source identifiers.");
    for (const source of data.sources) {
      validateUrl(source.url);
      if (source.feed) validateUrl(source.feed);
    }
    for (const key of ["url", "repository", "workflow"]) validateUrl(data.site[key]);
    if (!data.editions.some(edition => edition.id === data.currentEditionId)) throw new Error("The current edition is missing.");
    const ids = new Set();
    for (const edition of data.editions) {
      if (ids.has(edition.id) || !/^\d{4}-\d{2}-\d{2}$/u.test(edition.date) || !Array.isArray(edition.items) || !Array.isArray(edition.coverage)) throw new Error("Invalid edition.");
      ids.add(edition.id);
      for (const item of edition.items) {
        if (!sources.has(item.sourceId)) throw new Error("Unknown story source.");
        validateUrl(item.url);
      }
    }
    if (data.lastSuccessfulRefresh && !Number.isFinite(new Date(data.lastSuccessfulRefresh).getTime())) throw new Error("Invalid refresh timestamp.");
    return sources;
  }

  function boot() {
    const byId = id => document.getElementById(id);
    const state = { view: "edition", query: "", filter: "all" };
    let data, edition, sources, latest;

    function element(tag, className, text) {
      const node = document.createElement(tag);
      if (className) node.className = className;
      if (text !== undefined) node.textContent = text;
      return node;
    }

    function link(label, url, className) {
      const node = element("a", className, label);
      node.href = validateUrl(url);
      node.target = "_blank";
      node.rel = "noopener noreferrer";
      return node;
    }

    function showEmpty(container, title, explanation, reset = true) {
      const box = element("div", "empty");
      box.append(element("h3", "", title), element("p", "", explanation));
      if (reset) {
        const button = element("button", "quiet-button", "Clear filters");
        button.type = "button";
        button.addEventListener("click", clearFilters);
        box.append(button);
      } else {
        const anchor = element("a", "quiet-button", "See source health");
        anchor.href = "#sources";
        box.append(anchor);
      }
      container.append(box);
    }

    function renderStories() {
      const items = filterStories(edition, sources, state.query, state.filter);
      const container = byId("story-list");
      container.replaceChildren();
      byId("edition-heading").textContent = edition.id === data.currentEditionId ? "This edition" : "Archived edition";
      byId("edition-count").textContent = `${items.length} of ${edition.items.length} items`;
      byId("edition-description").textContent = editionDescription(edition);
      for (const item of items) {
        const article = byId("story-template").content.firstElementChild.cloneNode(true);
        const field = name => article.querySelector('[data-field="' + name + '"]');
        for (const key of ["kind", "topic", "title", "excerpt", "evidence", "caveat"]) field(key).textContent = item[key];
        field("excerptLabel").textContent = item.excerptLabel === "Publisher excerpt"
          ? "Publisher RSS/Atom excerpt" : item.excerptLabel;
        const summary = summaryFor(item);
        field("summary").textContent = summary.text;
        field("summaryLabel").textContent = summary.label;
        field("summaryMetadata").textContent = summary.ready
          ? `Rewritten with ${item.summary.model} (revision ${item.summary.modelRevision}); generated ${formatTime(item.summary.generatedAt)}. Based on ${item.summary.inputChars} characters${item.summary.inputTruncated ? " of bounded feed text, not the entire source" : " of source text"}. Automatic checks are not independent fact verification; read the source and caveats.`
          : "No excerpt is being presented as an AI rewrite. Check the workflow for a pending or failed summary backfill.";
        field("date").textContent = item.publishedAt ? formatDate(item.publishedAt) : "Publication date uncertain";
        const reason = item.topic === "Agent products" ? "First-party agent-product launch wording: " : "Matched web-intelligence signals: ";
        field("why").textContent = item.matchedTerms.length ? reason + item.matchedTerms.join(", ") + "." : "Manually selected seed; the original qualifications are preserved below.";
        field("source-link").textContent = sources.get(item.sourceId).name + " - read the source";
        field("source-link").href = validateUrl(item.url);
        article.dataset.storyId = item.id;
        container.append(article);
      }
      if (!items.length) {
        const quiet = !edition.items.length;
        showEmpty(container, quiet ? "A quiet result, not a filler edition" : "No matching stories in this edition",
          quiet ? "The successful feeds produced no new, in-window items matching the web-intelligence or qualified agent-product rules. This does not establish that nothing happened; failed and reference-only sources are not covered."
            : "Try a different topic or shorter search. The archive contains earlier editions.", !quiet);
      }
    }

    function sourceStatus(source) {
      const health = latest.coverage.find(item => item.sourceId === source.id);
      if (!source.feed) return { label: "Reference only - not collected", detail: source.referenceReason, status: "reference" };
      if (!health) return { label: "Collector configured - no result recorded", detail: "This snapshot does not contain a collection result for this feed.", status: "pending" };
      if (health.status !== "ok") return { label: health.status === "blocked" ? "Blocked - not collected" : "Failed - not collected", detail: `${health.message} Checked ${formatTime(health.checkedAt)}.`, status: health.status };
      const rejected = Object.entries(health.rejected).map(([reason, count]) => `${reason}: ${count}`).join("; ");
      return { label: "Collected public feed", status: "ok",
        detail: `${health.entries} entries examined; ${health.eligible} new candidates; ${health.selected} added on this refresh. Checked ${formatTime(health.checkedAt)}.${rejected ? " Excluded: " + rejected + "." : ""}${health.robotsNote ? " " + health.robotsNote : ""}` };
    }

    function renderSources() {
      const items = data.sources.filter(source => (state.filter === "all" || source.kind === state.filter) &&
        matches([source.name, source.kind, source.purpose, source.audience, sourceStatus(source).label], state.query));
      const container = byId("source-list");
      container.replaceChildren();
      byId("sources-count").textContent = `${items.length} of ${data.sources.length} sources`;
      const configured = data.sources.filter(source => source.feed).length;
      byId("source-description").textContent = `${configured} configured feed collectors; ${data.sources.length - configured} reference-only sources. Health below describes the latest saved refresh, not the entire catalog.`;
      byId("source-intro").textContent = `Audience numbers are snapshots verified on ${formatDate(data.site.audienceVerifiedAt)}, not live counters. Readers, subscribers and cross-channel counts are not directly comparable. Public excerpts do not imply paywall or full-article access.`;
      for (const source of items) {
        const row = element("article", "source-row");
        const identity = element("div");
        identity.append(element("p", "source-kind", source.kind));
        const title = element("h3");
        title.append(link(source.name, source.url));
        identity.append(title);
        if (source.feed) identity.append(link("Configured public feed", source.feed, "source-feed"));
        const notes = element("div");
        notes.append(element("p", "source-purpose", source.purpose));
        if (source.audience) notes.append(element("p", "source-audience", source.audience));
        const status = sourceStatus(source);
        const health = element("p", "source-health");
        const label = element("span", "health-label", status.label);
        label.dataset.status = status.status;
        health.append(label, document.createTextNode(status.detail));
        notes.append(health);
        row.append(identity, notes);
        container.append(row);
      }
      if (!items.length) showEmpty(container, "No sources match these filters", "Search by publisher, author, coverage or collection status.");
    }

    function renderArchive() {
      const items = data.editions.filter(item => matches([item.name, item.date, formatDate(item.date), editionDescription(item), ...item.items.map(story => story.title)], state.query));
      const container = byId("archive-list");
      container.replaceChildren();
      byId("archive-count").textContent = `${items.length} of ${data.editions.length} editions`;
      for (const item of items) {
        const card = element("article", "archive-card");
        card.append(element("p", "archive-date", formatDate(item.date)));
        const content = element("div");
        content.append(element("h3", "", item.name), element("p", "", editionDescription(item)));
        const anchor = element("a", "quiet-button", "Read this edition");
        anchor.href = "#edition/" + item.id;
        content.append(anchor);
        card.append(content);
        container.append(card);
      }
      if (!items.length) showEmpty(container, "No matching editions", "Search by date, title or a story in the archive.");
    }

    function render() {
      byId("clear-search").hidden = !state.query;
      if (state.view === "sources") renderSources();
      else if (state.view === "archive") renderArchive();
      else renderStories();
    }

    function clearFilters() {
      state.query = "";
      state.filter = "all";
      byId("search").value = "";
      byId("filter").value = "all";
      render();
    }

    function setView() {
      const selected = route(window.location.hash, data);
      state.view = selected.view;
      edition = selected.edition;
      state.query = "";
      state.filter = "all";
      byId("search").value = "";
      byId("load-error").hidden = !selected.missing;
      if (selected.missing) byId("load-error").textContent = "That archived edition is not available in this copy. Showing the latest saved edition.";
      document.querySelectorAll("[data-nav]").forEach(anchor => {
        if (anchor.dataset.nav === state.view) anchor.setAttribute("aria-current", "page");
        else anchor.removeAttribute("aria-current");
      });
      for (const view of ["edition", "archive", "sources"]) byId(view + "-view").hidden = state.view !== view;
      const select = byId("filter");
      select.replaceChildren();
      const sourceView = state.view === "sources";
      const all = element("option", "", sourceView ? "All source types" : "All topics");
      all.value = "all";
      select.append(all);
      const values = sourceView ? data.sources.map(source => source.kind) : edition.items.map(item => item.topic);
      for (const value of [...new Set(values)].sort()) {
        const option = element("option", "", value);
        option.value = value;
        select.append(option);
      }
      select.hidden = state.view === "archive";
      byId("filter-label").hidden = select.hidden;
      byId("filter-label").textContent = sourceView ? "Source type" : "Topic";
      byId("search").placeholder = sourceView ? "Search publications, authors or collection status" : state.view === "archive" ? "Search editions, dates or story titles" : "Search stories, topics or publishers";
      byId("stamp-label").textContent = sourceView ? "Latest collection" : state.view === "archive" ? "Saved history" : edition.id === data.currentEditionId ? "Latest edition" : "Archived edition";
      byId("stamp-date").textContent = state.view === "archive" ? `${data.editions.length} editions` : formatDate(sourceView ? latest.date : edition.date);
      byId("stamp-note").textContent = sourceView ? "Collectors and references are labeled separately." : state.view === "archive" ? "Original dates and qualifications preserved." : edition.origin === "curated" ? "Qualified seed; not an automatic refresh." : `${edition.items.length} items. No filler to meet a quota.`;
      render();
    }

    function updateStatus() {
      const status = freshness(data);
      byId("freshness-banner").dataset.level = status.level;
      byId("freshness-summary").textContent = status.message;
      byId("refresh-date").textContent = data.lastSuccessfulRefresh ? formatTime(data.lastSuccessfulRefresh) : "No successful collection recorded";
      byId("cadence").textContent = data.site.schedule.label;
      byId("next-refresh").textContent = status.due ? formatTime(status.due.toISOString()) : "After the first successful collection";
      const references = data.sources.filter(source => !source.feed).length;
      byId("coverage-summary").textContent = `${status.health.successful}/${status.health.total} collectors succeeded on the latest refresh. Feed success does not guarantee complete topic coverage. ${references} sources are reference-only, not scanned.`;
      const target = new URL(data.site.url);
      const hosted = window.location.origin === target.origin && window.location.pathname.startsWith(target.pathname);
      byId("hosting-status").textContent = hosted ? "Serving at the GitHub Pages URL" : "Local copy; Pages target configured";
      byId("automation-status").textContent = "Weekly workflow configured; run status linked below";
      byId("freshness-note").textContent = "The schedule is best effort, not exact-minute delivery. GitHub may delay runs or disable inactive schedules. Failed refreshes keep the last good edition; this copy cannot see later failures. Overdue means a target was missed by more than 24 hours.";
      byId("workflow-link").href = validateUrl(data.site.workflow);
      byId("site-link").href = validateUrl(data.site.url);
      byId("selection-limits").textContent = `At most ${data.site.selection.maxPerSource} items per source and ${data.site.selection.maxItems} per edition. Previously archived URLs or titles are excluded.`;
    }

    function updateThemeButton() {
      const dark = document.documentElement.dataset.theme === "dark";
      byId("theme-toggle").setAttribute("aria-pressed", String(dark));
      byId("theme-toggle").textContent = dark ? "Light mode" : "Dark mode";
      byId("theme-toggle").setAttribute("aria-label", dark ? "Switch to light mode" : "Switch to dark mode");
    }

    try {
      data = JSON.parse(byId("briefing-data").textContent);
      sources = validateData(data);
      latest = data.editions.find(item => item.id === data.currentEditionId);
      byId("search").addEventListener("input", event => { state.query = event.target.value; render(); });
      byId("filter").addEventListener("change", event => { state.filter = event.target.value; render(); });
      byId("clear-search").addEventListener("click", clearFilters);
      byId("theme-toggle").addEventListener("click", () => {
        document.documentElement.dataset.theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
        const url = new URL(window.location.href);
        url.searchParams.set("clawpilotTheme", document.documentElement.dataset.theme);
        window.history.replaceState(null, "", url);
        updateThemeButton();
      });
      window.addEventListener("hashchange", () => {
        if (["#main-content", "#update-status"].includes(window.location.hash)) return;
        setView();
        byId("main-content").focus({ preventScroll: true });
      });
      document.addEventListener("visibilitychange", () => { if (!document.hidden) updateStatus(); });
      window.setInterval(updateStatus, 60000);
      updateThemeButton();
      updateStatus();
      setView();
    } catch (error) {
      console.error("Grounding Weekly could not load its edition:", error);
      byId("load-error").textContent = "This edition could not be loaded because its data is incomplete or invalid. No refresh has been applied. Check the repository workflow before using this copy.";
      byId("load-error").hidden = false;
      byId("freshness-banner").hidden = true;
      byId("main-content").hidden = true;
    }
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = { validateUrl, validateData, route, coverage, nextFriday, freshness, editionDescription, filterStories, summaryFor, matches };
  }
  if (typeof document !== "undefined") boot();
})();
