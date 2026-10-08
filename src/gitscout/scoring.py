"""Lead scoring: 0-100, so the export is ordered by who is worth contacting first.

Deliberately a transparent weighted sum rather than a model. You can read any score
off the inputs, which matters when a human is deciding whether to email someone. Once
you have reply/bounce labels, swap this for a fitted model -- the interface is just
``score_rows(rows) -> {login: score}``.

The strongest signal by far is *what they did*: filing an issue or opening a PR on a
cloud-security tool says far more about intent than a star does.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

#: What each interaction says about intent. Max over a user's signals, not a sum.
KIND_WEIGHT: dict[str, float] = {
    "contribs": 32.0,  # landed commits on the default branch
    "prs": 30.0,  # opened a pull request
    "issues": 26.0,  # engaged enough to report something
    "discussions": 22.0,  # asking or answering in the open
    "forks": 18.0,  # took a copy, likely evaluating
    "stars": 10.0,  # bookmarked it
}

#: Bio/company words that suggest a security or platform buyer.
ICP_KEYWORDS: tuple[str, ...] = (
    "security",
    "appsec",
    "infosec",
    "cloudsec",
    "devsecops",
    "ciso",
    "compliance",
    "grc",
    "soc2",
    "pentest",
    "threat",
    "vulnerability",
    "devops",
    "sre",
    "platform engineer",
    "infrastructure",
    "cloud",
    "kubernetes",
    "terraform",
    "aws",
    "azure",
    "gcp",
)

#: Words that suggest a student/hobbyist rather than a buyer. Mildly negative.
NEGATIVE_KEYWORDS: tuple[str, ...] = ("student", "learning", "aspiring", "bootcamp", "intern")

MAX_SCORE = 100.0


def _text(row: Mapping[str, Any]) -> str:
    parts = [row.get("bio"), row.get("company"), row.get("location"), row.get("name")]
    return " ".join(str(p) for p in parts if p).lower()


def _followers_points(followers: int | None) -> float:
    """Diminishing returns: 10 followers matters, 10k vs 20k does not."""
    n = followers or 0
    if n <= 0:
        return 0.0
    import math

    return min(15.0, 3.0 * math.log10(n + 1) * 1.6)


def _recency_points(last_seen: str | None, *, now: datetime | None = None) -> float:
    """Someone who starred last week is a warmer lead than someone from 2019."""
    if not last_seen:
        return 0.0
    try:
        seen = datetime.fromisoformat(str(last_seen).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    days = ((now or datetime.now(timezone.utc)) - seen).days
    if days <= 30:
        return 15.0
    if days <= 90:
        return 11.0
    if days <= 365:
        return 6.0
    if days <= 730:
        return 2.0
    return 0.0


def _keyword_points(text: str) -> float:
    if not text:
        return 0.0
    hits = sum(1 for kw in ICP_KEYWORDS if kw in text)
    points = min(18.0, hits * 6.0)
    if any(kw in text for kw in NEGATIVE_KEYWORDS):
        points -= 8.0
    return points


def _kind_points(kinds: str | Sequence[str] | None) -> float:
    if not kinds:
        return 0.0
    items = kinds.split(",") if isinstance(kinds, str) else list(kinds)
    weights = [KIND_WEIGHT.get(k.strip(), 0.0) for k in items if k]
    if not weights:
        return 0.0
    best = max(weights)
    # A second, weaker signal adds a little: starred *and* forked beats either alone.
    extra = min(6.0, 2.0 * (len(set(w for w in weights if w > 0)) - 1))
    return best + extra


def score_row(row: Mapping[str, Any], *, now: datetime | None = None) -> float:
    """Score one user 0-100."""
    text = _text(row)
    points = 0.0
    points += _kind_points(row.get("kinds"))
    points += min(8.0, 3.0 * max(0, int(row.get("repo_count") or 1) - 1))
    points += _followers_points(row.get("followers"))
    points += _keyword_points(text)
    points += _recency_points(row.get("last_seen"), now=now)
    if row.get("company"):
        points += 6.0
    if row.get("email_conf"):
        points += 4.0 * float(row["email_conf"])
    return round(max(0.0, min(MAX_SCORE, points)), 1)


def score_rows(
    rows: Iterable[Mapping[str, Any]], *, now: datetime | None = None
) -> dict[str, float]:
    return {str(r["login"]): score_row(r, now=now) for r in rows if r.get("login")}


def rescore(store: Any, *, now: datetime | None = None) -> int:
    """Recompute every score in the database. Returns how many users were scored."""
    scores = score_rows(store.scoring_inputs(), now=now)
    store.set_scores(scores)
    return len(scores)


def explain(row: Mapping[str, Any], *, now: datetime | None = None) -> dict[str, float]:
    """Per-component breakdown, for debugging why someone ranks where they do."""
    text = _text(row)
    return {
        "signal": round(_kind_points(row.get("kinds")), 1),
        "repos": round(min(8.0, 3.0 * max(0, int(row.get("repo_count") or 1) - 1)), 1),
        "followers": round(_followers_points(row.get("followers")), 1),
        "keywords": round(_keyword_points(text), 1),
        "recency": round(_recency_points(row.get("last_seen"), now=now), 1),
        "company": 6.0 if row.get("company") else 0.0,
        "email": round(4.0 * float(row.get("email_conf") or 0), 1),
        "total": score_row(row, now=now),
    }
