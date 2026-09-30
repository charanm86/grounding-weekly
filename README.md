# Grounding Weekly

An evidence-first weekly briefing on web intelligence: upstream tools, downstream agentic applications, research and qualified first-party agent-product launches.

**Website:** https://charanm86.github.io/grounding-weekly/

Each article has one visible **Summary** section using its existing concise public-source description. Collected stories are labeled **Publisher RSS/Atom excerpt**; source-summarized archive seeds retain their original dated **Seed editorial note** label, not a claim of verbatim publisher text. The reader does not generate new prose or claim a full-article review.

The static reader keeps public links, archived editions, searchable sources, topic filters, evidence caveats, light/dark themes and collection health. Artificial Analysis is a prominent **reference**, not evidence that a model ranks well on grounding or citations.

## Setup and operation

Use Python 3.13+; the collector and builder use only the standard library. Node 24+ and an installed Chrome/Chromium/Edge are used for reader checks, not by the website.

```sh
python -m unittest discover -s tests
node --test tests/reader.test.cjs
python -m scripts.refresh
python -m scripts.build --check
python -m scripts.check_public
node tests/browser.cjs
python -m http.server 8000 --directory site --bind 127.0.0.1
```

`refresh` performs a real public-source collection and builds `site/index.html`. `python -m scripts.build` rebuilds offline from saved data without changing refresh timestamps. `--check` requires the committed reader to match its source of truth. Open the local server in a browser; the HTML also works as a self-contained local file. Set `BROWSER_BIN` to an installed Chromium-family executable if the browser test cannot find one. Set `READER_URL` to the canonical Pages URL to exercise the hosted reader against the local saved snapshot; offline and synthetic safety checks still run locally.

Only `site/` is uploaded to Pages. Editable inputs are `config/site.json`, `config/sources.json`, `data/state.json`, and `web/`. The single data file contains immutable earlier-date snapshots and their collection health. The September 29, 2026 seed was manually curated; it is explicitly not automated coverage. Exa's September 25 date and vendor-reported benchmarks, and Parallel/Lovable's September 28 index versus September 13 JSON-LD conflict, remain qualified in that archive. Audience-number verification remains September 29, 2026, not the latest refresh date.

## Pages and Friday refresh

The repository must be public, Actions enabled, and **Settings > Pages > Source** set to **GitHub Actions**. No paid runner, AI API key, external secret or long-lived PAT is required for weekly operation. Initial authorization to publish workflow files is separate from the weekly `GITHUB_TOKEN`.

The `Publish and refresh` workflow runs on `main` pushes, manually through **Actions > Publish and refresh > Run workflow**, and at cron **`30 3 * * 5`**: Friday **09:00 Asia/Kolkata / 03:30 UTC**. Pushes deploy the saved snapshot; scheduled/manual runs collect, validate, commit the source data and generated HTML, then deploy **in the same run**. A commit made with `GITHUB_TOKEN` normally does not trigger another push workflow; deployment does not depend on that. The workflow only runs for this repository's `main` branch.

GitHub schedules are **best effort, not exact-minute delivery**. Runs can be delayed or dropped during load; schedules only execute from the default branch, and GitHub can disable public-repository schedules after 60 days of inactivity. Check the linked workflow runs and re-enable a disabled schedule when necessary. The reader calculates overdue status in the browser after the next Friday target plus a 24-hour grace period; it never silently advances the saved successful-refresh time.

Actions use verified official releases pinned by commit (checked September 30, 2026), standard public `ubuntu-latest` runners, 5-10 minute job limits, scoped permissions, serialized publishing and one-day Pages artifacts. PR validation has read-only permissions and never collects or deploys. Publisher requests run without GitHub credentials; the ephemeral token is provided only to the commit/push step.

Do not add branch rules that prevent the workflow's `GITHUB_TOKEN` from committing refreshed data unless you also redesign that persistence step. A rejected/racing push fails before deployment rather than publishing an uncommitted snapshot. Re-run on current `main` after resolving the cause. Confirm the actual Pages deployment run and URL before describing initial hosting as live.

## Coverage and selection

The catalog contains 24 sources. A source with a `feed` field is an actual configured collector; sources without one are reference-only with a reason. Feed successes and failures are recorded separately for every collected edition. The Sources view describes the latest saved run, not every source in the catalog. A successful feed with zero selected stories is different from a failed or blocked feed.

Collection checks publisher `robots.txt`, never bypasses paywalls or access controls, and uses only public publisher RSS 2.0/Atom. Missing robots files (404/410) are disclosed; unavailable or disallowing policies stop that collector. Feed redirects are HTTPS-only and checked against destination robots policies. Each source has a 60-second hard process deadline, 10-second socket timeout, at most three redirects, 4 MiB feed / 512 KiB robots limits, and at most 2,000 entries. Four sources run concurrently. No raw feeds or full articles are saved.

The conservative filter examines the **title and at most 220 characters of publisher excerpt**. The scope is work that enables agents to obtain, assess, synthesize, monitor or act on web information. "Agentic scale" does not require a numeric threshold, a known brand or a literal search phrase. Transparent contextual paths select:

- **Web infrastructure:** crawling, indexes, retrieval/reranking, extraction, structured data, search/grounding APIs and connectors/tool protocols, paired with external-web context and a substantive change, implementation or study signal. A configured Search specialist's focused source context can also qualify a retrieval/crawling/extraction change without explicit web or AI wording. This contextual inference is disclosed, not presented as confirmed agent use.
- **Agentic applications:** external-information evidence (such as public filings, online listings, live fares or webpage tasks) plus an agent/automation/multi-step workflow and a concrete application, such as research, monitoring, enrichment, due diligence, market intelligence, shopping or travel. Independent, newsletter, Substack and Medium analysis, integrations, case studies and implementations are eligible on the same explicit evidence; a launch is not required.
- **Evaluation** and the established search/grounding/research topics retain direct relevance signals. External/open-corpus evidence assessments, citations and verification can qualify without being product announcements. An enterprise or internal-document mention does not veto an otherwise evidenced external-web application.
- **Agent products:** a limited-discovery fallback for configured Official or Search specialist sources whose title uses launch wording and whose title/excerpt explicitly describes AI/autonomous agents or proactive/always-on agents or assistants. It does **not** infer web-search capability, availability or independent verification from a thin announcement. The first-party restriction applies only to this fallback, not to evidence-backed applications.

Generic model releases, coding news, funding/hiring, SEO marketing, random AI/assistant mentions and internal-document-only RAG are not sufficient. The weak-context agent-product fallback is deliberately stricter: it excludes model/LLM, funding/hiring, coding/tutorial, benchmark/framework and internal-RAG signals. A generic assistant mention or the same weak-context launch wording in a newsletter does not qualify. Concrete external-web evidence is evaluated before generic-noise exclusions so relevant implementations are not discarded merely for mentioning enterprise use or coding.

Matched text and any source-context signal are shown with each story. Source kind comes from the reviewed catalog, not an untrusted feed category. No product-name, publisher-name or URL exceptions, article crawling or longer excerpts are used. This keyword filter does not measure importance and still has false positives and missed stories; a thin teaser can omit the necessary context even without truncation. **Successful feed collection is not proof of complete topic coverage.**

Publication times must include a timezone and fall inside the preceding seven days. Future, missing and invalid dates are excluded; Atom `updated` is not substituted for `published`. arXiv replacements and cross-list announcements are excluded. A preprint is not a claim of peer review. Canonical URL and normalized-title deduplication spans the entire archive; tracking parameters are removed without removing meaningful query identifiers. At most two items per source and twelve total are selected newest-first. There is no minimum quota.

A second refresh on the same Asia/Kolkata date updates one edition, retains its already-selected items and first window start, and adds only unseen items within the same caps. Subsequent dates create new snapshots. Quiet successful runs create an explicitly quiet edition; zero successful collectors fail and keep the previous good data/site. Even partial success does not imply that every source was scanned. Historical snapshots are retained in Git and in the reader; review size growth before the public-content guard's 10 MiB per-file limit is approached.

## Failure recovery and privacy

Collection, parsing, selection and HTML generation finish before any good output is replaced. Files are staged and replaced with a rollback journal so an I/O failure restores both source data and reader. Failed build/collection exits nonzero with the last good edition intact; no stale-data fallback is reported as success.

An interrupted process may leave `.refresh.lock` and `.refresh-transaction.json`. First ensure no build/refresh process is running (the lock contains its PID), then remove **only** `.refresh.lock` and rerun the command. The journal restores the prior good files before continuing; do not delete or commit it. A failed workflow leaves the previously deployed site in place. If persistence succeeded but deployment failed, rerun the workflow; a repeated same-day refresh does not duplicate the edition.

All feed strings are rendered as text. Source URLs are validated, embedded JSON escapes script terminators, XML DTD/entities are rejected, and a hash-based content security policy blocks external scripts, connections, forms and objects. No cookies, analytics, accounts, personal contacts, private documents or messaging delivery are used. Only public news, website code, minimal synthetic fixtures and the required GitHub noreply commit identities belong here. `check_public` is a generic heuristic against accidental secrets/contact details and unintended files; inspect the diff too.
