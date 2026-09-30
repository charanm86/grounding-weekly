"""Fail closed on likely credentials, contact details or unintended public files."""

from __future__ import annotations

import re
import subprocess
import sys

from scripts.build import ROOT

ALLOWED_ROOT = {".gitignore", ".gitattributes", "README.md"}
ALLOWED_DIRS = {"config", "data", "scripts", "tests", "web", "site", ".github"}
EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
TOKEN = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")
PRIVATE_KEY = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
CONTACT_LINK = re.compile(r"(?:mailto|tel|sms|whatsapp):|https://(?:wa\.me|api\.whatsapp\.com)/", re.I)
PRIVATE_PATH = re.compile(r"(?:[A-Z]:\\Users\\|\/home\/[^/]+\/|\.copilot\/session-state\/|\.azure\/|\.ssh\/)", re.I)


def problems(path: str, text: str) -> list[str]:
    issues = []
    if TOKEN.search(text) or PRIVATE_KEY.search(text):
        issues.append("possible credential")
    if CONTACT_LINK.search(text):
        issues.append("personal contact link")
    if PRIVATE_PATH.search(text):
        issues.append("private machine/session path")
    for address in EMAIL.findall(text):
        if not address.lower().endswith("@users.noreply.github.com"):
            issues.append("non-noreply email address")
            break
    if path.startswith(("data/", "site/")):
        # Contact-like long digit runs with dialing punctuation, not ISO dates.
        if re.search(r"(?<!\w)\+\d{1,3}[ -]\d{3,5}[ -]\d{4,10}(?!\w)", text):
            issues.append("possible personal phone number")
    return issues


def main() -> int:
    files = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT, capture_output=True, check=True,
    ).stdout.decode("utf-8").split("\0")
    errors = []
    for name in filter(None, files):
        parts = name.split("/")
        if not ((len(parts) == 1 and name in ALLOWED_ROOT) or (len(parts) > 1 and parts[0] in ALLOWED_DIRS)):
            errors.append(f"{name}: unexpected public file")
            continue
        path = ROOT.joinpath(*parts)
        if path.is_symlink():
            errors.append(f"{name}: symbolic links are not permitted")
            continue
        if not path.exists():
            continue
        if path.stat().st_size > 10 * 1024 * 1024:
            errors.append(f"{name}: exceeds 10 MB public-file limit")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeError:
            errors.append(f"{name}: non-text file is not expected")
            continue
        errors.extend(f"{name}: {issue}" for issue in problems(name, text))
    if errors:
        print("Public-content checks failed:\n" + "\n".join(errors), file=sys.stderr)
        return 1
    print("Public-content guard passed. This is a heuristic, not a substitute for reviewing the diff.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
