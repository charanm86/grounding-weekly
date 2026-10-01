"""Rewritten summaries with bounded public evidence and fail-closed persistence."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from scripts.collector import (
    UTC, SUMMARY_INPUT_CHARS, canonical_url, collect_bounded, iso, parse_date, plain_text,
)
from scripts.summary_model import MODEL, PROMPT, PROMPT_VERSION, MAX_INPUT_TOKENS, MAX_OUTPUT_TOKENS

ROOT = Path(__file__).resolve().parent.parent
BASES = {
    "public-abstract": "AI-rewritten from the public abstract in the arXiv feed",
    "publisher-feed": "AI-rewritten from the publisher feed description",
    "editorial-seed": "AI-rewritten from the preserved source-based editorial seed note",
}
INSTRUCTIONS = re.compile(
    r"<\|[^>]+\|>|\[/?INST\]|\b(?:ignore|disregard|override)\b.{0,60}\b(?:instructions?|prompts?|rules?)\b|"
    r"\b(?:system|assistant|developer)\s*:\s*|\byou are (?:chatgpt|an? ai|a language model)\b|"
    r"\b(?:reveal|print|send)\b.{0,50}\b(?:password|secret|token|credentials?)\b",
    re.I,
)
SUMMARY_KEYS = {
    "status", "text", "basis", "sourceUrl", "inputSha256", "storySha256", "recipeSha256",
    "inputChars", "inputTruncated", "model", "modelRevision", "promptVersion", "generatedAt",
}


class SummaryError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SummaryError(message)


def hashed(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def story_hash(item: dict) -> str:
    return hashed({key: value for key, value in item.items() if key != "summary"})


def recipe_hash() -> str:
    return hashed({
        "model": MODEL["model"], "revision": MODEL["revision"], "prompt": PROMPT,
        "version": PROMPT_VERSION, "inputTokens": MAX_INPUT_TOKENS, "outputTokens": MAX_OUTPUT_TOKENS,
        "dtype": "float32", "sampling": False, "threads": 4,
        "dependencies": (ROOT / "config" / "requirements-inference.txt").read_text(encoding="utf-8"),
    })


def validate_summary(item: dict, source: dict | None = None, required: bool = False) -> None:
    summary = item.get("summary")
    if summary is None:
        require(not required, f"Rewritten summary unavailable for {item['id']}; run the summary backfill.")
        return
    require(isinstance(summary, dict) and set(summary) == SUMMARY_KEYS, "Unknown/incomplete rewritten-summary metadata.")
    require(summary["status"] == "ready", "Unknown rewritten-summary status; no excerpt fallback is permitted.")
    require(summary["basis"] in BASES, "Unknown rewritten-summary source basis.")
    require(isinstance(summary["text"], str) and 40 <= len(summary["text"]) <= 700, "Invalid rewritten-summary length.")
    require(summary["text"] != item["excerpt"], "Publisher excerpts are not rewritten summaries.")
    require(summary["text"].endswith((".", "!", "?")) and not summary["text"].endswith("..."), "Summary is not a complete sentence.")
    require(not re.search(r"[<>\n\r]|https?://", summary["text"]) and not INSTRUCTIONS.search(summary["text"]), "Summary contains markup or instruction artifacts.")
    require(summary["storySha256"] == story_hash(item), "Summary no longer matches its saved story.")
    for key in ("storySha256", "inputSha256", "recipeSha256"):
        require(isinstance(summary[key], str) and re.fullmatch(r"[a-f0-9]{64}", summary[key]) is not None, "Invalid summary hash.")
    require(summary["model"] == MODEL["model"] and summary["modelRevision"] == MODEL["revision"], "Unknown summary model or unpinned revision.")
    require(isinstance(summary["promptVersion"], str) and re.fullmatch(r"rewrite-v\d+", summary["promptVersion"]) is not None, "Unknown summary prompt version.")
    require(parse_date(summary["generatedAt"]) is not None, "Invalid summary-generation timestamp.")
    require(type(summary["inputChars"]) is int and 40 <= summary["inputChars"] <= SUMMARY_INPUT_CHARS, "Invalid summary input size.")
    require(type(summary["inputTruncated"]) is bool, "Missing summary input-bound disclosure.")
    canonical_url(summary["sourceUrl"])
    if summary["basis"] == "editorial-seed":
        require(item["excerptLabel"].startswith("Seed editorial note"), "Seed-summary basis does not match its evidence.")
        require(summary["sourceUrl"] == item["url"], "Seed-summary source changed.")
    else:
        require(item["excerptLabel"] == "Publisher excerpt", "Feed-summary basis does not match its evidence.")
        if source:
            require(summary["sourceUrl"] == source.get("feed"), "Summary evidence did not come from the configured public feed.")
        if summary["basis"] == "public-abstract":
            require(urllib_host(item["url"]) == "arxiv.org", "Abstract basis is only supported for arXiv feed abstracts.")


def urllib_host(url: str) -> str:
    from urllib.parse import urlsplit
    return urlsplit(url).hostname or ""


def clean_input(text: str, truncated: bool = False) -> tuple[str, bool]:
    require(isinstance(text, str) and len(text) <= SUMMARY_INPUT_CHARS, "Summary source exceeds its transient character bound.")
    require(not INSTRUCTIONS.search(text), "Source contains instruction-like text; summary generation stopped.")
    text = plain_text(text)
    text = re.sub(r"^arXiv:\S+\s+Announce Type:\s*\w+\s+Abstract:\s*", "", text)
    if truncated:
        boundaries = list(re.finditer(r"[.!?](?:\s|$)", text))
        require(bool(boundaries), "Bounded source has no complete sentence.")
        text = text[:boundaries[-1].start() + 1]
    require(len(text) >= 40, "Insufficient source evidence for a rewritten summary.")
    from scripts.check_public import problems
    require(not problems("data/summary.json", text), "Source summary input contains contact/credential-like material.")
    return text, truncated


def evidence_for(item: dict, source: dict, entry: dict | None) -> tuple[dict, dict]:
    seed = item["excerptLabel"].startswith("Seed editorial note")
    if seed:
        body, truncated = clean_input(item["excerpt"])
        basis, url = "editorial-seed", item["url"]
    else:
        require(entry is not None and bool(entry.get("summaryInput")), f"No allowed fuller feed evidence for {item['id']}; saved excerpt is not a silent fallback.")
        body, truncated = clean_input(entry["summaryInput"], entry.get("summaryInputTruncated", False))
        basis = "public-abstract" if urllib_host(item["url"]) == "arxiv.org" else "publisher-feed"
        url = source["feed"]
    context = {
        "title": item["title"], "source": source["name"], "basis": BASES[basis],
        "text": body, "qualification": item["evidence"],
    }
    return context, {
        "basis": basis, "sourceUrl": url, "inputSha256": hashed(context),
        "inputChars": len(body), "inputTruncated": truncated,
    }


def words(text: str) -> list[str]:
    return re.findall(r"\b[\w]+(?:[-'][\w]+)*\b", text.casefold())


def validate_output(text: str, context: dict, item: dict) -> None:
    require(isinstance(text, str) and 40 <= len(text) <= 700, "Generated summary exceeds the length bound.")
    require(12 <= len(words(text)) <= 100, "Generated summary must be concise original prose.")
    require(text.endswith((".", "!", "?")) and not text.endswith("..."), "Generated summary is unfinished.")
    require(not re.search(r"[<>\n\r]|https?://|^\s*[-*#]|^\s*(?:summary|answer)\s*:", text, re.I), "Generated summary contains formatting/chat artifacts.")
    require(not INSTRUCTIONS.search(text), "Generated summary contains instruction artifacts.")
    from scripts.check_public import problems
    require(not problems("data/summary.json", text), "Generated summary failed the public-content guard.")
    source = " ".join((context["title"], context["source"], context["text"], context["qualification"]))
    source_words, output_words = words(source), words(text)
    source_grams = {" ".join(source_words[index:index + 9]) for index in range(len(source_words) - 8)}
    require(not any(" ".join(output_words[index:index + 9]) in source_grams for index in range(len(output_words) - 8)), "Generated summary copies a long source phrase.")
    source_triples = {" ".join(source_words[index:index + 3]) for index in range(len(source_words) - 2)}
    copied = sum(" ".join(output_words[index:index + 3]) in source_triples for index in range(len(output_words) - 2))
    require(copied / max(1, len(output_words) - 2) <= 0.55, "Generated summary is too close to its source.")
    require(words(text) != words(item["excerpt"]), "Generated summary is only the saved excerpt.")
    numbers = set(re.findall(r"\b\d+(?:\.\d+)?%?", source))
    require(set(re.findall(r"\b\d+(?:\.\d+)?%?", text)) <= numbers, "Generated summary introduces unsupported numbers.")
    sentence_words = {"the", "a", "an", "it", "its", "this", "these", "that", "their", "they", "by", "using", "in",
                      "under", "however", "but", "rather", "researchers", "authors", "according", "although", "while",
                      "instead", "for", "with", "results", "tests", "reported", "evidence"}
    for token in re.findall(r"\b[A-Z][\w-]*\b", text):
        require(token.casefold() in source.casefold() or token.casefold() in sentence_words, "Generated summary introduces an unsupported name/technical term.")
    for token in ("free", "available", "rollout", "superior", "outperforms", "browser", "pricing"):
        require(not re.search(r"\b" + token + r"\b", text, re.I) or re.search(r"\b" + token + r"\b", source, re.I), "Generated summary introduces an unsupported capability/access claim.")
    require(not re.search(r"\b(?:revolutioniz\w*|game[- ]chang\w*|underscores|highlights the importance|significant implications)\b", text, re.I), "Generated summary contains promotional/analytical boilerplate.")


def run_model(inputs: list[dict]) -> list[str]:
    env = {key: value for key, value in os.environ.items() if key.upper() in {
        "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL", "GROUNDING_MODEL_DIR",
    }}
    env.update(
        HF_HOME=str(Path(env.get("GROUNDING_MODEL_DIR", ROOT / ".venv" / "summary-model")) / "hub"),
        HF_HUB_DISABLE_IMPLICIT_TOKEN="1", HF_HUB_DISABLE_TELEMETRY="1", TOKENIZERS_PARALLELISM="false",
    )
    try:
        result = subprocess.run(
            [sys.executable, "-m", "scripts.summary_model"], input=json.dumps(inputs, ensure_ascii=True),
            text=True, encoding="utf-8", capture_output=True, timeout=1200, env=env, check=True,
        )
    except subprocess.TimeoutExpired as error:
        raise SummaryError("CPU summary job exceeded 20 minutes; good data/site were not replaced.") from error
    except subprocess.CalledProcessError as error:
        detail = next((line[:500] for line in (error.stderr or "").splitlines() if line.startswith("Summary inference failed")), "")
        raise SummaryError("CPU summary worker failed. " + (detail or "Verify the pinned dependency install, public model download and input limits; no data replaced.")) from error
    if result.stderr:
        print(result.stderr[-1000:], file=sys.stderr)
    outputs = json.loads(result.stdout)
    require(isinstance(outputs, list) and len(outputs) == len(inputs), "Incomplete model output; no summaries saved.")
    return outputs


def summarize_state(state: dict, sources: list[dict], results=None, *, model_fn=run_model, collect_fn=collect_bounded, clock=lambda: datetime.now(UTC), smoke=False) -> dict:
    updated = copy.deepcopy(state)
    source_by_id = {source["id"]: source for source in sources}
    pending = []
    for edition in updated["editions"]:
        for item in edition["items"]:
            validate_summary(item, source_by_id[item["sourceId"]])
            if item.get("summary") and item["summary"]["recipeSha256"] == recipe_hash():
                continue
            pending.append(item)
    require(len(pending) <= 12, "More than 12 summaries need backfill; split the migration explicitly rather than exceed runtime bounds.")
    if not pending:
        print("All saved summaries match their story and generation recipe; reused without refetching archives.")
        return updated
    feeds = {}
    if results is not None:
        for source, (entries, health) in zip((source for source in sources if source.get("feed")), results):
            feeds[source["id"]] = (entries, health)
    contexts, metadata = [], []
    for item in pending:
        source = source_by_id[item["sourceId"]]
        entry = None
        if item["excerptLabel"] == "Publisher excerpt":
            if source["id"] not in feeds:
                feeds[source["id"]] = collect_fn(source)
            entries, health = feeds[source["id"]]
            require(health["status"] == "ok", f"Summary evidence failed for {source['id']}: {health['message']}")
            for candidate in entries:
                try:
                    matches = canonical_url(candidate["rawUrl"], source["feed"]) == canonical_url(item["url"])
                except ValueError:
                    continue
                if matches:
                    entry = candidate
                    break
        context, meta = evidence_for(item, source, entry)
        contexts.append(context)
        metadata.append(meta)
    if smoke:
        require(len(pending) < 12, "Leave one slot for the unseen-input quality check.")
        contexts.append({
            "title": "Harbor adds monitoring for changed safety notices", "source": "Synthetic test publisher",
            "basis": "Synthetic unseen test, not a story",
            "text": "Harbor has introduced a monitoring agent for public product-safety notices. It checks a fixed list of regulator webpages each morning, compares new text with saved versions, and sends a cited draft update to a reviewer. It does not issue warnings to customers automatically. In a small pilot, reviewers found the citations useful but still had to check ambiguous product names.",
            "qualification": "Synthetic evaluation example, never published as news.",
        })
    outputs = model_fn(contexts)
    require(isinstance(outputs, list) and len(outputs) == len(contexts), "Model returned an incomplete summary batch.")
    for index, (context, text) in enumerate(zip(contexts, outputs)):
        item = pending[index] if index < len(pending) else {"excerpt": context["text"], "id": "unseen-input-check"}
        try:
            validate_output(text, context, item)
        except SummaryError as error:
            from scripts.check_public import problems
            if isinstance(text, str) and len(text) <= 700 and not problems("data/summary.json", text) and not INSTRUCTIONS.search(text):
                print(f"Rejected rewrite candidate {item['id']}: {text}", file=sys.stderr)
            raise SummaryError(f"{item['id']}: {error}") from error
        print(f"Rewritten {item['id']}: {text}")
    for item, context, meta, text in zip(pending, contexts, metadata, outputs):
        item["summary"] = dict(
            meta, status="ready", text=text, storySha256=story_hash(item), recipeSha256=recipe_hash(),
            model=MODEL["model"], modelRevision=MODEL["revision"], promptVersion=PROMPT_VERSION, generatedAt=iso(clock()),
        )
        validate_summary(item, source_by_id[item["sourceId"]], required=True)
    return updated


def backfill(root=ROOT, *, summarize_fn=summarize_state) -> dict:
    from scripts.build import configuration, read_json, render, publish_files, validate_state, workspace_lock
    with workspace_lock(root):
        settings, sources = configuration(root)
        previous = read_json(root / "data" / "state.json")
        validate_state(previous, settings, sources)
        updated = summarize_fn(previous, sources, smoke=True)
        stripped = copy.deepcopy(updated)
        for edition in stripped["editions"]:
            for item in edition["items"]:
                old = next(old for old_edition in previous["editions"] for old in old_edition["items"] if old["id"] == item["id"])
                if "summary" in old:
                    item["summary"] = old["summary"]
                else:
                    item.pop("summary", None)
        require(stripped == previous, "Summary backfill attempted to alter news data, collection health or dates.")
        for edition in updated["editions"]:
            for item in edition["items"]:
                validate_summary(item, next(source for source in sources if source["id"] == item["sourceId"]), required=True)
        publish_files(updated, render(updated, settings, sources, root), root)
        print(f"Saved rewritten summaries only. Last successful collection remains {previous['lastSuccessfulRefresh']}.")
        return updated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backfill", action="store_true", help="Rewrite only the saved story set; no news discovery or date changes.")
    args = parser.parse_args()
    try:
        if args.backfill:
            backfill()
        else:
            from scripts.build import configuration, read_json
            _, sources = configuration()
            for edition in read_json(ROOT / "data" / "state.json")["editions"]:
                for item in edition["items"]:
                    validate_summary(item, next(source for source in sources if source["id"] == item["sourceId"]), required=True)
            print("Every saved story has a genuine rewritten summary.")
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"Summary operation failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
