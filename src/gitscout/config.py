"""Runtime settings, read from environment variables (and an optional .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import quote

from dotenv import load_dotenv

DEFAULT_DB = "gitscout.db"
DEFAULT_ACTOR = "aleloro_dev~github-stars-email-extractor"


@dataclass(frozen=True)
class Settings:
    tokens: tuple[str, ...] = ()
    db_path: str = DEFAULT_DB
    concurrency: int = 8
    max_repos_for_commits: int = 3
    user_agent: str = "gitscout/0.2 (+https://github.com)"

    # --- GraphQL tuning
    page_size: int = 100
    commit_batch: int = 10  # users probed per aliased commit query
    commits_per_repo: int = 5

    # --- optional Supabase mirror (disabled unless a URL and key are present)
    supabase_url: str | None = None
    supabase_key: str | None = None
    #: Direct Postgres DSN. Preferred when present: it writes without the project's
    #: Data API being exposed at all, which matters on a project that deliberately
    #: keeps PostgREST switched off.
    #:
    #: It can be given whole as SUPABASE_DB_URL, or assembled from
    #: SUPABASE_DB_PASSWORD plus the project ref. The second form exists because a
    #: Postgres URI needs the password percent-encoded, and a generated password
    #: containing '@' or '/' silently produces a URI that parses into the wrong host
    #: -- Supabase warns about this in its own reset dialog. Pasting the raw password
    #: removes that whole class of mistake.
    supabase_db_url: str | None = None
    supabase_schema: str = "gitscout"
    supabase_batch: int = 500

    # --- optional third-party providers (all disabled unless a key is present)
    hunter_api_key: str | None = None
    apify_token: str | None = None
    apify_actor: str = DEFAULT_ACTOR
    provider_budget: int = 0  # max paid lookups per run; 0 = unlimited once enabled

    # --- scheduling
    schedule: str | None = None  # e.g. "6h"
    targets_file: str | None = None

    extra: dict[str, str] = field(default_factory=dict)

    @property
    def has_token(self) -> bool:
        return bool(self.tokens)

    @property
    def supabase_enabled(self) -> bool:
        return bool(self.supabase_db_url or (self.supabase_url and self.supabase_key))

    @property
    def supabase_transport(self) -> str:
        """Which writer a push would use. The DSN wins when both are configured."""
        if self.supabase_db_url:
            return "postgres"
        return "rest" if (self.supabase_url and self.supabase_key) else "none"

    @property
    def providers_enabled(self) -> tuple[str, ...]:
        names = []
        if self.hunter_api_key:
            names.append("hunter")
        return tuple(names)


def parse_tokens(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    seen: dict[str, None] = {}
    for part in raw.replace("\n", ",").split(","):
        part = part.strip()
        if part:
            seen[part] = None
    return tuple(seen)


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _clean(name: str) -> str | None:
    """Env value, or None when unset/blank/an obvious placeholder."""
    raw = (os.getenv(name) or "").strip()
    if not raw or raw.startswith(("<", "your", "YOUR", "changeme")):
        return None
    return raw


def _project_ref(url: str | None) -> str | None:
    """`https://abcd.supabase.co` -> `abcd`."""
    if not url:
        return None
    host = url.split("//", 1)[-1].split("/", 1)[0]
    return host.split(".", 1)[0] or None


def _build_dsn() -> str | None:
    """Assemble a pooler DSN from the raw password, percent-encoding it correctly."""
    password = _clean("SUPABASE_DB_PASSWORD")
    if not password:
        return None
    ref = _clean("SUPABASE_PROJECT_REF") or _project_ref(_clean("SUPABASE_URL"))
    if not ref:
        return None
    host = _clean("SUPABASE_DB_HOST") or "aws-0-us-west-1.pooler.supabase.com"
    port = _clean("SUPABASE_DB_PORT") or "6543"
    return (
        f"postgresql://postgres.{ref}:{quote(password, safe='')}@{host}:{port}/postgres"
    )


def load_settings(db_path: str | None = None) -> Settings:
    load_dotenv()
    raw = _clean("GITHUB_TOKENS") or _clean("GITHUB_TOKEN")
    return Settings(
        tokens=parse_tokens(raw),
        db_path=db_path or os.getenv("GITSCOUT_DB") or DEFAULT_DB,
        concurrency=_int("GITSCOUT_CONCURRENCY", 8),
        page_size=max(1, min(100, _int("GITSCOUT_PAGE_SIZE", 100))),
        commit_batch=max(1, min(25, _int("GITSCOUT_COMMIT_BATCH", 10))),
        commits_per_repo=max(1, min(20, _int("GITSCOUT_COMMITS_PER_REPO", 5))),
        max_repos_for_commits=max(1, min(10, _int("GITSCOUT_REPOS_PER_USER", 3))),
        supabase_url=_clean("SUPABASE_URL"),
        supabase_db_url=(
            _clean("SUPABASE_DB_URL") or _clean("DATABASE_URL") or _build_dsn()
        ),
        # SUPABASE_SERVICE_KEY is the documented name; SUPABASE_KEY is accepted because
        # that is what the Supabase dashboard copy button calls it.
        supabase_key=_clean("SUPABASE_SERVICE_KEY") or _clean("SUPABASE_KEY"),
        supabase_schema=_clean("SUPABASE_SCHEMA") or "gitscout",
        supabase_batch=max(1, min(5000, _int("SUPABASE_BATCH", 500))),
        hunter_api_key=_clean("HUNTER_API_KEY"),
        apify_token=_clean("APIFY_TOKEN"),
        apify_actor=_clean("APIFY_ACTOR") or DEFAULT_ACTOR,
        provider_budget=_int("GITSCOUT_PROVIDER_BUDGET", 0),
        schedule=_clean("GITSCOUT_SCHEDULE"),
        targets_file=_clean("GITSCOUT_TARGETS"),
    )
