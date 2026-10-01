"""Validate the public source of truth and build one offline-capable HTML reader."""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import os
import re
import sys
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from scripts.collector import canonical_url, parse_date, safe_url, title_key
from scripts.summaries import validate_summary

ROOT = Path(__file__).resolve().parent.parent
OUTPUTS = ("data/state.json", "site/index.html")
JOURNAL = ".refresh-transaction.json"
THEME_SCRIPT = """\n    (() => {
      const param = new URLSearchParams(window.location.search).get("clawpilotTheme");
      const theme =
        param || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
      document.documentElement.setAttribute("data-theme", theme);
    })();
  """


class BuildError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise BuildError(message)


def read_json(path: Path):
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def configuration(root: Path = ROOT) -> tuple[dict, list[dict]]:
    settings = read_json(root / "config" / "site.json")
    sources = read_json(root / "config" / "sources.json")
    require(isinstance(sources, list) and bool(sources), "The source catalog is empty.")
    ids = [source["id"] for source in sources]
    require(len(set(ids)) == len(ids), "Duplicate source identifiers.")
    for source in sources:
        require(bool(re.fullmatch(r"[a-z0-9-]+", source["id"])), "Invalid source identifier.")
        for key in ("name", "kind", "purpose"):
            require(isinstance(source.get(key), str) and bool(source[key]), f"Source missing {key}.")
        safe_url(source["url"])
        if source.get("feed"):
            require(safe_url(source["feed"]).startswith("https://"), "Feed must use HTTPS.")
        else:
            require(bool(source.get("referenceReason")), "Reference-only source needs an explanation.")
    for key in ("url", "repository", "workflow"):
        require(safe_url(settings[key]).startswith("https://"), "Site links must use HTTPS.")
    require(settings["schedule"]["cron"] == "30 3 * * 5", "Friday 03:30 UTC schedule changed unexpectedly.")
    require(settings["schedule"]["timezone"] == "Asia/Kolkata", "Unexpected schedule timezone.")
    require(settings["schedule"]["graceHours"] == 24, "Staleness grace must be 24 hours.")
    limits = settings["selection"]
    require(1 <= limits["windowDays"] <= 7, "Window must be at most seven days.")
    require(1 <= limits["maxPerSource"] <= 3, "Invalid source diversity cap.")
    require(1 <= limits["maxItems"] <= 20, "Invalid edition size.")
    date.fromisoformat(settings["audienceVerifiedAt"])
    return settings, sources


def validate_state(state: dict, settings: dict, sources: list[dict]) -> None:
    require(state.get("schemaVersion") == 2, "Unsupported data schema.")
    require(isinstance(state.get("editions"), list) and bool(state["editions"]), "No editions.")
    source_ids = {source["id"] for source in sources}
    ids = [edition["id"] for edition in state["editions"]]
    require(len(ids) == len(set(ids)), "Duplicate editions.")
    require(state["currentEditionId"] in ids, "Current edition is missing.")
    seen_urls, seen_titles = set(), set()
    for edition in state["editions"]:
        require(edition["id"] == date.fromisoformat(edition["date"]).isoformat(), "Edition identifier must be its date.")
        require(edition["origin"] in {"curated", "collected"}, "Unknown edition origin.")
        require(isinstance(edition["items"], list), "Missing edition items.")
        coverage = edition["coverage"]
        require(isinstance(coverage, list), "Missing collection health.")
        health_ids = [health["sourceId"] for health in coverage]
        require(len(set(health_ids)) == len(health_ids), "Duplicate source health.")
        for health in coverage:
            require(health["sourceId"] in source_ids, "Unknown source health.")
            require(health["status"] in {"ok", "failed", "blocked"}, "Unknown collection status.")
            require(parse_date(health["checkedAt"]) is not None, "Invalid collection timestamp.")
            for key in ("entries", "eligible", "selected"):
                require(isinstance(health[key], int) and health[key] >= 0, f"Invalid health count: {key}.")
        if edition["origin"] == "collected":
            require(any(health["status"] == "ok" for health in coverage), "A collected edition requires a successful collector.")
            require(parse_date(edition["refreshedAt"]) is not None, "Missing successful refresh timestamp.")
            start, end = parse_date(edition["windowStart"]), parse_date(edition["windowEnd"])
            require(start is not None and end is not None and start <= end, "Invalid edition window.")
            require(end <= parse_date(edition["refreshedAt"]), "Refresh precedes its collection window.")
            require(edition["outcome"] == ("stories" if edition["items"] else "quiet"), "Misleading edition outcome.")
            require(len(edition["items"]) <= settings["selection"]["maxItems"], "Edition exceeds its cap.")
        counts: dict[str, int] = {}
        for item in edition["items"]:
            require(item["sourceId"] in source_ids, "Story references an unknown source.")
            validate_summary(item, next(source for source in sources if source["id"] == item["sourceId"]))
            for key in ("id", "title", "topic", "kind", "excerptLabel", "evidence", "caveat"):
                require(isinstance(item.get(key), str) and bool(item[key]), f"Story missing {key}.")
            require(isinstance(item.get("excerpt"), str) and len(item["excerpt"]) <= 240, "Publisher excerpt exceeds 240 characters.")
            require(len(item["title"]) <= 300, "Story title is too long.")
            require(isinstance(item["matchedTerms"], list), "Missing filter explanation.")
            url, title = canonical_url(item["url"]), title_key(item["title"])
            require(bool(title), "Empty normalized title.")
            require(url not in seen_urls and title not in seen_titles, "A story was announced more than once.")
            seen_urls.add(url)
            seen_titles.add(title)
            if edition["origin"] == "collected":
                published = parse_date(item["publishedAt"])
                require(published is not None and start <= published <= end, "Fresh item has an invalid, future or out-of-window date.")
                require(item["excerptLabel"] == "Publisher excerpt", "Collected text must be labeled as a publisher excerpt.")
                counts[item["sourceId"]] = counts.get(item["sourceId"], 0) + 1
                require(counts[item["sourceId"]] <= settings["selection"]["maxPerSource"], "Source diversity cap exceeded.")
            elif item["publishedAt"] is not None:
                date.fromisoformat(item["publishedAt"])
    current = next(edition for edition in state["editions"] if edition["id"] == state["currentEditionId"])
    require(state["lastSuccessfulRefresh"] == current["refreshedAt"], "Refresh timestamp does not describe the current edition.")


def embedded_json(payload: dict) -> str:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    for before, after in (("&", "\\u0026"), ("<", "\\u003c"), (">", "\\u003e"), ("\u2028", "\\u2028"), ("\u2029", "\\u2029")):
        text = text.replace(before, after)
    return text


def digest(text: str) -> str:
    return base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode("ascii")


def render(state: dict, settings: dict, sources: list[dict], root: Path = ROOT) -> str:
    validate_state(state, settings, sources)
    template = (root / "web" / "template.html").read_text(encoding="utf-8")
    styles = (root / "web" / "styles.css").read_text(encoding="utf-8")
    app = (root / "web" / "app.js").read_text(encoding="utf-8")
    require("</script" not in app.lower(), "Reader script contains an unsafe HTML terminator.")
    payload = dict(state, site=settings, sources=sources)
    values = {
        "THEME": THEME_SCRIPT,
        "STYLE": styles,
        "APP": app,
        "DATA": embedded_json(payload),
        "CANONICAL": html.escape(settings["url"], quote=True),
        "CSP": html.escape(
            "default-src 'none'; "
            f"script-src 'sha256-{digest(THEME_SCRIPT)}' 'sha256-{digest(app)}'; "
            f"style-src 'sha256-{digest(styles)}'; "
            "base-uri 'none'; form-action 'none'; connect-src 'none'; object-src 'none'",
            quote=True,
        ),
    }
    for key in values:
        marker = "@@" + key + "@@"
        require(template.count(marker) == 1, f"Template must contain exactly one {marker}.")
    require(set(re.findall(r"@@([A-Z]+)@@", template)) == set(values), "Unknown template marker.")
    return re.sub(r"@@([A-Z]+)@@", lambda match: values[match[1]], template)


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    stage = path.with_name(path.name + ".stage")
    try:
        with stage.open("wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(stage, path)
    finally:
        stage.unlink(missing_ok=True)


def recover(root: Path) -> bool:
    journal = root / JOURNAL
    if not journal.exists():
        return False
    originals = read_json(journal)
    require(set(originals) == set(OUTPUTS), "Unrecognized transaction journal; do not overwrite files.")
    for name, content in originals.items():
        path = root.joinpath(*name.split("/"))
        if content is None:
            path.unlink(missing_ok=True)
        else:
            atomic_write(path, base64.b64decode(content, validate=True))
    journal.unlink()
    print("Recovered the previous good data and site from an interrupted write.", file=sys.stderr)
    return True


@contextmanager
def workspace_lock(root: Path):
    lock = root / ".refresh.lock"
    try:
        stream = lock.open("x", encoding="ascii")
    except FileExistsError as error:
        raise BuildError("Another build/refresh may be running. If it was interrupted, see README recovery steps.") from error
    with stream:
        stream.write(str(os.getpid()))
    try:
        recover(root)
        yield
    finally:
        lock.unlink()


def publish_files(state: dict, page: str, root: Path = ROOT) -> None:
    values = (json.dumps(state, ensure_ascii=False, indent=2) + "\n", page)
    originals = {}
    for name in OUTPUTS:
        path = root.joinpath(*name.split("/"))
        originals[name] = base64.b64encode(path.read_bytes()).decode("ascii") if path.exists() else None
    atomic_write(root / JOURNAL, json.dumps(originals).encode("utf-8"))
    try:
        for name, value in zip(OUTPUTS, values):
            atomic_write(root.joinpath(*name.split("/")), value.encode("utf-8"))
        (root / JOURNAL).unlink()
    except OSError:
        recover(root)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail if committed HTML differs from its data/template; do not write.")
    args = parser.parse_args()
    try:
        if args.check:
            require(not (ROOT / JOURNAL).exists(), "Interrupted transaction requires recovery before validation.")
            settings, sources = configuration()
            page = render(read_json(ROOT / "data" / "state.json"), settings, sources)
            require((ROOT / "site" / "index.html").read_text(encoding="utf-8") == page, "Generated reader is stale; run python -m scripts.build.")
        else:
            with workspace_lock(ROOT):
                settings, sources = configuration()
                state = read_json(ROOT / "data" / "state.json")
                page = render(state, settings, sources)
                publish_files(state, page)
        print("Reader and public source data are consistent.")
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"Build failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
