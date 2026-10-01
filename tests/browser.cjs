"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { once } = require("node:events");
const { pathToFileURL } = require("node:url");
const { filterStories } = require("../web/app.js");

const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "site", "index.html"), "utf8");
const fileUrl = pathToFileURL(path.join(root, "site", "index.html")).href;
const dataPattern = /(<script id="briefing-data" type="application\/json">)([\s\S]*?)(<\/script>)/u;
const data = JSON.parse(html.match(dataPattern)[2]);
const seed = data.editions.find(edition => edition.origin === "curated");
const payload = "</script><img src=x onerror=window.untrustedRan=true><script>window.untrustedRan=true";
const relevanceTopics = ["Agent products", "Web infrastructure", "Agentic applications"];
const escapedJson = value => JSON.stringify(value).replaceAll("<", "\\u003c").replaceAll(">", "\\u003e").replaceAll("&", "\\u0026");
function withPayload(unsafeUrl = false, topic = "Agent products") {
  const modified = structuredClone(data);
  const current = modified.editions.find(edition => edition.id === data.currentEditionId);
  current.items = [{
    ...seed.items[0], id: "synthetic-rendering-check", title: payload,
    excerpt: "<b>Publisher text only</b>", excerptLabel: "Publisher excerpt",
    summary: {
      status: "ready", text: "<i>Rewritten text only</i>", basis: "publisher-feed",
      model: "Synthetic test model", modelRevision: "synthetic-fixture",
      inputChars: 80, inputTruncated: false, generatedAt: data.lastSuccessfulRefresh,
    },
    topic, matchedTerms: topic === "Agent products" ? ["introducing", "proactive assistant"] : ["public websites", "agent workflow"],
    url: unsafeUrl ? "javascript:window.untrustedRan=true" : "https://example.org/research",
  }];
  return html.replace(dataPattern, (_, before, text, after) => before + escapedJson(modified) + after);
}

class Protocol {
  constructor(socket) {
    this.socket = socket;
    this.sequence = 0;
    this.pending = new Map();
    this.errors = [];
    this.requests = [];
    socket.addEventListener("message", event => {
      const message = JSON.parse(event.data);
      if (message.id) {
        const request = this.pending.get(message.id);
        if (!request) return;
        this.pending.delete(message.id);
        clearTimeout(request.timer);
        if (message.error) request.reject(new Error(JSON.stringify(message.error)));
        else request.resolve(message.result);
      }
      if (message.method === "Runtime.exceptionThrown") this.errors.push(message.params.exceptionDetails.text);
      if (message.method === "Log.entryAdded" && message.params.entry.level === "error") this.errors.push(message.params.entry.text);
      if (message.method === "Network.requestWillBeSent") this.requests.push(message.params.request.url);
    });
  }
  async send(method, params = {}) {
    const id = ++this.sequence;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error("DevTools timed out: " + method + (params.url ? " " + params.url : "")));
      }, 30000);
      this.pending.set(id, { resolve, reject, timer });
      this.socket.send(JSON.stringify({ id, method, params }));
    });
  }
  async evaluate(expression) {
    const response = await this.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    if (response.exceptionDetails) throw new Error(JSON.stringify(response.exceptionDetails));
    return response.result.value;
  }
  async wait(expression) {
    for (let attempt = 0; attempt < 80; attempt++) {
      if (await this.evaluate(expression)) return;
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    throw new Error("Browser state not reached: " + expression);
  }
  close() {
    for (const request of this.pending.values()) clearTimeout(request.timer);
    this.pending.clear();
    this.socket.close();
  }
}

async function connect(url) {
  const socket = new WebSocket(url);
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("DevTools connection timed out")), 15000);
    socket.addEventListener("open", () => { clearTimeout(timer); resolve(); }, { once: true });
    socket.addEventListener("error", () => { clearTimeout(timer); reject(new Error("DevTools connection failed")); }, { once: true });
  });
  return new Protocol(socket);
}

async function assertSummaries(protocol, items) {
  const cards = await protocol.evaluate(`Array.from(document.querySelectorAll('#story-list .story'), article => {
    const blocks = article.querySelectorAll('.summary-block');
    const block = blocks[0];
    const summary = article.querySelector('[data-field="summary"]')?.textContent;
    const fields = selector => Array.from(article.querySelectorAll(selector), node => node.textContent);
    return {
      id: article.dataset.storyId,
      blocks: blocks.length,
      headings: fields('h4'),
      summaries: fields('[data-field="summary"]'),
      provenance: fields('[data-field="summaryLabel"]'),
      evidence: fields('details [data-field="excerpt"]'),
      evidenceProvenance: fields('details [data-field="excerptLabel"]'),
      visible: Boolean(block) && [block, ...block.querySelectorAll('h4, [data-field]')].filter(node => node.textContent).every(node =>
        node.getClientRects().length > 0 && getComputedStyle(node).visibility === 'visible'),
      contained: Boolean(block?.querySelector('[data-field="summary"]') && block.querySelector('[data-field="summaryLabel"]')),
      originalDisclosed: !block?.querySelector('[data-field="excerpt"]') && Boolean(article.querySelector('details [data-field="excerpt"]')),
      copies: summary ? article.textContent.split(summary).length - 1 : 0,
    };
  })`);
  assert.deepEqual(cards, items.map(item => ({
    id: item.id,
    blocks: 1,
    headings: ["Summary"],
    summaries: [item.summary?.text || "A rewritten summary is unavailable in this saved snapshot. The original short source text remains in the evidence disclosure."],
    provenance: [{
      "public-abstract": "AI-rewritten from the public abstract in the arXiv feed",
      "publisher-feed": "AI-rewritten from the publisher feed description",
      "editorial-seed": "AI-rewritten from the preserved source-based editorial seed note",
    }[item.summary?.basis] || "Rewritten summary unavailable"],
    evidence: [item.excerpt],
    evidenceProvenance: [item.excerptLabel === "Publisher excerpt" ? "Publisher RSS/Atom excerpt" : item.excerptLabel],
    visible: true,
    contained: true,
    originalDisclosed: true,
    copies: 1,
  })));
}

async function main() {
  const requestedUrl = process.env.READER_URL ? new URL(process.env.READER_URL) : null;
  assert.ok(!requestedUrl || ["http:", "https:"].includes(requestedUrl.protocol), "READER_URL must be an HTTP(S) reader URL.");
  const candidates = process.env.BROWSER_BIN ? [process.env.BROWSER_BIN] : [
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
    "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
  ];
  const executable = candidates.find(file => fs.existsSync(file));
  assert.ok(executable, "Install Chrome/Chromium/Edge or set BROWSER_BIN; no browser checks were run.");
  const server = http.createServer((request, response) => {
    if (request.url === "/favicon.ico") { response.writeHead(204); response.end(); return; }
    response.setHeader("Content-Type", "text/html; charset=utf-8");
    const requested = new URL(request.url, "http://127.0.0.1");
    response.end(requested.pathname.startsWith("/unsafe-url") ? withPayload(true) : requested.pathname.startsWith("/untrusted") ? withPayload(false, requested.searchParams.get("topic") || "Agent products") : html);
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const base = "http://127.0.0.1:" + server.address().port;
  const readerUrl = requestedUrl || new URL(base + "/");
  readerUrl.searchParams.set("clawpilotTheme", "light");
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "grounding-reader-test-"));
  const args = [
    "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
    "--disable-background-networking", "--disable-component-update", "--disable-sync",
    "--disable-extensions", "--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1",
    "--user-data-dir=" + profile, "about:blank",
  ];
  if (process.env.GITHUB_ACTIONS === "true") args.unshift("--no-sandbox");
  const browser = spawn(executable, args, { stdio: ["ignore", "ignore", "pipe"] });
  let protocol;
  let browserProtocol;
  try {
    const endpoint = await new Promise((resolve, reject) => {
      let output = "";
      const timer = setTimeout(() => reject(new Error("Headless browser did not expose DevTools")), 25000);
      browser.once("error", error => { clearTimeout(timer); reject(error); });
      browser.stderr.on("data", chunk => {
        output += chunk.toString();
        const match = output.match(/DevTools listening on (ws:\/\/[^\s]+)/u);
        if (match) { clearTimeout(timer); resolve(match[1]); }
        if (output.length > 20000) output = output.slice(-10000);
      });
      browser.once("exit", code => { clearTimeout(timer); reject(new Error("Browser exited early: " + code)); });
    });
    browserProtocol = await connect(endpoint);
    const port = new URL(endpoint).port;
    const response = await fetch("http://127.0.0.1:" + port + "/json/new?about:blank", { method: "PUT" });
    assert.equal(response.status, 200);
    protocol = await connect((await response.json()).webSocketDebuggerUrl);
    for (const command of ["Page.enable", "Runtime.enable", "Log.enable", "Network.enable"]) await protocol.send(command);
    await protocol.send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });
    await protocol.send("Page.navigate", { url: readerUrl.href });
    await protocol.wait("document.readyState === 'complete' && document.getElementById('stamp-date').textContent.length > 0");
    assert.equal(await protocol.evaluate("document.getElementById('load-error').hidden"), true);
    assert.equal(await protocol.evaluate("document.documentElement.dataset.theme"), "light");
    const current = data.editions.find(edition => edition.id === data.currentEditionId);
    assert.equal(await protocol.evaluate("document.querySelectorAll('#story-list .story').length"), current.items.length);
    await assertSummaries(protocol, current.items);
    for (const topic of new Set(current.items.map(item => item.topic))) {
      await protocol.evaluate(`document.getElementById('filter').value = ${JSON.stringify(topic)}; document.getElementById('filter').dispatchEvent(new Event('change'))`);
      assert.equal(await protocol.evaluate("document.querySelectorAll('#story-list .story').length"), current.items.filter(item => item.topic === topic).length);
      assert.deepEqual(await protocol.evaluate("[...document.querySelectorAll('#story-list [data-field=\"topic\"]')].map(node => node.textContent)"), current.items.filter(item => item.topic === topic).map(item => item.topic));
      await assertSummaries(protocol, current.items.filter(item => item.topic === topic));
    }
    await protocol.evaluate("document.getElementById('filter').value = 'all'; document.getElementById('filter').dispatchEvent(new Event('change'))");
    if (current.items.length) {
      const sources = new Map(data.sources.map(source => [source.id, source]));
      const publisher = sources.get(current.items[0].sourceId).name;
      await protocol.evaluate(`document.getElementById('search').value = ${JSON.stringify(publisher)}; document.getElementById('search').dispatchEvent(new Event('input'))`);
      await assertSummaries(protocol, filterStories(current, sources, publisher, "all"));
      await protocol.evaluate("document.getElementById('clear-search').click()");
    }
    assert.ok((await protocol.evaluate("document.getElementById('refresh-date').textContent")).length);
    assert.doesNotMatch(await protocol.evaluate("document.body.innerText"), /Private website preview|Not published|Not scheduled|First edition/u);
    assert.equal(await protocol.evaluate("getComputedStyle(document.querySelector('.brand-mark')).color === getComputedStyle(document.querySelector('.brand-accent')).color"), false);
    await protocol.evaluate("document.getElementById('theme-toggle').click()");
    assert.equal(await protocol.evaluate("document.documentElement.dataset.theme"), "dark");
    assert.equal(await protocol.evaluate("document.getElementById('theme-toggle').getAttribute('aria-pressed')"), "true");

    await protocol.evaluate("location.hash = '#archive'");
    await protocol.wait("!document.getElementById('archive-view').hidden");
    assert.equal(await protocol.evaluate("document.querySelectorAll('#archive-list .archive-card').length"), data.editions.length);
    await protocol.evaluate("location.hash = '#edition/" + seed.id + "'");
    await protocol.wait("document.getElementById('edition-heading').textContent === 'Archived edition' || " + JSON.stringify(seed.id === data.currentEditionId));
    assert.equal(await protocol.evaluate("document.querySelectorAll('#story-list .story').length"), seed.items.length);
    await assertSummaries(protocol, seed.items);
    assert.match(await protocol.evaluate("document.getElementById('story-list').innerText"), /Publication date uncertain/u);
    await protocol.evaluate("document.querySelector('#story-list details summary').click()");
    assert.equal(await protocol.evaluate("document.querySelector('#story-list details').open"), true);
    await protocol.evaluate("document.getElementById('search').value = 'exa'; document.getElementById('search').dispatchEvent(new Event('input'))");
    assert.equal(await protocol.evaluate("document.querySelectorAll('#story-list .story').length"), 1);
    await assertSummaries(protocol, seed.items.filter(item => item.sourceId === "exa"));
    await protocol.evaluate("document.getElementById('clear-search').click(); document.getElementById('filter').value = 'Web access'; document.getElementById('filter').dispatchEvent(new Event('change'))");
    assert.equal(await protocol.evaluate("document.querySelectorAll('#story-list .story').length"), 1);
    await assertSummaries(protocol, seed.items.filter(item => item.topic === "Web access"));
    await protocol.evaluate("document.querySelector('[data-nav=\"edition\"]').click()");
    await protocol.wait("document.getElementById('edition-heading').textContent === 'This edition'");
    assert.equal(await protocol.evaluate("document.querySelectorAll('#story-list .story').length"), current.items.length);
    await assertSummaries(protocol, current.items);

    await protocol.evaluate("location.hash = '#sources'");
    await protocol.wait("!document.getElementById('sources-view').hidden");
    assert.equal(await protocol.evaluate("document.querySelectorAll('#source-list .source-row').length"), 24);
    assert.match(await protocol.evaluate("document.getElementById('source-intro').textContent"), /29 September 2026/u);
    assert.match(await protocol.evaluate("document.getElementById('source-list').innerText"), /Reference only - not collected/u);
    await protocol.evaluate("document.getElementById('search').value = 'Artificial Analysis'; document.getElementById('search').dispatchEvent(new Event('input'))");
    assert.equal(await protocol.evaluate("document.querySelectorAll('#source-list .source-row').length"), 1);
    await protocol.evaluate("document.getElementById('clear-search').click(); document.getElementById('filter').value = 'Medium'; document.getElementById('filter').dispatchEvent(new Event('change'))");
    assert.equal(await protocol.evaluate("document.querySelectorAll('#source-list .source-row').length"), 2);
    await protocol.send("Emulation.setDeviceMetricsOverride", { width: 360, height: 800, deviceScaleFactor: 1, mobile: true });
    assert.equal(await protocol.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), true);
    await protocol.evaluate("location.hash = '#edition/" + seed.id + "'");
    await protocol.wait("!document.getElementById('edition-view').hidden");
    assert.equal(await protocol.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), true);
    await protocol.send("Emulation.setDeviceMetricsOverride", { width: 320, height: 740, deviceScaleFactor: 1, mobile: true });
    assert.equal(await protocol.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), true);
    await assertSummaries(protocol, seed.items);
    assert.deepEqual(protocol.errors, []);

    await protocol.send("Page.navigate", { url: fileUrl + "?clawpilotTheme=light" });
    await protocol.wait("location.protocol === 'file:' && document.readyState === 'complete' && document.getElementById('stamp-date').textContent.length > 0");
    assert.equal(await protocol.evaluate("document.getElementById('load-error').hidden"), true);
    assert.equal(await protocol.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), true);
    await assertSummaries(protocol, current.items);
    await protocol.evaluate("document.getElementById('theme-toggle').click()");
    assert.equal(await protocol.evaluate("document.documentElement.dataset.theme"), "dark");
    assert.equal(await protocol.evaluate("new URL(location.href).searchParams.get('clawpilotTheme')"), "dark");
    assert.deepEqual(protocol.errors, []);

    for (const topic of relevanceTopics) {
      await protocol.send("Page.navigate", { url: base + "/untrusted?clawpilotTheme=dark&topic=" + encodeURIComponent(topic) });
      await protocol.wait(`location.pathname === '/untrusted' && document.readyState === 'complete' && document.querySelector('#story-list [data-field="topic"]')?.textContent === ${JSON.stringify(topic)}`);
      assert.equal(await protocol.evaluate("document.querySelector('#story-list h3').textContent"), payload);
      assert.equal(await protocol.evaluate("document.querySelector('#story-list .story-summary').textContent"), "<i>Rewritten text only</i>");
      assert.equal(await protocol.evaluate("document.querySelector('#story-list .source-excerpt').textContent"), "<b>Publisher text only</b>");
      await assertSummaries(protocol, JSON.parse(withPayload(false, topic).match(dataPattern)[2]).editions.find(edition => edition.id === data.currentEditionId).items);
      await protocol.evaluate(`document.getElementById('filter').value = ${JSON.stringify(topic)}; document.getElementById('filter').dispatchEvent(new Event('change'))`);
      assert.equal(await protocol.evaluate("document.querySelectorAll('#story-list .story').length"), 1);
      assert.equal(await protocol.evaluate("document.querySelector('#story-list [data-field=\"topic\"]').textContent"), topic);
      assert.match(await protocol.evaluate("document.querySelector('#story-list [data-field=\"why\"]').textContent"), topic === "Agent products" ? /^First-party agent-product launch wording:/u : /^Matched web-intelligence signals:/u);
      assert.equal(await protocol.evaluate("document.querySelectorAll('#story-list img, #story-list script, #story-list b, #story-list i').length"), 0);
      assert.equal(await protocol.evaluate("Boolean(window.untrustedRan)"), false);
      assert.deepEqual(protocol.errors, []);
    }

    await protocol.send("Page.navigate", { url: base + "/unsafe-url" });
    await protocol.wait("document.readyState === 'complete' && !document.getElementById('load-error').hidden");
    assert.equal(await protocol.evaluate("document.getElementById('main-content').hidden"), true);
    assert.equal(await protocol.evaluate("Boolean(window.untrustedRan)"), false);
    assert.ok(protocol.requests.every(url => url.startsWith(base) || url.startsWith(fileUrl) || url.startsWith(readerUrl.origin + "/") || url === "about:blank"), "The reader made an unexpected external network request.");
  } finally {
    if (protocol) protocol.close();
    if (browserProtocol) {
      await browserProtocol.send("Browser.close").catch(error => console.error("Browser cleanup:", error.message));
      browserProtocol.close();
    }
    if (browser.exitCode === null) {
      await Promise.race([once(browser, "exit"), new Promise(resolve => setTimeout(resolve, 3000))]);
      if (browser.exitCode === null) browser.kill();
    }
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
    // Windows may release the browser's profile handles after its main process exits.
    await fs.promises.rm(profile, { recursive: true, force: true, maxRetries: 20, retryDelay: 300 });
  }
  console.log("Browser checks passed: live data, single visible summaries with accurate provenance, theme, mobile, archive/home routes, search, filters, evidence, source health, text-only rendering and invalid-data error.");
}

main().catch(error => { console.error(error); process.exitCode = 1; });
