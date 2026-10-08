"""Apify fallback: run a third-party GitHub actor and fold its output into the DB.

This exists as an escape hatch, not a recommendation. Use it when you have no GitHub
token, or when you specifically want an actor's extra website-scraped addresses.

What you give up versus the GraphQL path:

* **Cost.** Actors bill per result (roughly $15 per 1,000) for data the API gives free.
* **No incremental mode.** An actor re-scrapes the whole stargazer list every run, so
  it is a poor fit for a cron schedule.
* **Fragility.** Actors scrape HTML, so they break when GitHub changes markup, and
  field names differ between actors.
* **Provenance.** You cannot tell how an address was obtained, so everything lands at
  a deliberately modest confidence.

Because every actor returns a different shape, the field mapper here is tolerant: it
accepts a range of common key spellings and ignores what it does not recognise. Point
it at a different actor with ``APIFY_ACTOR``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

import httpx

from .emails import normalize_email
from .ingest import parse_repo
from .models import EmailCandidate, Interaction, Profile
from .storage import Store

log = logging.getLogger(__name__)

API_BASE = "https://api.apify.com/v2"
#: Third-party, unverifiable provenance: never outrank a GitHub-sourced address.
APIFY_CONFIDENCE = 0.55

#: Tolerant field mapping -- actors disagree on nearly every key name.
_FIELDS: dict[str, tuple[str, ...]] = {
    "login": ("login", "username", "user", "userName", "handle", "githubUsername"),
    "email": ("email", "emailAddress", "publicEmail", "email_address"),
    "name": ("name", "fullName", "displayName", "full_name"),
    "company": ("company", "organization", "org"),
    "location": ("location", "city", "country"),
    "blog": ("blog", "website", "websiteUrl", "url", "homepage"),
    "twitter": ("twitter", "twitterUsername", "twitter_username"),
    "bio": ("bio", "description", "about"),
    "followers": ("followers", "followersCount", "followers_count"),
    "type": ("type", "accountType", "__typename"),
}


class ApifyError(RuntimeError):
    """The Apify API refused or failed the run."""


def pick(item: Mapping[str, Any], field: str) -> Any:
    for key in _FIELDS.get(field, (field,)):
        if key in item and item[key] not in (None, ""):
            return item[key]
    return None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass
class ApifyResult:
    repo: str
    kind: str
    fetched: int = 0
    new: int = 0
    profiles: int = 0
    emails_new: int = 0
    items: int = 0


def map_item(repo: str, kind: str, item: Mapping[str, Any]) -> tuple[Interaction, Profile] | None:
    """Convert one dataset item into an Interaction + Profile, or None if unusable."""
    login = pick(item, "login")
    if not login or not isinstance(login, str):
        return None
    login = login.strip().lstrip("@")
    if not login or login == "ghost":
        return None

    raw_type = pick(item, "type")
    user_type = str(raw_type) if raw_type else "User"
    profile = Profile(
        login=login,
        type="User" if user_type.lower() in ("user", "", "none") else user_type,
        name=pick(item, "name"),
        company=pick(item, "company"),
        bio=pick(item, "bio"),
        location=pick(item, "location"),
        blog=pick(item, "blog"),
        twitter=pick(item, "twitter"),
        public_email=pick(item, "email"),
        followers=_as_int(pick(item, "followers")),
    )
    return Interaction(repo, login, kind, profile.type), profile


class ApifyClient:
    """Minimal Apify REST client: start an actor, wait, read the dataset."""

    def __init__(
        self,
        token: str,
        *,
        actor: str,
        base_url: str = API_BASE,
        http: httpx.AsyncClient | None = None,
        timeout: float = 600.0,
    ) -> None:
        if not token:
            raise ApifyError("APIFY_TOKEN is not set")
        self._token = token
        self.actor = actor.replace("/", "~")
        self._base = base_url.rstrip("/")
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(
            timeout=timeout, headers={"User-Agent": "gitscout/0.2"}
        )

    async def __aenter__(self) -> "ApifyClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def run_actor(self, payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Run the actor to completion and return its dataset items."""
        url = f"{self._base}/acts/{self.actor}/run-sync-get-dataset-items"
        resp = await self._http.post(url, params={"token": self._token}, json=dict(payload))
        if resp.status_code in (401, 403):
            raise ApifyError("Apify rejected the token (check APIFY_TOKEN)")
        if resp.status_code == 404:
            raise ApifyError(f"Apify actor not found: {self.actor}")
        if resp.status_code >= 400:
            raise ApifyError(f"Apify returned HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise ApifyError("Apify returned non-JSON") from exc
        if isinstance(data, Mapping):
            data = data.get("items") or []
        return [item for item in data if isinstance(item, Mapping)]


def actor_input(repo: str, kind: str, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """A deliberately redundant input blob.

    Actors disagree on input keys as much as output keys, and unknown keys are
    ignored, so sending several spellings is the pragmatic way to work with whichever
    actor the user has configured.
    """
    url = f"https://github.com/{repo}"
    payload: dict[str, Any] = {
        "repositoryUrl": url,
        "repoUrl": url,
        "repository": repo,
        "startUrls": [{"url": f"{url}/stargazers"}],
        "type": kind,
        "maxItems": 1000,
    }
    payload.update(dict(extra or {}))
    return payload


async def ingest_via_apify(
    client: ApifyClient,
    store: Store,
    repo: str,
    kind: str = "stars",
    *,
    extra_input: Mapping[str, Any] | None = None,
) -> ApifyResult:
    """Run the actor for one repo and fold the results into the database."""
    repo = parse_repo(repo)
    result = ApifyResult(repo, kind)

    items = await client.run_actor(actor_input(repo, kind, extra_input))
    result.items = len(items)

    interactions: list[Interaction] = []
    profiles: list[Profile] = []
    for item in items:
        mapped = map_item(repo, kind, item)
        if mapped is None:
            continue
        interaction, profile = mapped
        interactions.append(interaction)
        profiles.append(profile)

    result.fetched = len(interactions)
    result.new = store.add_interactions(interactions)
    for profile in profiles:
        store.upsert_profile(profile)
        result.profiles += 1
        email = normalize_email(profile.public_email)
        if email:
            result.emails_new += store.add_emails(
                profile.login, [EmailCandidate(email, "apify", APIFY_CONFIDENCE)]
            )
    log.info(
        "apify %s/%s: %d items -> %d interactions (%d new), %d emails",
        repo,
        kind,
        result.items,
        result.fetched,
        result.new,
        result.emails_new,
    )
    return result


async def ingest_repos_apify(
    client: ApifyClient,
    store: Store,
    repos: Iterable[str],
    kinds: Sequence[str] = ("stars",),
    *,
    extra_input: Mapping[str, Any] | None = None,
) -> list[ApifyResult]:
    out: list[ApifyResult] = []
    for repo in repos:
        for kind in kinds:
            out.append(
                await ingest_via_apify(client, store, repo, kind, extra_input=extra_input)
            )
    return out
