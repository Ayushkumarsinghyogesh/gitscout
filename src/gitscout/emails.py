"""Email validation, junk filtering, extraction and confidence scoring."""
from __future__ import annotations

import re
from urllib.parse import unquote

_EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+\-]+@(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,24}"
)
_STRICT_RE = re.compile(
    r"^[a-z0-9._%+\-]+@(?:[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$"
)

_JUNK_DOMAINS = {
    "noreply.github.com",
    "example.com",
    "example.org",
    "example.net",
    "localhost",
    "test.com",
    "domain.com",
    "email.com",
    "yourdomain.com",
    "sentry.io",
}
_JUNK_DOMAIN_SUFFIXES = (
    ".noreply.github.com",
    ".sentry.io",
    ".local",
    ".localdomain",
    ".invalid",
    ".example",
    ".test",
    ".lan",
    ".internal",
)
# "logo@2x.png" style false positives when scanning web pages
_FILE_TLDS = {"png", "jpg", "jpeg", "gif", "svg", "webp", "css", "js", "ico", "woff", "woff2"}
_JUNK_LOCAL_PREFIXES = (
    "noreply",
    "no-reply",
    "no_reply",
    "donotreply",
    "do-not-reply",
    "bounce",
    "mailer-daemon",
)

SOURCE_BASE_CONFIDENCE = {
    "profile": 0.95,  # the user published it on their profile
    "commit_api": 0.85,  # author of commits in the user's own repos (API filtered by login)
    "events": 0.80,  # author on the user's public push events
    "website": 0.60,  # found on the website linked from the profile
}


def is_junk_email(email: str) -> bool:
    local, _, domain = email.lower().rpartition("@")
    if not local or not domain:
        return True
    if domain in _JUNK_DOMAINS or domain.endswith(_JUNK_DOMAIN_SUFFIXES):
        return True
    if domain.rsplit(".", 1)[-1] in _FILE_TLDS:
        return True
    return local.startswith(_JUNK_LOCAL_PREFIXES)


def normalize_email(raw: str | None) -> str | None:
    """Return a clean lowercase email, or None if invalid / junk / noreply."""
    if not raw:
        return None
    s = unquote(raw).strip().strip("<>\"' ,;:.()[]")
    if s.lower().startswith("mailto:"):
        s = s[7:]
    s = s.split("?")[0].strip().lower()
    if len(s) > 254 or not _STRICT_RE.match(s):
        return None
    if is_junk_email(s):
        return None
    return s


def extract_emails_from_text(text: str) -> list[str]:
    """All distinct, valid, non-junk emails in a blob of text/HTML (order preserved)."""
    found: dict[str, None] = {}
    for match in _EMAIL_RE.findall(text or ""):
        email = normalize_email(match)
        if email:
            found[email] = None
    return list(found)


def _tokens(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", value.lower()))


def _squash(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def name_matches(login: str, profile_name: str | None, commit_name: str | None) -> bool:
    """Does the git author name plausibly belong to this GitHub user?"""
    if not commit_name:
        return False
    squashed = _squash(commit_name)
    if not squashed:
        return False
    if squashed == _squash(login):
        return True
    if profile_name:
        if squashed == _squash(profile_name):
            return True
        wanted = {t for t in _tokens(profile_name) if len(t) >= 3}
        overlap = wanted & _tokens(commit_name)
        if wanted and len(overlap) >= min(2, len(wanted)):
            return True
    return False


def score(source: str, *, name_match: bool = True) -> float:
    base = SOURCE_BASE_CONFIDENCE.get(source, 0.3)
    return round(base if name_match else base * 0.5, 2)
