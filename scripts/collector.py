"""Bounded, unauthenticated public RSS/Atom collection. No article scraping."""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import math
import re
import socket
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

UTC = timezone.utc
USER_AGENT = "GroundingWeekly/1.0 (+https://github.com/charanm86/grounding-weekly)"
MAX_FEED_BYTES = 4 * 1024 * 1024
MAX_ROBOTS_BYTES = 512 * 1024
SOURCE_SECONDS = 45
SOCKET_SECONDS = 10
MAX_ENTRIES = 2000
EXCERPT_CHARS = 220
TRACKING_KEYS = {"fbclid", "gclid", "dclid", "mc_cid", "mc_eid"}
ATOM = "{http://www.w3.org/2005/Atom}"
DC = "{http://purl.org/dc/elements/1.1/}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}"
ARXIV = "{http://arxiv.org/schemas/atom}"


class CollectionError(ValueError):
    """Expected public-source failure, safe to report without credentials."""


class RobotsDenied(CollectionError):
    pass


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_date(value: str | None) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    value = value.strip()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (ValueError, TypeError, OverflowError):
            return None
    if parsed.tzinfo is None:
        return None
    try:
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def safe_url(value: str, base: str | None = None) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        raise CollectionError("Missing public URL.")
    value = value.strip()
    if re.search(r"[\x00-\x20\x7f\\]", value):
        raise CollectionError("URL contains whitespace, controls or a backslash.")
    try:
        parts = urllib.parse.urlsplit(urllib.parse.urljoin(base, value) if base else value)
        host = (parts.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
        port = parts.port
    except (ValueError, UnicodeError) as error:
        raise CollectionError("Malformed URL.") from error
    if parts.scheme not in {"http", "https"} or not host or parts.username or parts.password:
        raise CollectionError("Only unauthenticated HTTP(S) URLs are allowed.")
    if port not in {None, 80, 443}:
        raise CollectionError("Nonstandard public-web port.")
    try:
        ipaddress.ip_address(host)
    except ValueError as error:
        if (
            "." not in host or host.endswith((".localhost", ".local", ".internal"))
            or re.fullmatch(r"(?:0x[0-9a-f]+|\d+)(?:\.(?:0x[0-9a-f]+|\d+)){0,3}", host, re.I)
        ):
            raise CollectionError("Nonpublic host is not allowed.") from error
    else:
        raise CollectionError("Source URLs must use publisher domains, not literal IP addresses.")
    authority = f"[{host}]" if ":" in host else host
    if port and not ((parts.scheme == "https" and port == 443) or (parts.scheme == "http" and port == 80)):
        authority += f":{port}"
    return urllib.parse.urlunsplit((parts.scheme, authority, parts.path or "/", parts.query, ""))


def canonical_url(value: str, base: str | None = None) -> str:
    parts = urllib.parse.urlsplit(safe_url(value, base))
    query = [
        (key, val) for key, val in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_KEYS
        and not (key.lower() == "source" and val.startswith("rss"))
    ]
    path = parts.path.rstrip("/") or "/"
    scheme = parts.scheme
    if parts.hostname in {"arxiv.org", "www.arxiv.org"}:
        scheme = "https"
        path = re.sub(r"(\/abs\/\d{4}\.\d{4,5})v\d+$", r"\1", path)
    return urllib.parse.urlunsplit((scheme, parts.netloc, path, urllib.parse.urlencode(sorted(query)), ""))


class PlainText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in {"script", "style", "noscript"}:
            self.ignored += 1
        elif tag in {"p", "br", "div", "li", "h1", "h2", "h3", "blockquote"} and not self.ignored:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"}:
            self.ignored = max(0, self.ignored - 1)
        elif not self.ignored:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.ignored:
            self.parts.append(data)


def plain_text(value: str) -> str:
    parser = PlainText()
    parser.feed(value)
    text = "".join(parser.parts)
    return " ".join("".join(char for char in text if char.isprintable() or char.isspace()).split())


def excerpt(value: str, limit: int = EXCERPT_CHARS) -> str:
    text = plain_text(value)
    if len(text) <= limit:
        return text
    cut = text[:limit - 3]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:") + "..."


def title_key(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.sub(r"[^\w]+", " ", text).split())


TOPICS = (
    ("Deep research", (
        r"\bdeep[- ]research\b", r"\bresearch agents?\b", r"\bagentic research\b",
        r"\bdeepsearchqa\b", r"\bweb research\b",
    )),
    ("Agentic search", (
        r"\bagentic search\b", r"\bsearch agents?\b", r"\bai search\b",
        r"\bsearch[- ]augmented\b", r"\bsearch[- ]enabled\b",
    )),
    ("Web grounding", (
        r"\bweb[- ]ground(?:ing|ed)\b", r"\bgrounding (?:with|in|via) (?:google|bing|web|search)\b",
        r"\bground(?:ed|ing) (?:answers?|responses?)\b",
        r"\bweb search\b", r"\bsearch the web\b", r"\bsearch engine\b",
    )),
    ("Web access", (
        r"\b(?:live|public|open)[- ]web\b", r"\bweb (?:retrieval|extraction|access|browsing)\b",
        r"\bsearch api\b",
    )),
)
EVALUATION = re.compile(
    r"\b(?:benchmarks?|evaluat\w*|citation\w*|attribution|faithfulness|verification|"
    r"fact[- ]checking|evidence poisoning|source quality)\b", re.I
)
AI_CONTEXT = re.compile(
    r"\b(?:ai|llms?|language models?|agents?|ground(?:ing|ed)|research|api|citations?|"
    r"rag|retrieval[- ]augmented generation)\b", re.I
)
OFF_TOPIC_TITLE = re.compile(
    r"\b(?:funding|fundrais\w*|series [a-f]|we.re hiring|job openings?|"
    r"internal[- ]document|enterprise[- ]rag)\b", re.I
)
FIRST_PARTY_KINDS = {"Official", "Search specialist"}
EXTERNAL_INFORMATION = re.compile(
    r"\b(?:websites?|webpages?|internet|open[- ](?:web|corpus)|"
    r"(?:public|external)[- ](?:web|sources?|pages?|sites?|filings)|"
    r"web[- ](?:data|information|content|sources?|tasks?|connectors?)|"
    r"browser[- ](?:agents?|automation|navigation|tasks?)|"
    r"online[- ](?:sources?|listings?|prices?|stores?|information)|"
    r"live[- ](?:web|prices?|fares|listings?|news)|news feeds?)\b", re.I
)
UPSTREAM_CAPABILITY = re.compile(
    r"\b(?:crawl(?:ers?|ing)?|scrap(?:e[sd]?|ers?|ing)|index(?:es|ing)?|"
    r"retriev(?:al|e[sd]?|ers?|ing)|rerank(?:ers?|ing)?|extract(?:s|ed|ion|ors?|ing)?|"
    r"structured (?:web )?data|connectors?|tool protocols?|model context protocol|mcp|"
    r"(?:search|grounding|extraction|retrieval)[- ]apis?)\b", re.I
)
AGENT_WORKFLOW = re.compile(
    r"\b(?:agents?|agentic|autonomous(?:ly)?|automate[sd]?|automation|"
    r"multi[- ]step|workflows?|orchestrat\w*|proactive|always[- ]on)\b", re.I
)
DOWNSTREAM_APPLICATION = re.compile(
    r"\b(?:research|synthesi[sz]\w*|enrich\w*|due diligence|(?:market|news|competitive) intelligence|"
    r"(?:web|news|price|competitor|company)[- ]monitoring|"
    r"monitor(?:s|ing)? (?:news|prices?|sources?|companies|competitors)|"
    r"shopping|travel|itinerar(?:y|ies)|fares|web tasks?|browser automation)\b", re.I
)
MATERIAL_WORK = re.compile(
    r"\b(?:new|now|introduc\w*|announc\w*|launch\w*|releas\w*|improv\w*|reduc\w*|"
    r"support\w*|updat\w*|integrat\w*|pric\w*|cost\w*|access|freshness|"
    r"benchmark\w*|evaluat\w*|verif\w*|research|study|report|case study|"
    r"build\w*|implement\w*|test\w*|guide|tutorial)\b", re.I
)
INTERNAL_CONTEXT = re.compile(
    r"\b(?:internal[- ](?:documents?|knowledge|data|sources?)|company documents?|"
    r"enterprise[- ]rag)\b", re.I
)
SEO_NOISE = re.compile(r"\b(?:seo|search[- ]engine optimization|organic traffic|keyword rankings?)\b", re.I)
PRODUCT_LAUNCH = re.compile(
    r"\b(?:introducing|announcing|launch(?:es|ed|ing)?|unveil(?:s|ed|ing)?|releas(?:e[sd]?|ing))\b", re.I
)
AGENT_PRODUCT = re.compile(
    r"\b(?:(?:ai|autonomous) agents?|(?:proactive|always[- ]on) (?:agents?|assistants?))\b", re.I
)
AGENT_PRODUCT_NOISE = re.compile(
    r"\b(?:models?|llms?|coding|code[- ](?:generation|assistants?)|tutorials?|guides?|"
    r"how[- ]to|cookbooks?|workshops?|webinars?|courses?|hiring|careers?|"
    r"benchmarks?|evaluations?|datasets?|sdks?|frameworks?|"
    r"internal[- ](?:documents?|knowledge)|enterprise[- ](?:rag|search)|rag)\b", re.I
)


def classify(title: str, preview: str, source_kind: str | None = None) -> tuple[str, list[str]] | None:
    """Only title + the short public excerpt count, never hidden full-feed text."""
    text = f"{title} {preview}"
    external = EXTERNAL_INFORMATION.search(text)
    capability = UPSTREAM_CAPABILITY.search(text)
    workflow = AGENT_WORKFLOW.search(text)
    application = DOWNSTREAM_APPLICATION.search(text)
    assessment = EVALUATION.search(text)
    internal_only = INTERNAL_CONTEXT.search(text) and not external
    seo = SEO_NOISE.search(text)
    matches = []
    first_topic = None
    if external and workflow and application:
        first_topic = "Agentic applications"
        matches = [match.group().lower() for match in (external, workflow, application)]
    elif not seo and external and assessment and (capability or AI_CONTEXT.search(text)):
        first_topic = "Evaluation"
        matches = [match.group().lower() for match in (external, assessment, capability) if match]
    elif not seo and not internal_only and capability and MATERIAL_WORK.search(text) and (
        external or source_kind == "Search specialist"
    ):
        first_topic = "Web infrastructure"
        matches = [capability.group().lower(), external.group().lower() if external else "search-specialist source context"]
    elif not seo and not internal_only and not OFF_TOPIC_TITLE.search(title) and AI_CONTEXT.search(text):
        for topic, patterns in TOPICS:
            for pattern in patterns:
                match = re.search(pattern, text, re.I)
                if match:
                    first_topic = first_topic or topic
                    matches.append(match.group().lower())
    if matches:
        if assessment:
            matches.append(assessment.group().lower())
        topic = "Evaluation" if assessment else first_topic
        return topic, sorted(set(matches))
    if source_kind in FIRST_PARTY_KINDS and not OFF_TOPIC_TITLE.search(text) and not seo and not AGENT_PRODUCT_NOISE.search(text):
        launch, agent = PRODUCT_LAUNCH.search(title), AGENT_PRODUCT.search(text)
        if launch and agent:
            return "Agent products", sorted({launch.group().lower(), agent.group().lower()})
    return None


def node_text(node: ET.Element, names: tuple[str, ...]) -> str:
    for name in names:
        child = node.find(name)
        if child is not None:
            if list(child):
                return ET.tostring(child, encoding="unicode", method="html")
            if child.text:
                return child.text.strip()
    return ""


class FeedTreeBuilder(ET.TreeBuilder):
    def doctype(self, name, public_id, system_id):
        raise CollectionError("DTD/entity declarations are not accepted.")


def parse_feed(raw: bytes, source: dict) -> list[dict]:
    if len(raw) > MAX_FEED_BYTES:
        raise CollectionError("Feed exceeds the byte limit.")
    if re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", raw, re.I):
        raise CollectionError("DTD/entity declarations are not accepted.")
    try:
        root = ET.fromstring(raw, parser=ET.XMLParser(target=FeedTreeBuilder()))
    except (ET.ParseError, LookupError) as error:
        raise CollectionError("Response is not a valid RSS/Atom document.") from error
    atom = root.tag == ATOM + "feed"
    if atom:
        nodes = root.findall(ATOM + "entry")
    elif root.tag == "rss" and root.find("channel") is not None:
        nodes = root.findall("channel/item")
    else:
        raise CollectionError("Response is not a supported RSS 2.0 or Atom feed.")
    if len(nodes) > MAX_ENTRIES:
        raise CollectionError(f"Feed exceeds {MAX_ENTRIES} entries.")
    entries = []
    for node in nodes:
        title = plain_text(node_text(node, (ATOM + "title",) if atom else ("title",)))[:300]
        raw_excerpt = node_text(node, (ATOM + "summary", ATOM + "content") if atom else ("description", CONTENT + "encoded"))
        raw_date = node_text(node, (ATOM + "published",) if atom else ("pubDate", DC + "date"))
        link = ""
        if atom:
            for candidate in node.findall(ATOM + "link"):
                if candidate.get("rel", "alternate") == "alternate" and candidate.get("type", "text/html") in {"text/html", "application/xhtml+xml"}:
                    link = candidate.get("href", "")
                    break
        else:
            link = node_text(node, ("link",))
            guid = node.find("guid")
            if not link and guid is not None and guid.get("isPermaLink", "true") == "true":
                link = guid.text or ""
        entries.append({
            "title": title,
            "excerpt": excerpt(raw_excerpt),
            "rawDate": raw_date,
            "rawUrl": link,
            "announceType": node_text(node, (ARXIV + "announce_type",)),
        })
    return entries


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        return None


def robots_path(value: str) -> str:
    unreserved = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~"
    value = re.sub(
        r"%([0-9a-fA-F]{2})",
        lambda match: chr(int(match[1], 16)) if chr(int(match[1], 16)) in unreserved else "%" + match[1].upper(),
        value,
    )
    return urllib.parse.quote(value, safe="/?*=$&:+,;@!()[]%-._~")


class RobotsPolicy:
    """Publisher rules with longest-match precedence and RFC 9309 wildcards."""

    def __init__(self, text: str) -> None:
        groups = []
        agents, directives = [], []
        for line in text.splitlines():
            key, separator, value = line.partition("#")[0].partition(":")
            if not separator:
                continue
            key, value = key.strip().lower(), value.strip()
            if key == "user-agent":
                if directives:
                    groups.append((agents, directives))
                    agents, directives = [], []
                agents.append(value.lower())
            elif agents and key in {"allow", "disallow", "crawl-delay", "request-rate"}:
                directives.append((key, value))
        if agents:
            groups.append((agents, directives))
        product = USER_AGENT.split("/", 1)[0].lower()
        selected, best = [], -1
        for agents, directives in groups:
            specificity = max(
                (0 if agent == "*" else len(agent) for agent in agents if agent == "*" or (agent and agent in product)),
                default=-1,
            )
            if specificity > best:
                selected, best = list(directives), specificity
            elif specificity == best and specificity >= 0:
                selected.extend(directives)
        self.rules = []
        self.delay = 0.0
        for key, value in selected:
            if key in {"allow", "disallow"} and value:
                pattern = robots_path(value)
                end = pattern.endswith("$")
                body = pattern[:-1] if end else pattern
                expression = "^" + re.escape(body).replace(r"\*", ".*") + ("$" if end else "")
                self.rules.append((len(body.replace("*", "")), key == "allow", re.compile(expression)))
            elif key in {"crawl-delay", "request-rate"}:
                try:
                    if key == "crawl-delay":
                        delay = float(value)
                    else:
                        requests, seconds = value.split("/", 1)
                        delay = float(seconds) / float(requests)
                    if not math.isfinite(delay) or delay < 0:
                        raise ValueError("Invalid publisher delay.")
                except (ValueError, ZeroDivisionError) as error:
                    raise RobotsDenied("Publisher request-rate rule could not be interpreted safely.") from error
                self.delay = max(self.delay, delay)

    def allows(self, url: str) -> bool:
        parts = urllib.parse.urlsplit(url)
        target = robots_path((parts.path or "/") + ("?" + parts.query if parts.query else ""))
        matches = [(length, allowed) for length, allowed, pattern in self.rules if pattern.search(target)]
        return max(matches)[1] if matches else True


class PublicClient:
    def __init__(self) -> None:
        self.deadline = time.monotonic() + SOURCE_SECONDS
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.robots: dict[str, RobotsPolicy] = {}
        self.robots_notes: list[str] = []
        self.last_request: dict[str, float] = {}

    def remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise CollectionError("Source time budget exceeded.")
        return remaining

    def request(self, url: str, limit: int, redirects: int = 0, check_robots: bool = True) -> bytes:
        url = safe_url(url)
        if not url.startswith("https://"):
            raise CollectionError("Collectors require HTTPS; no insecure redirect is followed.")
        if redirects > 3:
            raise CollectionError("Too many redirects.")
        host = urllib.parse.urlsplit(url).hostname
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(address[4][0]).is_global for address in addresses):
            raise CollectionError("DNS returned a nonpublic address.")
        if check_robots:
            self.respect_robots(url)
        request = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, text/plain;q=0.8",
            "Accept-Encoding": "identity",
        })
        origin = urllib.parse.urlsplit(url).netloc
        self.last_request[origin] = time.monotonic()
        try:
            response = self.opener.open(request, timeout=min(SOCKET_SECONDS, self.remaining()))
        except urllib.error.HTTPError as error:
            if error.code in {301, 302, 303, 307, 308} and error.headers.get("Location"):
                destination = safe_url(error.headers["Location"], url)
                error.close()
                return self.request(destination, limit, redirects + 1, check_robots)
            error.close()
            raise
        with response:
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise CollectionError("Unexpected compressed response; byte bound cannot be verified.")
            length = response.headers.get("Content-Length")
            if length and int(length) > limit:
                raise CollectionError("Response exceeds the byte limit.")
            chunks = []
            total = 0
            while True:
                self.remaining()
                chunk = response.read1(min(65536, limit - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise CollectionError("Response exceeds the byte limit.")
                chunks.append(chunk)
            return b"".join(chunks)

    def respect_robots(self, url: str) -> None:
        parts = urllib.parse.urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self.robots:
            try:
                body = self.request(origin + "/robots.txt", MAX_ROBOTS_BYTES, check_robots=False)
            except urllib.error.HTTPError as error:
                if error.code in {404, 410}:
                    body = b""
                    self.robots_notes.append("No robots.txt published (HTTP 404/410).")
                else:
                    raise RobotsDenied(f"robots.txt unavailable (HTTP {error.code}); collection skipped.") from error
            except (OSError, CollectionError) as error:
                raise RobotsDenied("robots.txt could not be verified; collection skipped.") from error
            text = body.decode("utf-8-sig", errors="replace")
            if "<html" in text.lower() or "<!doctype html" in text.lower():
                raise RobotsDenied("robots.txt returned HTML; collection skipped.")
            self.robots[origin] = RobotsPolicy(text)
        policy = self.robots[origin]
        if not policy.allows(url):
            raise RobotsDenied("Disallowed by the publisher's robots.txt.")
        delay = policy.delay
        elapsed = time.monotonic() - self.last_request.get(parts.netloc, 0)
        if delay > elapsed:
            wait = delay - elapsed
            if wait >= self.remaining():
                raise RobotsDenied("Publisher crawl delay exceeds the source time budget.")
            time.sleep(wait)


def collect(source: dict) -> tuple[list[dict], dict]:
    health = {
        "sourceId": source["id"],
        "checkedAt": iso(datetime.now(UTC)),
        "status": "failed",
        "entries": 0,
        "eligible": 0,
        "selected": 0,
        "rejected": {},
    }
    client = PublicClient()
    try:
        raw = client.request(source["feed"], MAX_FEED_BYTES)
        entries = parse_feed(raw, source)
    except RobotsDenied as error:
        health.update(status="blocked", message=str(error))
    except urllib.error.HTTPError as error:
        health["message"] = f"Feed returned HTTP {error.code}; no access-control workaround attempted."
    except (OSError, http.client.HTTPException, CollectionError, ValueError) as error:
        health["message"] = f"{type(error).__name__}: {str(error)[:180]}"
    else:
        health.update(status="ok", entries=len(entries), bytes=len(raw), message="Public feed retrieved and parsed.")
        if client.robots_notes:
            health["robotsNote"] = " ".join(client.robots_notes)
        return entries, health
    return [], health


def story_id(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def collect_bounded(source: dict) -> tuple[list[dict], dict]:
    # A subprocess also bounds DNS resolution and pathological XML processing.
    try:
        result = subprocess.run(
            [sys.executable, "-m", "scripts.collector"],
            input=json.dumps(source), text=True, encoding="utf-8",
            capture_output=True, timeout=SOURCE_SECONDS + 15, check=True,
        )
    except subprocess.TimeoutExpired:
        return [], {
            "sourceId": source["id"], "checkedAt": iso(datetime.now(UTC)),
            "status": "failed", "entries": 0, "eligible": 0, "selected": 0, "rejected": {},
            "message": "Collector exceeded its 60-second hard deadline.",
        }
    except subprocess.CalledProcessError as error:
        raise CollectionError(f"Collector process for {source['id']} failed; refresh aborted.") from error
    payload = json.loads(result.stdout)
    return payload["entries"], payload["health"]


if __name__ == "__main__":
    entries, health = collect(json.load(sys.stdin))
    print(json.dumps({"entries": entries, "health": health}, ensure_ascii=True))
