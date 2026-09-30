import copy
import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from xml.sax.saxutils import escape

from scripts import build, collector, refresh
from scripts.check_public import problems

NOW = datetime(2026, 9, 30, 3, 30, tzinfo=collector.UTC)
SETTINGS, SOURCES = build.configuration()
COLLECTORS = [source for source in SOURCES if source.get("feed")]
SOURCE = COLLECTORS[0]
DOTS_DESCRIPTION = (
    "Dots by OpenAI are proactive assistants that can keep working across complex projects and everyday tasks. "
    "Learn how dots help you stay in control while work moves forward."
)
DOTS_RSS = (
    '<rss version="2.0"><channel><item><title>Introducing dots</title>'
    '<link>https://openai.com/index/introducing-dots</link>'
    '<pubDate>Tue, 29 Sep 2026 00:00:00 GMT</pubDate><category>Product</category>'
    f'<description>{DOTS_DESCRIPTION}</description></item></channel></rss>'
).encode()


def seed():
    curated = next(
        edition for edition in build.read_json(build.ROOT / "data" / "state.json")["editions"]
        if edition["origin"] == "curated"
    )
    return {"schemaVersion": 2, "currentEditionId": curated["id"], "lastSuccessfulRefresh": None, "editions": [copy.deepcopy(curated)]}


def entry(title="Web search for research agents", url="https://example.org/news", published="2026-09-29T10:00:00Z"):
    return {
        "title": title, "rawUrl": url, "rawDate": published,
        "excerpt": "Research agents use public-web sources and citations.",
        "announceType": "",
    }


def health(source, status="ok", count=0):
    return {
        "sourceId": source["id"], "checkedAt": collector.iso(NOW),
        "status": status, "entries": count, "eligible": 0, "selected": 0,
        "rejected": {}, "message": "Synthetic collector result.",
    }


def results(entries=None, statuses=None):
    entries, statuses = entries or {}, statuses or {}
    return [
        (entries.get(source["id"], []), health(source, statuses.get(source["id"], "ok"), len(entries.get(source["id"], []))))
        for source in COLLECTORS
    ]


def rss(items):
    return ("<rss version='2.0'><channel>" + "".join(
        "<item><title>" + escape(item["title"]) + "</title><link>" + escape(item["rawUrl"]) +
        "</link><pubDate>" + escape(item["rawDate"]) + "</pubDate><description>" +
        escape(item["excerpt"]) + "</description></item>" for item in items
    ) + "</channel></rss>").encode()


class ParserTests(unittest.TestCase):
    def test_rss_html_is_plain_and_excerpt_is_short(self):
        item = entry()
        item["excerpt"] = "<p>Hello &amp; goodbye.</p><script>unsafe()</script>" + "Public research " * 100
        parsed = collector.parse_feed(rss([item]), SOURCE)[0]
        self.assertIn("Hello & goodbye.", parsed["excerpt"])
        self.assertNotIn("unsafe", parsed["excerpt"])
        self.assertNotIn("<p>", parsed["excerpt"])
        self.assertLessEqual(len(parsed["excerpt"]), 220)

    def test_atom_uses_published_and_alternate_link(self):
        raw = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry>
          <title>Web search &amp; AI</title><published>2026-09-29T10:00:00Z</published>
          <updated>2026-09-30T10:00:00Z</updated>
          <link rel="self" href="https://example.org/feed-entry"/>
          <link rel="alternate" type="text/html" href="https://example.org/article"/>
          <summary type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml"><p>Public web <b>research</b>.</p></div></summary>
        </entry></feed>"""
        parsed = collector.parse_feed(raw, SOURCE)[0]
        self.assertEqual(parsed["rawDate"], "2026-09-29T10:00:00Z")
        self.assertEqual(parsed["rawUrl"], "https://example.org/article")
        self.assertIn("research", parsed["excerpt"])
        without_published = raw.replace(b"<published>2026-09-29T10:00:00Z</published>", b"")
        self.assertEqual(collector.parse_feed(without_published, SOURCE)[0]["rawDate"], "")

    def test_rss_guid_and_dc_date(self):
        raw = b"""<rss xmlns:dc="http://purl.org/dc/elements/1.1/"><channel><item>
          <title>Web research</title><guid>https://example.org/research</guid>
          <dc:date>2026-09-29T10:00:00Z</dc:date></item></channel></rss>"""
        parsed = collector.parse_feed(raw, SOURCE)[0]
        self.assertEqual(parsed["rawUrl"], "https://example.org/research")
        self.assertEqual(parsed["rawDate"], "2026-09-29T10:00:00Z")
        raw = raw.replace(b"<guid>", b'<guid isPermaLink="false">')
        self.assertEqual(collector.parse_feed(raw, SOURCE)[0]["rawUrl"], "")

    def test_bad_feed_empty_feed_and_resource_limits(self):
        for raw in (b"<html></html>", b"<rss", b'<!DOCTYPE rss [<!ENTITY a "x">]><rss/>'):
            with self.subTest(raw=raw), self.assertRaises(collector.CollectionError):
                collector.parse_feed(raw, SOURCE)
        self.assertEqual(collector.parse_feed(rss([]), SOURCE), [])
        with patch.object(collector, "MAX_ENTRIES", 1), self.assertRaises(collector.CollectionError):
            collector.parse_feed(rss([entry(), entry()]), SOURCE)
        with patch.object(collector, "MAX_FEED_BYTES", 2), self.assertRaises(collector.CollectionError):
            collector.parse_feed(rss([]), SOURCE)

    def test_utf16_dtd_is_rejected_before_entity_expansion(self):
        raw = '<?xml version="1.0" encoding="utf-16"?><!DOCTYPE rss [<!ENTITY text "injected">]><rss><channel><title>&text;</title></channel></rss>'
        with self.assertRaises(collector.CollectionError):
            collector.parse_feed(raw.encode("utf-16"), SOURCE)

    def test_date_timezones_and_invalid_dates(self):
        self.assertEqual(collector.parse_date("2026-09-30T09:00:00+05:30"), NOW)
        self.assertEqual(collector.parse_date("Wed, 30 Sep 2026 03:30:00 GMT"), NOW)
        for value in (None, "", "2026-02-30T00:00:00Z", "2026-09-29", "yesterday", "2026-09-29T09:00:00"):
            self.assertIsNone(collector.parse_date(value), value)

    def test_canonicalization_preserves_meaningful_parameters(self):
        self.assertEqual(
            collector.canonical_url("https://EXAMPLE.org/post/?utm_source=test&id=42&source=rss-feed#read"),
            "https://example.org/post?id=42",
        )
        self.assertEqual(collector.canonical_url("/post", "https://example.org/feed"), "https://example.org/post")
        self.assertEqual(collector.canonical_url("http://arxiv.org/abs/2609.12345v2"), "https://arxiv.org/abs/2609.12345")
        self.assertEqual(collector.title_key("Public-web: Research!"), collector.title_key("PUBLIC web research"))

    def test_unsafe_urls(self):
        for value in (
            "javascript:alert(1)", "data:text/html,test", "file:///tmp/file",
            "https://name:password" + "@" + "example.org/", "https://localhost/",
            "https://127.0.0.1/", "https://10.0.0.1/", "https://[::1]/",
            "https://127.1/", "https://127.0.0.01/", "https://0x7f000001/", "https://localhost./",
            "https://host.internal/", "https://example.org:9000/", "https://example.org\\path",
            "https://example.org/\nheader", "https://example.org/" + "a" * 2100,
        ):
            with self.subTest(value=value), self.assertRaises(collector.CollectionError):
                collector.safe_url(value)

    def test_conservative_topic_filter(self):
        for title, preview in (
            ("Agentic search for reliable research", ""),
            ("Web search grounding for AI", ""),
            ("A new deep research agent", ""),
            ("Evaluating citation accuracy in web research", ""),
        ):
            self.assertIsNotNone(collector.classify(title, preview))
        for title in (
            "A new frontier model launches", "AI funding round announced",
            "Internal-document RAG with embeddings", "Coding agent gets faster",
            "Browser performance leaderboard", "AI hiring", "Search engine optimization tips",
        ):
            self.assertIsNone(collector.classify(title, "General product news."), title)
        self.assertEqual(collector.classify("A web search citation benchmark", "")[0], "Evaluation")


class NetworkTests(unittest.TestCase):
    def test_robots_wildcards_longest_rule_and_specific_groups(self):
        policy = collector.RobotsPolicy(
            "User-agent: *\nDisallow: /private*\nAllow: /private/public\n"
            "Disallow: /*?tracking=*\nDisallow: /exact$\n"
        )
        self.assertFalse(policy.allows("https://example.org/private-feed"))
        self.assertTrue(policy.allows("https://example.org/private/public/feed"))
        self.assertFalse(policy.allows("https://example.org/feed?tracking=yes"))
        self.assertFalse(policy.allows("https://example.org/exact"))
        self.assertTrue(policy.allows("https://example.org/exactly"))
        self.assertFalse(policy.allows("https://example.org/%70rivate"))
        specific = collector.RobotsPolicy(
            "User-agent: *\nDisallow: /\n\nUser-agent: GroundingWeekly\n"
            "Disallow: /blocked\nAllow: /blocked\nCrawl-delay: 0.5\nRequest-rate: 1/2\n"
        )
        self.assertTrue(specific.allows("https://example.org/allowed"))
        self.assertTrue(specific.allows("https://example.org/blocked"))
        self.assertEqual(specific.delay, 2)

    def test_robots_disallow_and_unavailable_are_explicit(self):
        client = collector.PublicClient()
        with patch.object(client, "request", return_value=b"User-agent: *\nDisallow: /private\n"):
            with self.assertRaises(collector.RobotsDenied):
                client.respect_robots("https://example.org/private/feed")
        client = collector.PublicClient()
        error = urllib.error.HTTPError("https://example.org/robots.txt", 403, "Forbidden", {}, None)
        with patch.object(client, "request", side_effect=error), self.assertRaises(collector.RobotsDenied):
            client.respect_robots("https://example.org/feed")

    def test_missing_robots_disclosed_and_feed_allowed(self):
        client = collector.PublicClient()
        error = urllib.error.HTTPError("https://example.org/robots.txt", 404, "Not found", {}, None)
        with patch.object(client, "request", side_effect=error):
            client.respect_robots("https://example.org/feed")
        self.assertIn("No robots.txt", client.robots_notes[0])

    def test_html_robots_and_excessive_crawl_delay_block(self):
        for body in (b"<html>Login required</html>", b"User-agent: *\nCrawl-delay: 120\n"):
            client = collector.PublicClient()
            client.last_request["example.org"] = time.monotonic()
            with patch.object(client, "request", return_value=body), self.assertRaises(collector.RobotsDenied):
                client.respect_robots("https://example.org/feed")

    def test_network_request_has_no_credentials_and_bounds_size(self):
        class Response(io.BytesIO):
            headers = {"Content-Length": "4"}

        client = collector.PublicClient()
        with patch.object(collector.socket, "getaddrinfo", return_value=[(0, 0, 0, "", ("93.184.216.34", 443))]), \
             patch.object(client, "respect_robots"), \
             patch.object(client.opener, "open", return_value=Response(b"test")) as open_mock, \
             patch.dict(os.environ, {"GH_TOKEN": "never-send-this-synthetic-value"}):
            self.assertEqual(client.request("https://example.org/feed", 4), b"test")
            request = open_mock.call_args.args[0]
            self.assertNotIn("Authorization", request.headers)
            self.assertNotIn("Cookie", request.headers)
            self.assertNotIn("never-send", str(request.headers))
        with patch.object(collector.socket, "getaddrinfo", return_value=[(0, 0, 0, "", ("93.184.216.34", 443))]), \
             patch.object(client, "respect_robots"), \
             patch.object(client.opener, "open", return_value=Response(b"test")), \
             self.assertRaises(collector.CollectionError):
            client.request("https://example.org/feed", 2)

    def test_private_dns_and_http_redirect_block(self):
        client = collector.PublicClient()
        with patch.object(collector.socket, "getaddrinfo", return_value=[(0, 0, 0, "", ("127.0.0.1", 443))]), self.assertRaises(collector.CollectionError):
            client.request("https://example.org/feed", 100)
        with self.assertRaises(collector.CollectionError):
            client.request("http://example.org/feed", 100)
        with self.assertRaises(collector.CollectionError):
            client.request("https://example.org/feed", 100, redirects=4)

    def test_hard_process_timeout_is_failed_coverage(self):
        with patch.object(collector.subprocess, "run", side_effect=subprocess.TimeoutExpired("collector", 60)):
            entries, status = collector.collect_bounded(SOURCE)
        self.assertEqual(entries, [])
        self.assertEqual(status["status"], "failed")
        self.assertIn("deadline", status["message"])

    def test_truncated_http_response_is_one_failed_collector(self):
        error = collector.http.client.IncompleteRead(b"", 10)
        with patch.object(collector.PublicClient, "request", side_effect=error):
            entries, status = collector.collect(SOURCE)
        self.assertEqual(entries, [])
        self.assertEqual(status["status"], "failed")
        self.assertIn("IncompleteRead", status["message"])


class SelectionTests(unittest.TestCase):
    def assemble(self, selected=None, statuses=None, previous=None, now=NOW):
        return refresh.assemble(previous or seed(), SETTINGS, SOURCES, results(selected, statuses), now, now + timedelta(seconds=5))

    def test_agent_product_exact_dots_feed_through_candidate_and_assembly(self):
        source = next(source for source in SOURCES if source["id"] == "openai")
        parsed = collector.parse_feed(DOTS_RSS, source)[0]
        self.assertEqual(parsed["excerpt"], DOTS_DESCRIPTION)
        self.assertEqual(parsed["rawDate"], "Tue, 29 Sep 2026 00:00:00 GMT")
        item, reason = refresh.candidate(parsed, source, NOW - timedelta(days=7), NOW)
        self.assertEqual(reason, "")
        self.assertEqual(item["title"], "Introducing dots")
        self.assertEqual(item["publishedAt"], "2026-09-29T00:00:00Z")
        self.assertEqual(item["url"], "https://openai.com/index/introducing-dots")
        self.assertEqual(item["sourceId"], "openai")
        self.assertEqual(item["topic"], "Agent products")
        self.assertEqual(item["matchedTerms"], ["introducing", "proactive assistants"])
        self.assertEqual(item["excerptLabel"], "Publisher excerpt")
        self.assertIn("Publisher-reported", item["evidence"])
        self.assertIn("does not establish web-search", item["caveat"])
        previous = self.assemble()
        state = self.assemble({"openai": [parsed]}, previous=previous)
        self.assertEqual(state["editions"][0]["items"], [item])
        self.assertEqual(state["editions"][0]["origin"], "collected")
        self.assertEqual(state["editions"][0]["outcome"], "stories")
        self.assertEqual(len(state["editions"]), len(previous["editions"]))
        self.assertEqual(state["editions"][0]["windowStart"], previous["editions"][0]["windowStart"])
        self.assertEqual(state["editions"][1], previous["editions"][1])

    def test_agent_product_rule_generalizes_to_other_first_party_publishers(self):
        source = next(source for source in COLLECTORS if source["kind"] == "Search specialist")
        synthetic = entry("Announcing Compass", "https://example.org/compass")
        synthetic["excerpt"] = "Compass is an always-on agent that follows up on multi-step tasks."
        state = self.assemble({source["id"]: [synthetic]})
        item = state["editions"][0]["items"][0]
        self.assertEqual(item["sourceId"], source["id"])
        self.assertNotEqual(item["sourceId"], "openai")
        self.assertEqual(item["topic"], "Agent products")
        self.assertEqual(item["matchedTerms"], ["always-on agent", "announcing"])
        for kind in ("Official", "Search specialist"):
            with self.subTest(kind=kind):
                self.assertEqual(collector.classify("Launching TaskPilot", "TaskPilot is an autonomous agent.", kind)[0], "Agent products")
                self.assertEqual(collector.classify("Introducing Beacon", "Beacon is an AI agent for complex tasks.", kind)[0], "Agent products")
                self.assertEqual(collector.classify("Introducing web search", "Grounded answers for AI agents.", kind)[0], "Web grounding")

    def test_agent_product_rule_rejects_generic_noise_and_untrusted_launches(self):
        positives = ("Introducing Compass", "Compass is a proactive assistant for complex tasks.")
        for kind in ("Newsletter", "Substack", "Analysis", "Medium", "Research", None):
            with self.subTest(kind=kind):
                self.assertIsNone(collector.classify(*positives, kind))
        negatives = (
            ("Introducing a new assistant", "An AI assistant answers your questions."),
            ("Introducing Nova", "A language model that powers autonomous agents."),
            ("Launching a new model", "Better performance for AI agents."),
            ("Introducing a coding agent", "A proactive assistant for coding tasks."),
            ("Introducing Compass: a tutorial", "Build an always-on agent with this tutorial."),
            ("How to launch your AI agent", "A practical guide to proactive assistants."),
            ("Announcing a funding round", "Investment in proactive assistants."),
            ("Introducing careers at Example", "We are hiring people to build AI agents."),
            ("Introducing enterprise RAG", "An autonomous agent for internal documents."),
            ("Introducing an internal-document assistant", "A proactive assistant for company documents."),
            ("Our proactive assistant gets faster", "An update for existing AI agents."),
            ("Introducing Compass", "A proactive assistant startup announces a funding round."),
        )
        for title, preview in negatives:
            with self.subTest(title=title, preview=preview):
                self.assertIsNone(collector.classify(title, preview, "Official"))

    def test_agent_product_dates_privacy_and_url_guards_still_apply(self):
        parsed = collector.parse_feed(DOTS_RSS, SOURCE)[0]
        for field, value, reason in (
            ("rawDate", "2026-10-01T00:00:00Z", "futureDate"),
            ("rawDate", "2026-09-01T00:00:00Z", "outsideWindow"),
            ("rawDate", "2026-09-29", "invalidDate"),
            ("rawUrl", "javascript:alert(1)", "invalidUrl"),
            ("excerpt", DOTS_DESCRIPTION + " Contact " + "inbox" + "@" + "example.invalid", "privacyGuard"),
        ):
            with self.subTest(field=field, value=value):
                item, rejection = refresh.candidate(dict(parsed, **{field: value}), SOURCE, NOW - timedelta(days=7), NOW)
                self.assertIsNone(item)
                self.assertEqual(rejection, reason)

    def test_agent_product_source_cap_same_day_dedupe_and_archives(self):
        dots = collector.parse_feed(DOTS_RSS, SOURCE)[0]
        others = [
            dict(entry("Introducing " + name, "https://example.org/" + name.lower(), published),
                 excerpt=name + " is a proactive assistant for complex tasks.")
            for name, published in (("Compass", "2026-09-28T00:00:00Z"), ("Beacon", "2026-09-27T00:00:00Z"))
        ]
        first = self.assemble({SOURCE["id"]: [dots, *others]})
        self.assertEqual(len(first["editions"][0]["items"]), 2)
        self.assertEqual(first["editions"][0]["items"][0]["title"], "Introducing dots")
        self.assertEqual(first["editions"][0]["coverage"][0]["rejected"]["sourceCap"], 1)
        duplicate = dict(dots, rawUrl=dots["rawUrl"] + "?utm_source=feed")
        second = self.assemble({SOURCE["id"]: [dots, duplicate, *others]}, previous=first)
        self.assertEqual(second["editions"][0]["items"], first["editions"][0]["items"])
        self.assertEqual(second["editions"][1], first["editions"][1])
        self.assertEqual(len(second["editions"]), len(first["editions"]))
        later = self.assemble({SOURCE["id"]: [dots, others[0]]}, previous=second, now=NOW + timedelta(days=2))
        self.assertEqual(later["editions"][0]["items"], [])
        self.assertEqual(later["editions"][1], second["editions"][0])

    def test_upstream_context_and_source_specialization_without_ai_keywords(self):
        examples = (
            ("New crawling endpoint", "Crawl public websites into structured data with fresher indexes.", "Official", "Web infrastructure"),
            ("Reranking now costs half as much", "Query pricing falls and retrieval freshness improves.", "Search specialist", "Web infrastructure"),
            ("Extracting structured data for agents", "An implementation guide extracts public pages into JSON for multi-step workflows.", "Medium", "Web infrastructure"),
            ("A connector for public filings", "MCP now connects tools to online sources with lower access costs.", "Analysis", "Web infrastructure"),
            ("Open-corpus evidence verification", "This study evaluates retrieval defenses against coordinated evidence poisoning.", "Research", "Evaluation"),
            ("Measuring a model's citation quality", "A benchmark tests attribution when agents retrieve fresh online sources.", "Research", "Evaluation"),
        )
        for index, (title, preview, kind, topic) in enumerate(examples):
            with self.subTest(title=title, kind=kind):
                source = next(source for source in COLLECTORS if source["kind"] == kind)
                record = dict(entry(title, f"https://example.org/upstream/{index}"), excerpt=preview)
                item, reason = refresh.candidate(record, source, NOW - timedelta(days=7), NOW)
                self.assertEqual(reason, "")
                self.assertEqual(item["topic"], topic)
                state = self.assemble({source["id"]: [record]})
                self.assertEqual(state["editions"][0]["items"], [item])
                for date, rejection in (("2026-09-01T00:00:00Z", "outsideWindow"), ("2026-10-01T00:00:00Z", "futureDate")):
                    self.assertEqual(refresh.candidate(dict(record, rawDate=date), source, NOW - timedelta(days=7), NOW)[1], rejection)
        title, preview, _, _ = examples[1]
        self.assertIsNone(collector.classify(title, preview, "Newsletter"))
        contextual = collector.classify(title, preview, "Search specialist")
        self.assertIn("search-specialist source context", contextual[1])

    def test_downstream_independent_applications_do_not_need_launch_or_search_keywords(self):
        examples = (
            ("Automating supplier diligence", "A multi-step workflow follows public filings and enriches company records for due diligence.", "Substack"),
            ("Enterprise RAG: a coding case study", "The workflow monitors public filings, extracts facts and synthesizes updates alongside internal company documents.", "Medium"),
            ("A travel-planning workflow", "A multi-step assistant compares live fares on airline websites then books an itinerary.", "Analysis"),
            ("Automated shopping comparison", "Agents visit online listings, compare stock across stores and choose a purchase.", "Newsletter"),
            ("Shopping with browser automation", "An autonomous workflow reads listing prices and compares availability before checkout.", "Analysis"),
            ("Competitor monitoring without manual checks", "An autonomous workflow monitors online prices and synthesizes changes for market intelligence.", "Medium"),
            ("Robust execution for complex web tasks", "This report presents an agent system using semantic webpage interactions for multi-step tasks.", "Research"),
        )
        for index, (title, preview, kind) in enumerate(examples):
            with self.subTest(title=title, kind=kind):
                source = next(source for source in SOURCES if source["kind"] == kind)
                record = dict(entry(title, f"https://example.org/applications/{index}"), excerpt=preview)
                # Reference-only sources are not collected; this source clone only exercises classification context.
                source = dict(source, feed="https://example.org/feed")
                item, reason = refresh.candidate(record, source, NOW - timedelta(days=7), NOW)
                self.assertEqual(reason, "")
                self.assertEqual(item["topic"], "Agentic applications")
                self.assertNotIn("web search", (title + " " + preview).lower())
                self.assertNotIn("agentic scale", (title + " " + preview).lower())
        independent = next(source for source in COLLECTORS if source["kind"] == "Medium")
        record = dict(entry(examples[1][0]), excerpt=examples[1][1])
        state = self.assemble({independent["id"]: [record]})
        self.assertEqual(state["editions"][0]["items"][0]["topic"], "Agentic applications")
        self.assertEqual(state["editions"][0]["items"][0]["sourceId"], independent["id"])

    def test_contextual_rules_reject_generic_ai_seo_coding_and_internal_only_items(self):
        examples = (
            ("AI for search engine optimization", "An automated SEO guide to keyword rankings and organic traffic.", "Medium"),
            ("New retrieval model", "Dense retrieval benchmarks for internal documents.", "Research"),
            ("Updating our crawler", "New retrieval support for internal company documents only.", "Search specialist"),
            ("Building an agent", "A generic tutorial about tool calls and memory.", "Medium"),
            ("AI assistants for customer calls", "Autonomous agents handle voice, chat and web support.", "Official"),
            ("Introducing a cheaper frontier model", "Better coding and generic agent tasks at a lower API price.", "Official"),
            ("Our new website", "Use an AI assistant to generate CSS and web components.", "Analysis"),
            ("Automating enterprise RAG", "Agents retrieve only internal company documents.", "Official"),
            ("Introducing a shopping assistant", "A generic chatbot suggests travel ideas from its training data.", "Official"),
            ("Funding the next wave of AI agents", "We raised a funding round to hire builders.", "Official"),
            ("Keeping websites online", "A dashboard charts uptime, CPU load and memory.", "Analysis"),
        )
        for title, preview, kind in examples:
            with self.subTest(title=title, kind=kind):
                self.assertIsNone(collector.classify(title, preview, kind))

    def test_contextual_topics_share_source_caps_and_existing_snapshot_dedupe(self):
        records = [
            dict(entry("New public data extraction", "https://example.org/upstream"), excerpt="This release extracts structured data from public websites."),
            dict(entry("A market intelligence workflow", "https://example.org/downstream"), excerpt="Agents synthesize public filings into company research."),
            dict(entry("Introducing Compass", "https://example.org/product"), excerpt="Compass is a proactive assistant for complex tasks."),
        ]
        first = self.assemble({SOURCE["id"]: records})
        self.assertEqual(len(first["editions"][0]["items"]), 2)
        self.assertEqual(first["editions"][0]["coverage"][0]["rejected"]["sourceCap"], 1)
        second = self.assemble({SOURCE["id"]: records}, previous=first)
        self.assertEqual(second["editions"][0]["items"], first["editions"][0]["items"])
        self.assertEqual(second["editions"][1], first["editions"][1])
        self.assertEqual(len(second["editions"]), len(first["editions"]))

    def test_fresh_window_rejects_future_invalid_old_and_replacements(self):
        entries = [entry()]
        for published in ("2026-10-01T00:00:00Z", "2026-09-01T00:00:00Z", "bad", "2026-09-29"):
            entries.append(entry("Web research " + published, "https://example.org/" + str(len(entries)), published))
        replacement = entry("Deep research paper replaced", "https://example.org/replacement")
        replacement["announceType"] = "replace"
        entries.append(replacement)
        state = self.assemble({SOURCE["id"]: entries})
        issue = state["editions"][0]
        self.assertEqual(len(issue["items"]), 1)
        rejected = issue["coverage"][0]["rejected"]
        self.assertEqual(rejected, {"futureDate": 1, "outsideWindow": 1, "invalidDate": 2, "notNewPreprint": 1})

    def test_no_topic_match_in_hidden_full_feed_text(self):
        item = entry("A broad model update")
        item["excerpt"] = collector.excerpt("General model capabilities. " * 20 + "Deep research and web search.")
        self.assertIsNone(refresh.candidate(item, SOURCE, NOW - timedelta(days=7), NOW)[0])

    def test_contact_details_are_not_saved_as_public_excerpts(self):
        item = entry()
        item["excerpt"] += " Contact " + "inbox" + "@" + "example.invalid"
        story, reason = refresh.candidate(item, SOURCE, NOW - timedelta(days=7), NOW)
        self.assertIsNone(story)
        self.assertEqual(reason, "privacyGuard")

    def test_dedupe_url_title_and_archive(self):
        entries = [
            entry(),
            entry("Different web research title", "https://example.org/news/?utm_source=feed"),
            entry("WEB search for research agents!", "https://example.org/duplicate-title"),
            entry("Exa web research announcement", "https://exa.ai/blog/exa-agent-ultra"),
        ]
        state = self.assemble({SOURCE["id"]: entries})
        self.assertEqual(len(state["editions"][0]["items"]), 1)
        state2 = self.assemble({SOURCE["id"]: entries}, previous=state, now=NOW + timedelta(days=2))
        self.assertEqual(state2["editions"][0]["items"], [])
        self.assertEqual(state2["editions"][1], state["editions"][0])

    def test_source_diversity_and_edition_cap(self):
        entries = {
            source["id"]: [entry(f"Web research {source['id']} {number}", f"https://example.org/{source['id']}/{number}") for number in range(4)]
            for source in COLLECTORS[:3]
        }
        state = self.assemble(entries)
        items = state["editions"][0]["items"]
        self.assertEqual(len(items), 6)
        settings = copy.deepcopy(SETTINGS)
        settings["selection"]["maxItems"] = 3
        state = refresh.assemble(seed(), settings, SOURCES, results(entries), NOW, NOW)
        self.assertEqual(len(state["editions"][0]["items"]), 3)

    def test_same_ist_date_is_idempotent_and_preserves_items(self):
        first = self.assemble({SOURCE["id"]: [entry()]}, now=NOW - timedelta(hours=3))
        second = self.assemble({SOURCE["id"]: [entry()]}, previous=first)
        self.assertEqual(len(second["editions"]), 2)
        self.assertEqual(second["editions"][0]["items"], first["editions"][0]["items"])
        self.assertEqual(second["editions"][0]["windowStart"], first["editions"][0]["windowStart"])
        third = self.assemble({SOURCE["id"]: []}, previous=second, now=NOW + timedelta(hours=1))
        self.assertEqual(third["editions"][0]["items"], first["editions"][0]["items"])
        late_utc = datetime(2026, 9, 30, 20, 0, tzinfo=collector.UTC)
        next_day = self.assemble(previous=third, now=late_utc)
        self.assertEqual(next_day["currentEditionId"], "2026-10-01")

    def test_quiet_partial_and_zero_success_are_distinct(self):
        partial = self.assemble(statuses={SOURCE["id"]: "blocked"})
        issue = partial["editions"][0]
        self.assertEqual(issue["outcome"], "quiet")
        self.assertEqual(issue["coverage"][0]["status"], "blocked")
        self.assertIsNotNone(partial["lastSuccessfulRefresh"])
        with self.assertRaisesRegex(collector.CollectionError, "Zero successful"):
            self.assemble(statuses={source["id"]: "failed" for source in COLLECTORS})

    def test_seed_qualifications_remain_unchanged(self):
        state = self.assemble()
        original = seed()["editions"][0]
        self.assertEqual(state["editions"][1], original)
        self.assertIn("vendor-reported", original["items"][0]["caveat"])
        self.assertIsNone(original["items"][1]["publishedAt"])
        self.assertIn("13 September", original["items"][1]["caveat"])
        self.assertEqual(SETTINGS["audienceVerifiedAt"], "2026-09-29")


class BuildAndPreservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("config", "web"):
            shutil.copytree(build.ROOT / name, self.root / name)
        self.initial = seed()
        self.page = build.render(self.initial, SETTINGS, SOURCES, self.root)
        build.publish_files(self.initial, self.page, self.root)
        self.originals = {name: self.root.joinpath(*name.split("/")).read_bytes() for name in build.OUTPUTS}
        quiet = patch("builtins.print")
        quiet.start()
        self.addCleanup(quiet.stop)

    def assert_preserved(self):
        for name, content in self.originals.items():
            self.assertEqual(self.root.joinpath(*name.split("/")).read_bytes(), content)

    def fake(self, source):
        entries = [entry()] if source["id"] == SOURCE["id"] else []
        return entries, health(source, count=len(entries))

    def test_refresh_then_repeat_preserves_archive_without_duplicate(self):
        first = refresh.refresh(self.root, self.fake, lambda: NOW)
        second = refresh.refresh(self.root, self.fake, lambda: NOW)
        self.assertEqual(len(first["editions"]), len(second["editions"]))
        self.assertEqual(first["editions"][0]["items"], second["editions"][0]["items"])
        self.assertEqual(second["editions"][0]["coverage"][0]["selected"], 0)
        page = (self.root / "site" / "index.html").read_text(encoding="utf-8")
        self.assertEqual(page, build.render(second, SETTINGS, SOURCES, self.root))
        self.assertEqual(second["editions"][1], self.initial["editions"][0])

    def test_zero_success_and_failed_build_do_not_replace_good_files(self):
        with self.assertRaises(collector.CollectionError):
            refresh.refresh(self.root, lambda source: ([], health(source, "failed")), lambda: NOW)
        self.assert_preserved()
        (self.root / "web" / "template.html").write_text("broken template", encoding="utf-8")
        with self.assertRaises(build.BuildError):
            refresh.refresh(self.root, self.fake, lambda: NOW)
        self.assert_preserved()
        self.assertFalse((self.root / ".refresh.lock").exists())

    def test_atomic_write_failure_rolls_back_both_files(self):
        real_write = build.atomic_write
        failed = False

        def failing_write(path, content):
            nonlocal failed
            if path.name == "index.html" and not failed:
                failed = True
                raise OSError("Synthetic write failure")
            return real_write(path, content)

        with patch.object(build, "atomic_write", side_effect=failing_write), self.assertRaises(OSError):
            build.publish_files(dict(self.initial, extra="new"), "new reader", self.root)
        self.assert_preserved()
        self.assertFalse((self.root / build.JOURNAL).exists())

    def test_concurrent_lock_rejects_and_interrupted_journal_recovers(self):
        with build.workspace_lock(self.root):
            with self.assertRaises(build.BuildError):
                with build.workspace_lock(self.root):
                    self.fail("Lock should not be reentrant")
        import base64
        journal = {name: base64.b64encode(content).decode("ascii") for name, content in self.originals.items()}
        (self.root / build.JOURNAL).write_text(json.dumps(journal), encoding="utf-8")
        (self.root / "site" / "index.html").write_text("interrupted output", encoding="utf-8")
        with build.workspace_lock(self.root):
            self.assert_preserved()
        self.assertFalse((self.root / build.JOURNAL).exists())

    def test_embedded_json_escapes_terminators_and_remains_exact(self):
        value = "</script><script>alert('x')</script>&\u2028\u2029"
        text = build.embedded_json({"title": value})
        self.assertNotIn("<", text)
        self.assertNotIn("&", text)
        self.assertNotIn("\u2028", text)
        self.assertEqual(json.loads(text)["title"], value)
        self.assertIn("script-src &#x27;sha256-", self.page)
        self.assertIn("connect-src &#x27;none&#x27;", self.page)
        state = seed()
        state["editions"][0]["items"][0]["title"] = "Literal @@CANONICAL@@ and @@DATA@@ tokens"
        page = build.render(state, SETTINGS, SOURCES, self.root)
        self.assertIn("Literal @@CANONICAL@@ and @@DATA@@ tokens", page)

    def test_validation_rejects_untrusted_links_duplicate_and_bad_fresh_dates(self):
        state = refresh.assemble(seed(), SETTINGS, SOURCES, results({SOURCE["id"]: [entry()]}), NOW, NOW)
        for field, value in (("url", "javascript:alert(1)"), ("publishedAt", "2030-01-01T00:00:00Z"), ("excerpt", "x" * 241)):
            broken = copy.deepcopy(state)
            broken["editions"][0]["items"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                build.render(broken, SETTINGS, SOURCES, self.root)
        state["editions"].append(copy.deepcopy(state["editions"][0]))
        with self.assertRaises(build.BuildError):
            build.validate_state(state, SETTINGS, SOURCES)

    def test_privacy_guard_uses_generic_examples(self):
        self.assertTrue(problems("data/sample.json", "gh" + "p_" + "x" * 40))
        self.assertTrue(problems("data/sample.json", "private" + "@" + "example.invalid"))
        self.assertTrue(problems("site/index.html", "mail" + "to:" + "inbox"))
        self.assertTrue(problems("data/sample.json", "C:" + "\\" + "Users" + "\\" + "sample"))
        self.assertEqual(problems("web/app.js", "Web search and public sources."), [])


if __name__ == "__main__":
    unittest.main()
