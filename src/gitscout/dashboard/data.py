"""What the dashboard reads and writes, always scoped to what the viewer may see.

Every function takes the viewer, so a query cannot forget the scope: a super admin
sees every repository, everyone else the repositories in their (unrevoked)
memberships. Writes go through the same JobStore the worker uses, and every change
leaves an audit row.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence

import httpx

from ..ingest import parse_repo
from ..jobs import JobStore, next_run

ROLES = {"platform_admin": "Super admin", "client_admin": "Admin client", "client_viewer": "Client"}
DEFAULT_SETTINGS = {"max": 2000, "limit": 2000}
RECORD_COLUMNS = ("username", "full_name", "email", "linkedin_url", "company_name", "location",
                  "followers_count", "issues_count", "pull_requests_count", "discussions_count",
                  "forks_count", "engagement_signals", "first_seen_at", "last_seen_at")


class Forbidden(Exception):
    pass


@dataclass
class Viewer:
    user_id: str
    email: str
    role: str
    display_name: str | None = None
    repos: list[str] = field(default_factory=list)          # visible repositories
    admin_repos: list[str] = field(default_factory=list)    # where they are client_admin

    @property
    def is_super(self) -> bool:
        return self.role == "platform_admin"

    @property
    def role_label(self) -> str:
        return ROLES.get(self.role, self.role)

    def can_see(self, repo: str) -> bool:
        return self.is_super or repo in self.repos

    def can_manage(self, repo: str) -> bool:
        return self.is_super or repo in self.admin_repos

    @property
    def can_manage_users(self) -> bool:
        return self.is_super or self.role == "client_admin"


def _dicts(conn: Any, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        if not cur.description:
            return []
        names = [d.name for d in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]


def viewer(conn: Any, user_id: str) -> Viewer | None:
    """The app_users row and memberships for an Auth user; None when not provisioned
    or disabled."""
    rows = _dicts(conn, "select user_id::text, email, role, display_name, disabled_at "
                        "from gitscout.app_users where user_id = %s", [user_id])
    if not rows or rows[0]["disabled_at"] is not None:
        return None
    u = rows[0]
    v = Viewer(u["user_id"], u["email"], u["role"], u["display_name"])
    if v.is_super:
        v.repos = [r["repo"] for r in _dicts(conn, "select repo from gitscout.repositories order by repo")]
        v.admin_repos = list(v.repos)
    else:
        members = _dicts(conn, "select repo, role from gitscout.memberships "
                               "where user_id = %s and revoked_at is null order by repo", [user_id])
        v.repos = [m["repo"] for m in members]
        v.admin_repos = [m["repo"] for m in members if m["role"] == "client_admin"]
    return v


def audit(conn: Any, v: Viewer | None, action: str, **kw: Any) -> None:
    JobStore.wrap(conn).audit(action, actor_id=v.user_id if v else None,
                              actor_email=v.email if v else None, **kw)


# ---------------------------------------------------------------- overview


def repositories(conn: Any, v: Viewer) -> list[dict[str, Any]]:
    if not v.repos:
        return []
    rows = _dicts(conn, """
        select r.repo, r.enabled, r.schedule_cron, r.timezone, r.collections, r.settings, r.added_at,
               r.description, r.stars, r.last_success_at,
               h.consecutive_failures, h.last_job_id, h.last_job_status, h.last_job_finished_at,
               h.last_job_error, h.active_job_id, h.active_job_status, h.overdue,
               coalesce(s.records, 0) as records, coalesce(s.with_email, 0) as with_email,
               coalesce(s.with_linkedin, 0) as with_linkedin, s.newest_collected_at
        from gitscout.repositories r
        left join gitscout.repo_health h using (repo)
        left join gitscout.repo_stats s using (repo)
        where r.repo = any(%s) order by r.repo""", [v.repos])
    now = datetime.now(timezone.utc)
    for r in rows:
        r["next_run"] = next_run(r["schedule_cron"], r["timezone"], now) if r["enabled"] else None
        r["can_manage"] = v.can_manage(r["repo"])
    return rows


def totals(repos: Sequence[dict[str, Any]]) -> dict[str, int]:
    return {k: sum(int(r[k] or 0) for r in repos) for k in ("records", "with_email", "with_linkedin")} | {
        "repos": len(repos), "running": sum(1 for r in repos if r["active_job_status"] == "running"),
        "failing": sum(1 for r in repos if (r["consecutive_failures"] or 0) > 0)}


def jobs(conn: Any, v: Viewer, *, repo: str | None = None, status: str | None = None,
         limit: int = 50) -> list[dict[str, Any]]:
    scope = [repo] if repo else v.repos
    if repo and not v.can_see(repo):
        raise Forbidden(repo)
    where, params = ["repo = any(%s)"], [scope]
    if status:
        where.append("status = %s"); params.append(status)
    params.append(limit)
    return _dicts(conn, f"select * from gitscout.run_history where {' and '.join(where)} "
                        f"order by id desc limit %s", params)


# ----------------------------------------------------------------- records


def records(conn: Any, v: Viewer, *, repo: str | None, q: str | None, with_email: bool,
            page: int, per_page: int = 50) -> tuple[list[dict[str, Any]], int]:
    scope = [repo] if repo else v.repos
    if repo and not v.can_see(repo):
        raise Forbidden(repo)
    where = ["d.repository_owner || '/' || d.repository_name = any(%s)"]
    params: list[Any] = [scope]
    if q:
        where.append("(d.username ilike %s or d.full_name ilike %s or d.email ilike %s or d.company_name ilike %s)")
        params += [f"%{q}%"] * 4
    if with_email:
        where.append("d.email is not null")
    clause = " and ".join(where)
    total = _dicts(conn, f'select count(*) as n from "Defendermate2".final_outreach_users d where {clause}', params)[0]["n"]
    cols = ", ".join(f"d.{c}" for c in RECORD_COLUMNS)
    rows = _dicts(conn, f"""select d.repository_owner || '/' || d.repository_name as repo, {cols}, fc.collected_at
        from "Defendermate2".final_outreach_users d
        left join gitscout.dataset_first_collected fc
          on fc.repo = d.repository_owner || '/' || d.repository_name and fc.login = d.username
        where {clause} order by fc.collected_at desc nulls first, d.username
        limit %s offset %s""", params + [per_page, max(0, page - 1) * per_page])
    return rows, int(total)


def person(conn: Any, v: Viewer, repo: str, login: str) -> dict[str, Any] | None:
    if not v.can_see(repo):
        raise Forbidden(repo)
    owner, name = repo.split("/", 1)
    rows = _dicts(conn, """select d.*, fc.collected_at as first_collected_at
        from "Defendermate2".final_outreach_users d
        left join gitscout.dataset_first_collected fc
          on fc.repo = d.repository_owner || '/' || d.repository_name and fc.login = d.username
        where d.repository_owner = %s and d.repository_name = %s and d.username = %s""", [owner, name, login])
    return rows[0] if rows else None


# --------------------------------------------------------- adding repositories


def parse_repo_list(text: str) -> tuple[list[str], list[str]]:
    """Lines, commas or whitespace; owner/name or GitHub URLs. -> (repos, rejected)."""
    seen: dict[str, None] = {}
    bad: list[str] = []
    for token in text.replace(",", "\n").split():
        token = token.strip().strip('"\'')
        if not token or token.lower() in ("repo", "repository", "url"):
            continue
        try:
            seen[parse_repo(token)] = None
        except ValueError:
            bad.append(token)
    return list(seen), bad


def github_repo(repo: str, token: str | None, *, transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    """Public facts about a repository from GitHub's REST API; raises ValueError when it
    does not exist or is private."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "gitscout-dashboard"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with httpx.Client(timeout=20, transport=transport, headers=headers) as http:
        r = http.get(f"https://api.github.com/repos/{repo}")
    if r.status_code == 404:
        raise ValueError("not found on GitHub")
    if r.status_code >= 300:
        raise ValueError(f"GitHub answered {r.status_code}")
    data = r.json()
    if data.get("private"):
        raise ValueError("private repositories are not collected")
    return data


def add_repositories(conn: Any, v: Viewer, repos: Sequence[str], *, token: str | None,
                     max_per_user: int, transport: httpx.BaseTransport | None = None) -> dict[str, list]:
    """Register each repository (or attach the viewer to an existing one), give the viewer
    access, and queue its first run. -> {"added": [...], "joined": [...], "failed": [(repo, why)]}"""
    store = JobStore.wrap(conn)
    out: dict[str, list] = {"added": [], "joined": [], "queued": [], "failed": []}
    mine = _dicts(conn, "select count(*) as n from gitscout.repositories where added_by = %s", [v.user_id])[0]["n"]
    for wanted in repos:
        try:
            info = github_repo(wanted, token, transport=transport)
        except (ValueError, httpx.HTTPError) as exc:
            out["failed"].append((wanted, str(exc)))
            continue
        repo = info["full_name"]
        exists = _dicts(conn, "select repo from gitscout.repositories where repo = %s or repository_id = %s",
                        [repo, info["id"]])
        if exists:
            repo = exists[0]["repo"]
            if not v.can_see(repo):
                _grant(conn, v.user_id, repo, _member_role(v), v.user_id)
                v.repos.append(repo)
            out["joined"].append(repo)
            audit(conn, v, "repo.join", repo=repo, target_kind="repository", target_id=repo)
        else:
            if not v.is_super and mine >= max_per_user:
                out["failed"].append((repo, f"limit of {max_per_user} repositories per user reached"))
                continue
            owner = info.get("owner") or {}
            with conn.cursor() as cur:
                cur.execute("""insert into gitscout.repositories
                    (repo, repository_id, node_id, owner, name, url, description, homepage, stars, forks,
                     language, topics, is_archived, enabled, settings, added_by)
                    values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true, %s, %s)""",
                    [repo, info["id"], info.get("node_id"), owner.get("login"), info.get("name"),
                     info.get("html_url"), info.get("description"), info.get("homepage"),
                     info.get("stargazers_count"), info.get("forks_count"), info.get("language"),
                     list(info.get("topics") or []), bool(info.get("archived")),
                     store._jsonb(DEFAULT_SETTINGS), v.user_id])
            mine += 1
            if not v.is_super:
                _grant(conn, v.user_id, repo, _member_role(v), v.user_id)
                v.repos.append(repo)
                if v.role == "client_admin":
                    v.admin_repos.append(repo)
            else:
                v.repos.append(repo); v.admin_repos.append(repo)
            out["added"].append(repo)
            audit(conn, v, "repo.add", repo=repo, target_kind="repository", target_id=repo)
        if store.enqueue(repo, "incremental", "manual", requested_by=v.user_id) is not None:
            out["queued"].append(repo)
    return out


def _member_role(v: Viewer) -> str:
    return "client_admin" if v.role in ("client_admin", "platform_admin") else "client_viewer"


def _grant(conn: Any, user_id: str, repo: str, role: str, by: str) -> None:
    with conn.cursor() as cur:
        cur.execute("""insert into gitscout.memberships (user_id, repo, role, granted_by)
                       values (%s, %s, %s, %s)
                       on conflict (user_id, repo) do update
                       set role = excluded.role, revoked_at = null, granted_by = excluded.granted_by,
                           granted_at = now()""", [user_id, repo, role, by])


def run_now(conn: Any, v: Viewer, repo: str, kind: str = "incremental") -> int | None:
    if not v.can_see(repo):
        raise Forbidden(repo)
    if kind != "incremental" and not v.can_manage(repo):
        raise Forbidden(repo)
    job_id = JobStore.wrap(conn).enqueue(repo, kind, "manual", requested_by=v.user_id)
    audit(conn, v, "job.enqueue", repo=repo, target_kind="job", target_id=str(job_id),
          details={"kind": kind}, result="ok" if job_id else "skipped")
    return job_id


def set_enabled(conn: Any, v: Viewer, repo: str, enabled: bool) -> None:
    if not v.can_manage(repo):
        raise Forbidden(repo)
    JobStore.wrap(conn).set_repository(repo, enabled=enabled)
    audit(conn, v, "repo.enable" if enabled else "repo.disable", repo=repo, target_kind="repository", target_id=repo)


def cancel_job(conn: Any, v: Viewer, job_id: int) -> bool:
    store = JobStore.wrap(conn)
    job = store.get(job_id)
    if job is None or not v.can_manage(job.repo):
        raise Forbidden(str(job_id))
    done = store.cancel(job_id)
    audit(conn, v, "job.cancel", repo=job.repo, target_kind="job", target_id=str(job_id))
    return done


# ------------------------------------------------------------------- users


def users(conn: Any, v: Viewer) -> list[dict[str, Any]]:
    if not v.can_manage_users:
        raise Forbidden("users")
    if v.is_super:
        scope_sql, params = "", []
    else:
        scope_sql = ("where u.user_id in (select user_id from gitscout.memberships "
                     "where repo = any(%s) and revoked_at is null)")
        params = [v.admin_repos]
    return _dicts(conn, f"""select u.user_id::text, u.email, u.role, u.display_name, u.created_at, u.disabled_at,
            coalesce(array_agg(m.repo order by m.repo) filter (where m.repo is not null and m.revoked_at is null), '{{}}') as repos
        from gitscout.app_users u left join gitscout.memberships m using (user_id)
        {scope_sql} group by u.user_id order by u.created_at""", params)


def provision(conn: Any, v: Viewer, *, user_id: str, email: str, role: str, repos: Sequence[str]) -> None:
    """Create or update the app_users row for an invited account, and grant repositories.
    An admin client may invite clients only, to repositories they administer."""
    if role not in ROLES:
        raise ValueError("unknown role")
    if not v.is_super:
        if v.role != "client_admin" or role != "client_viewer":
            raise Forbidden("role")
        if any(r not in v.admin_repos for r in repos):
            raise Forbidden("repos")
    with conn.cursor() as cur:
        cur.execute("""insert into gitscout.app_users (user_id, email, role, created_by)
                       values (%s, %s, %s, %s)
                       on conflict (user_id) do update set role = case when %s then excluded.role
                           else gitscout.app_users.role end, disabled_at = null""",
                    [user_id, email.lower(), role, v.user_id, v.is_super])
    if role != "platform_admin":
        for repo in repos:
            _grant(conn, user_id, repo, role, v.user_id)
    audit(conn, v, "user.invite", target_kind="user", target_id=user_id,
          details={"email": email.lower(), "role": role, "repos": list(repos)})


def set_disabled(conn: Any, v: Viewer, user_id: str, disabled: bool) -> None:
    if not v.is_super or user_id == v.user_id:
        raise Forbidden("user")
    with conn.cursor() as cur:
        cur.execute("update gitscout.app_users set disabled_at = case when %s then now() end where user_id = %s",
                    [disabled, user_id])
    audit(conn, v, "user.disable" if disabled else "user.enable", target_kind="user", target_id=user_id)


def revoke(conn: Any, v: Viewer, user_id: str, repo: str) -> None:
    if not v.can_manage(repo):
        raise Forbidden(repo)
    with conn.cursor() as cur:
        cur.execute("update gitscout.memberships set revoked_at = now() where user_id = %s and repo = %s",
                    [user_id, repo])
    audit(conn, v, "access.revoke", repo=repo, target_kind="user", target_id=user_id)
