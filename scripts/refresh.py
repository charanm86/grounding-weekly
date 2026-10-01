"""Refresh from configured public feeds, preserving prior editions and failures."""

from __future__ import annotations

import copy
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from scripts.build import ROOT, BuildError, configuration, publish_files, read_json, render, validate_state, workspace_lock
from scripts.collector import (
    CollectionError, FIRST_PARTY_KINDS, UTC, canonical_url, classify, collect_bounded, iso, parse_date, story_id, title_key,
)
from scripts.check_public import problems
from scripts.summaries import summarize_state

IST = timezone(timedelta(hours=5, minutes=30))


def candidate(entry: dict, source: dict, start: datetime, end: datetime) -> tuple[dict | None, str]:
    if not entry["title"] or not entry["rawUrl"]:
        return None, "missingFields"
    published = parse_date(entry["rawDate"])
    if published is None:
        return None, "invalidDate"
    if published > end:
        return None, "futureDate"
    if published < start:
        return None, "outsideWindow"
    if entry["announceType"] and entry["announceType"].lower() != "new":
        return None, "notNewPreprint"
    try:
        url = canonical_url(entry["rawUrl"], source["feed"])
    except CollectionError:
        return None, "invalidUrl"
    match = classify(entry["title"], entry["excerpt"], source["kind"])
    if not match:
        return None, "offTopic"
    if problems("data/story.json", " ".join((entry["title"], entry["excerpt"], url))):
        return None, "privacyGuard"
    topic, terms = match
    official = source["kind"] in FIRST_PARTY_KINDS
    research = source["kind"] == "Research"
    evidence = (
        "Publisher-reported; not independently tested" if official else
        "Preprint; peer review not established" if research else
        "Publisher excerpt; follow the original evidence"
    )
    caveat = (
        "Publication time comes from the feed, not an independent article-date check. "
        "Automatic keyword selection can miss relevant items or include marginal ones. "
        "Short excerpts omit context; read the source."
    )
    if official:
        caveat += " Product availability and benchmark comparisons are vendor-reported, not independent verification."
    if topic == "Agent products":
        caveat += " Included as an agent-product announcement; this excerpt does not establish web-search or grounding capabilities."
    if "search-specialist source context" in terms:
        caveat += " Relevance uses the publisher's web-search specialization; agent use is not established by this excerpt."
    if research:
        caveat += " A preprint is research, not a verified product capability or a claim of peer review."
    return {
        "id": story_id(url),
        "sourceId": source["id"],
        "kind": "Publisher update" if official else "Research / preprint" if research else "Analysis / discovery",
        "topic": topic,
        "publishedAt": iso(published),
        "title": entry["title"],
        "excerpt": entry["excerpt"],
        "excerptLabel": "Publisher excerpt",
        "matchedTerms": terms,
        "evidence": evidence,
        "url": url,
        "caveat": caveat,
    }, ""


def assemble(
    previous: dict, settings: dict, sources: list[dict], results: list[tuple[list[dict], dict]],
    started: datetime, completed: datetime,
) -> dict:
    limits = settings["selection"]
    start = started - timedelta(days=limits["windowDays"])
    edition_id = started.astimezone(IST).date().isoformat()
    collectors = [source for source in sources if source.get("feed")]
    if len(collectors) != len(results):
        raise BuildError("Collector results are incomplete.")
    coverage = [copy.deepcopy(health) for _, health in results]
    if not any(health["status"] == "ok" for health in coverage):
        raise CollectionError("Zero successful collectors. The previous good edition and reader have not been replaced.")
    old_edition = next((edition for edition in previous["editions"] if edition["id"] == edition_id), None)
    if old_edition and old_edition["origin"] != "collected":
        raise BuildError("A curated edition already occupies this date; preserve it instead of overwriting it.")
    items = copy.deepcopy(old_edition["items"]) if old_edition else []
    seen_urls, seen_titles = set(), set()
    for edition in previous["editions"]:
        for item in edition["items"]:
            seen_urls.add(canonical_url(item["url"]))
            seen_titles.add(title_key(item["title"]))
    candidates = []
    health_by_id = {}
    for source, (entries, _), health in zip(collectors, results, coverage):
        if health["sourceId"] != source["id"]:
            raise BuildError("Source health is associated with the wrong collector.")
        health_by_id[source["id"]] = health
        if health["status"] != "ok":
            continue
        rejected = Counter()
        for entry in entries:
            item, reason = candidate(entry, source, start, started)
            if item is None:
                rejected[reason] += 1
            elif canonical_url(item["url"]) in seen_urls or title_key(item["title"]) in seen_titles:
                rejected["alreadyArchived"] += 1
            else:
                health["eligible"] += 1
                candidates.append(item)
        health["rejected"] = dict(rejected)
    candidates.sort(key=lambda item: (item["publishedAt"], item["url"]), reverse=True)
    counts = Counter(item["sourceId"] for item in items)
    for item in candidates:
        health = health_by_id[item["sourceId"]]
        url, title = canonical_url(item["url"]), title_key(item["title"])
        reason = None
        if url in seen_urls or title in seen_titles:
            reason = "duplicate"
        elif counts[item["sourceId"]] >= limits["maxPerSource"]:
            reason = "sourceCap"
        elif len(items) >= limits["maxItems"]:
            reason = "editionCap"
        if reason:
            health["rejected"][reason] = health["rejected"].get(reason, 0) + 1
            continue
        seen_urls.add(url)
        seen_titles.add(title)
        counts[item["sourceId"]] += 1
        health["selected"] += 1
        items.append(item)
    items.sort(key=lambda item: (item["publishedAt"], item["url"]), reverse=True)
    edition = {
        "id": edition_id,
        "date": edition_id,
        "name": "Weekly briefing - " + edition_id,
        "origin": "collected",
        "outcome": "stories" if items else "quiet",
        "refreshedAt": iso(completed),
        "windowStart": min(old_edition["windowStart"], iso(start)) if old_edition else iso(start),
        "windowEnd": iso(started),
        "coverage": coverage,
        "items": items,
    }
    state = copy.deepcopy(previous)
    state.update(currentEditionId=edition_id, lastSuccessfulRefresh=iso(completed))
    state["editions"] = sorted(
        [edition] + [old for old in state["editions"] if old["id"] != edition_id],
        key=lambda old: old["date"], reverse=True,
    )
    validate_state(state, settings, sources)
    return state


def refresh(root=ROOT, collect_fn=collect_bounded, clock=lambda: datetime.now(UTC)) -> dict:
    with workspace_lock(root):
        settings, sources = configuration(root)
        previous = read_json(root / "data" / "state.json")
        validate_state(previous, settings, sources)
        started = clock()
        collectors = [source for source in sources if source.get("feed")]
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(collect_fn, collectors))
        for _, health in results:
            print(f"{health['sourceId']}: {health['status']} - {health['entries']} entries. {health['message']}")
        state = assemble(previous, settings, sources, results, started, clock())
        state = summarize_state(state, sources, results)
        page = render(state, settings, sources, root)
        publish_files(state, page, root)
        edition = state["editions"][0]
        successes = sum(health["status"] == "ok" for health in edition["coverage"])
        print(
            f"Saved {edition['id']}: {len(edition['items'])} items; "
            f"{successes}/{len(collectors)} collectors succeeded; "
            f"{len(sources) - len(collectors)} reference-only sources. "
            f"Last successful refresh: {state['lastSuccessfulRefresh']}."
        )
        if successes < len(collectors):
            print("PARTIAL COVERAGE: inspect source health; this is not a scan of the full catalog.", file=sys.stderr)
        if not edition["items"]:
            print("QUIET RESULT: no new qualifying items from successful feeds; failed/reference sources are not covered.")
        return state


def main() -> int:
    try:
        refresh()
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"Refresh failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
