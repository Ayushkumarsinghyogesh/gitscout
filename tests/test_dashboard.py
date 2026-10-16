"""The dashboard end to end: login, roles and scoping, adding repositories, runs,
records, CSV download, invitations. Auth, GitHub and email are faked; Postgres is real."""
from __future__ import annotations

import csv
import io
import json
import re
import time
import uuid
from urllib.parse import unquote_plus

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from gitscout.config import Settings
from gitscout.dashboard.app import create_app
from gitscout.dashboard.auth import AuthError, Session

SECRET = "s" * 40
IDS = {"boss@x.dev": str(uuid.uuid4()), "ada@x.dev": str(uuid.uuid4()),
       "cal@x.dev": str(uuid.uuid4()), "nobody@x.dev": str(uuid.uuid4())}


class FakeAuth:
    def __init__(self):
        self.passwords = {e: "correct horse battery" for e in IDS}
        self.links: dict[str, tuple[str, str]] = {}
        self.issued = 0
        self.ttl = 3600

    def _session(self, email):
        self.issued += 1
        return Session(f"at-{email}-{self.issued}", f"rt-{email}", int(time.time()) + self.ttl, IDS[email], email)

    def _email_of(self, token):
        m = re.match(r"(?:at|rt)-(.+?)(?:-\d+)?$", token)
        if not m or m.group(1) not in IDS:
            raise AuthError("invalid token")
        return m.group(1)

    def sign_in(self, email, password):
        if self.passwords.get(email) != password:
            raise AuthError("Invalid login credentials")
        return self._session(email)

    def refresh(self, refresh_token):
        return self._session(self._email_of(refresh_token))

    def user(self, access_token):
        return {"id": IDS[self._email_of(access_token)]}

    def set_password(self, access_token, password):
        self.passwords[self._email_of(access_token)] = password

    def verify(self, token_hash, kind):
        if token_hash not in self.links:
            raise AuthError("expired")
        email, _ = self.links.pop(token_hash)
        return self._session(email)

    def generate_link(self, kind, email):
        IDS.setdefault(email, str(uuid.uuid4()))
        token = uuid.uuid4().hex
        self.links[token] = (email, kind)
        return IDS[email], token, kind

    def sign_out(self, access_token):
        pass


def github(request: httpx.Request) -> httpx.Response:
    repo = request.url.path.removeprefix("/repos/")
    if repo == "o/new":
        return httpx.Response(200, json={"id": 777, "node_id": "R_777", "full_name": "o/new", "name": "new",
                                         "owner": {"login": "o"}, "html_url": "https://github.com/o/new",
                                         "stargazers_count": 5, "forks_count": 1, "topics": [], "private": False})
    if repo == "o/secret":
        return httpx.Response(200, json={"id": 778, "full_name": "o/secret", "private": True})
    if repo == "o/r":
        return httpx.Response(200, json={"id": 1, "full_name": "o/r", "name": "r", "owner": {"login": "o"}, "private": False})
    return httpx.Response(404, json={"message": "Not Found"})


class Mail:
    def __init__(self):
        self.sent = []

    def handler(self, request):
        self.sent.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "m1"})


def q(dsn, sql, params=()):
    with psycopg.connect(dsn, autocommit=True) as c:
        cur = c.execute(sql, params)
        return cur.fetchall() if cur.description else []


@pytest.fixture
def site(pg_dsn):
    q(pg_dsn, "delete from gitscout.memberships; delete from gitscout.app_users; delete from gitscout.jobs; "
              "delete from gitscout.audit_log; delete from gitscout.repositories where repo not in ('o/r', 'o/s')")
    q(pg_dsn, "update gitscout.repositories set enabled = true, added_by = null")
    q(pg_dsn, "insert into gitscout.app_users (user_id, email, role) values (%s, 'boss@x.dev', 'platform_admin'), "
              "(%s, 'ada@x.dev', 'client_admin'), (%s, 'cal@x.dev', 'client_viewer')",
      [IDS["boss@x.dev"], IDS["ada@x.dev"], IDS["cal@x.dev"]])
    q(pg_dsn, "insert into gitscout.memberships (user_id, repo, role) values (%s, 'o/r', 'client_admin'), (%s, 'o/r', 'client_viewer')",
      [IDS["ada@x.dev"], IDS["cal@x.dev"]])
    # one person in o/r and one in o/s so scoping is visible
    q(pg_dsn, "select gitscout.ensure_repo_schema('o/r'), gitscout.ensure_repo_schema('o/s')")
    q(pg_dsn, "delete from gitscout.people where login in ('pat', 'sam')")
    q(pg_dsn, "insert into gitscout.people (login, name, best_email) values ('pat', 'Pat P', 'pat@p.dev'), ('sam', 'Sam S', 'sam@s.dev')")
    q(pg_dsn, 'insert into "Defendermate-Github1-R".interactions (repo, login, kind, occurred_at) values (\'o/r\', \'pat\', \'stars\', now()) on conflict do nothing')
    q(pg_dsn, 'insert into "Defendermate-Github1-S".interactions (repo, login, kind, occurred_at) values (\'o/s\', \'sam\', \'stars\', now()) on conflict do nothing')
    q(pg_dsn, "select gitscout.rebuild_union_views()")
    q(pg_dsn, "select gitscout.refresh_outreach()")
    auth, mail = FakeAuth(), Mail()
    app = create_app(Settings(resend_api_key="re_x", supabase_db_url=pg_dsn), auth=auth, dsn=pg_dsn,
                     session_secret=SECRET, public_url="https://dash.example",
                     github_transport=httpx.MockTransport(github), mail_transport=httpx.MockTransport(mail.handler))
    with TestClient(app, base_url="https://dash.example") as client:
        yield client, auth, mail, pg_dsn
    q(pg_dsn, 'delete from "Defendermate-Github1-R".interactions; delete from "Defendermate-Github1-S".interactions')
    q(pg_dsn, "delete from gitscout.people where login in ('pat', 'sam'); delete from gitscout.memberships; "
              "delete from gitscout.app_users; delete from gitscout.jobs; delete from gitscout.dataset_first_collected; "
              "delete from gitscout.repositories where repo not in ('o/r', 'o/s')")
    q(pg_dsn, "select gitscout.refresh_outreach()")


def login(client, email, password="correct horse battery"):
    r = client.post("/login", data={"email": email, "password": password}, follow_redirects=False)
    return r


def csrf(client, path="/repos"):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).text).group(1)


def test_everything_needs_a_login_and_pages_are_locked_down(site):
    client, *_ = site
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert client.get("/healthz").json() == {"ok": True}
    page = client.get("/login")
    assert "script-src 'none'" in page.headers["content-security-policy"]
    assert page.headers["x-frame-options"] == "DENY"


def test_login_checks_the_password_and_the_provisioned_role(site):
    client, *_ = site
    assert "Wrong+email" in login(client, "boss@x.dev", "nope").headers["location"]
    assert "no+access" in login(client, "nobody@x.dev").headers["location"]     # in Auth, not in app_users
    r = login(client, "boss@x.dev")
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "samesite=lax" in cookie.lower()
    home = client.get("/")
    assert "Super admin" in home.text and "o/r" in home.text and "o/s" in home.text


def test_a_client_sees_only_their_repositories(site):
    client, *_ = site
    login(client, "cal@x.dev")
    assert "o/s" not in client.get("/repos").text
    assert client.get("/repos/o/s").status_code == 403
    records = client.get("/records").text
    assert "Pat P" in records and "Sam S" not in records
    assert client.get("/records/o/s/sam").status_code == 403
    assert client.get("/users").status_code == 403
    body = client.get("/exports/download").content.decode("utf-8-sig")       # "all my repositories"
    names = [row["username"] for row in csv.DictReader(io.StringIO(body))]
    assert names == ["pat"]
    assert client.get("/exports/download?repo=o/s").status_code == 403


def test_csv_download_by_date_range(site):
    client, auth, mail, dsn = site
    login(client, "boss@x.dev")
    r = client.get("/exports/download?repo=o/r&date_from=2026-01-01&date_to=2099-12-31&basis=collected")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert 'attachment; filename="gitscout-o-r-collected-2026-01-01_to_2099-12-31-' in r.headers["content-disposition"]
    assert [x["username"] for x in csv.DictReader(io.StringIO(r.content.decode("utf-8-sig")))] == ["pat"]
    empty = client.get("/exports/download?repo=o/r&date_from=2000-01-01&date_to=2000-01-02")
    assert empty.content.decode("utf-8-sig").count("\n") == 1                 # header only
    bad = client.get("/exports/download?date_from=2026-02-01&date_to=2026-01-01", follow_redirects=False)
    assert bad.status_code == 303 and "after" in bad.headers["location"]
    assert q(dsn, "select count(*) from gitscout.audit_log where action = 'export.download'")[0][0] == 2


def test_adding_repositories_registers_grants_and_queues_a_run(site):
    client, auth, mail, dsn = site
    login(client, "cal@x.dev")
    token = csrf(client)
    r = client.post("/repos/add", data={"csrf": token, "repos": "https://github.com/o/new\no/secret\no/missing\nnot a repo"},
                    follow_redirects=False)
    loc = r.headers["location"]
    assert "Added+o%2Fnew" in loc and "private" in loc and "not+found" in loc
    assert q(dsn, "select enabled, settings->>'max', added_by::text from gitscout.repositories where repo = 'o/new'") == \
        [(True, "2000", IDS["cal@x.dev"])]
    assert q(dsn, "select role from gitscout.memberships where user_id = %s and repo = 'o/new'", [IDS["cal@x.dev"]]) == [("client_viewer",)]
    assert q(dsn, "select repo, kind, trigger, status from gitscout.jobs") == [("o/new", "incremental", "manual", "queued")]
    assert "o/new" in client.get("/repos").text
    # a second click while it is queued is refused, not duplicated
    again = client.post("/repos/o/new/run", data={"csrf": token}, follow_redirects=False)
    assert "already+queued" in again.headers["location"]
    # uploading a file works the same way; an existing repository is joined
    up = client.post("/repos/add", data={"csrf": token}, files={"file": ("list.txt", b"o/r\n", "text/plain")},
                     follow_redirects=False)
    assert "Already+tracked" in up.headers["location"]


def test_forms_without_the_csrf_token_are_refused(site):
    client, *_ = site
    login(client, "boss@x.dev")
    assert client.post("/repos/o/r/run", data={}).status_code == 403
    assert client.post("/repos/o/r/run", data={"csrf": "forged"}).status_code == 403


def test_pausing_needs_an_admin_and_is_audited(site):
    client, auth, mail, dsn = site
    login(client, "cal@x.dev")
    assert client.post("/repos/o/r/enabled", data={"csrf": csrf(client), "enabled": 0}).status_code == 403
    client.post("/logout")
    login(client, "ada@x.dev")
    r = client.post("/repos/o/r/enabled", data={"csrf": csrf(client), "enabled": 0}, follow_redirects=False)
    assert "paused" in r.headers["location"]
    assert q(dsn, "select enabled from gitscout.repositories where repo = 'o/r'") == [(False,)]
    assert q(dsn, "select actor_email from gitscout.audit_log where action = 'repo.disable'") == [("ada@x.dev",)]


def test_invitation_to_first_login(site):
    client, auth, mail, dsn = site
    login(client, "ada@x.dev")
    token = csrf(client, "/users")
    # an admin client may not hand out admin roles or other people's repositories
    assert client.post("/users/invite", data={"csrf": token, "email": "eve@x.dev", "role": "platform_admin"}).status_code == 403
    assert client.post("/users/invite", data={"csrf": token, "email": "eve@x.dev", "repos": ["o/s"]}).status_code == 403
    r = client.post("/users/invite", data={"csrf": token, "email": "Dee@X.dev", "role": "client_viewer", "repos": ["o/r"]},
                    follow_redirects=False)
    assert "Invitation+emailed" in r.headers["location"]
    (sent,) = mail.sent
    link = re.search(r"https://dash\.example/auth/confirm\?token_hash=(\w+)&type=invite", sent["text"])
    assert sent["to"] == ["dee@x.dev"] and link
    assert q(dsn, "select role from gitscout.app_users where email = 'dee@x.dev'") == [("client_viewer",)]
    # the invitee: GET shows a button (scanners cannot spend the token), POST signs in
    client.post("/logout")
    page = client.get(f"/auth/confirm?token_hash={link.group(1)}&type=invite")
    assert page.status_code == 200 and link.group(1) in auth.links
    r = client.post("/auth/confirm", data={"token_hash": link.group(1), "kind": "invite"}, follow_redirects=False)
    assert r.headers["location"] == "/account/password?first=1"
    form = csrf(client, "/account/password?first=1")
    short = client.post("/account/password", data={"csrf": form, "password": "short", "confirm": "short"}, follow_redirects=False)
    assert "12+characters" in short.headers["location"]
    client.post("/account/password", data={"csrf": form, "password": "a long new passphrase", "confirm": "a long new passphrase"})
    assert auth.passwords["dee@x.dev"] == "a long new passphrase"
    assert "Pat P" in client.get("/records").text
    # the link worked once
    assert "expired" in client.post("/auth/confirm", data={"token_hash": link.group(1)}, follow_redirects=False).headers["location"]


def test_an_expiring_session_is_refreshed_and_a_disabled_user_is_out(site):
    client, auth, mail, dsn = site
    auth.ttl = 30                                           # already inside the refresh margin
    login(client, "cal@x.dev")
    before = auth.issued
    assert client.get("/repos").status_code == 200 and auth.issued == before + 1
    q(dsn, "update gitscout.app_users set disabled_at = now() where email = 'cal@x.dev'")
    r = client.get("/repos", follow_redirects=False)
    assert r.status_code == 303 and "no access" in unquote_plus(r.headers["location"])


def test_forgot_password_does_not_reveal_accounts(site):
    client, auth, mail, dsn = site
    known = client.post("/auth/forgot", data={"email": "cal@x.dev"}, follow_redirects=False)
    unknown = client.post("/auth/forgot", data={"email": "ghost@x.dev"}, follow_redirects=False)
    assert known.headers["location"] == unknown.headers["location"]
    assert [m["to"] for m in mail.sent] == [["cal@x.dev"]]


def test_super_admin_sees_jobs_and_system(site):
    client, auth, mail, dsn = site
    login(client, "boss@x.dev")
    token = csrf(client)
    client.post("/repos/o/s/run", data={"csrf": token})
    (job_id,) = q(dsn, "select id from gitscout.jobs")[0]
    assert "#" + str(job_id) in client.get("/jobs").text
    assert "queued" in client.get(f"/jobs/{job_id}").text
    r = client.post(f"/jobs/{job_id}/cancel", data={"csrf": token}, follow_redirects=False)
    assert "Cancelled" in r.headers["location"]
    assert "Workers" in client.get("/").text


def test_every_page_renders_for_each_role(site):
    client, auth, mail, dsn = site
    for email, pages in (("boss@x.dev", ["/", "/repos", "/repos/o/r", "/records", "/records?repo=o/r&q=pat&email=1",
                                         "/records/o/r/pat", "/exports", "/exports?repo=o/r", "/jobs", "/users",
                                         "/account/password"]),
                         ("cal@x.dev", ["/", "/repos", "/repos/o/r", "/records", "/records/o/r/pat", "/exports", "/jobs"])):
        login(client, email)
        for path in pages:
            r = client.get(path)
            assert r.status_code == 200, (email, path, r.text[:300])
            assert "Traceback" not in r.text
        client.post("/logout")
    assert client.get("/records/o/r/nobody", follow_redirects=False).status_code == 303   # signed out


def test_the_worker_can_run_inside_the_dashboard_process(pg_dsn, tmp_path):
    q(pg_dsn, "delete from gitscout.workers; delete from gitscout.jobs")
    q(pg_dsn, "update gitscout.repositories set enabled = false")
    settings = Settings(tokens=("tok",), db_path=str(tmp_path / "e.db"), supabase_db_url=pg_dsn)
    app = create_app(settings, auth=FakeAuth(), dsn=pg_dsn, session_secret=SECRET, embedded_worker=True)
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"ok": True}
        for _ in range(50):                                   # the first tick reports the worker alive
            if q(pg_dsn, "select count(*) from gitscout.workers")[0][0]:
                break
            time.sleep(0.1)
        assert q(pg_dsn, "select stopped_at is null from gitscout.workers") == [(True,)]
    assert q(pg_dsn, "select stopped_at is not null from gitscout.workers") == [(True,)]   # clean stop
    q(pg_dsn, "update gitscout.repositories set enabled = true")
