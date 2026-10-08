import asyncio

import httpx
from helpers import profile

from gitscout.enrich import Enricher
from gitscout.models import Interaction
from gitscout.storage import Store


def run(coro):
    return asyncio.run(coro)


def _seed(store: Store, *logins: str, user_type: str = "User") -> None:
    store.add_interactions([Interaction("o/r", login, "stars", user_type) for login in logins])


async def _enrich(fake, make_client, logins, *, user_type="User", http=None, host_check=None, **kw):
    async def allow(_host: str) -> bool:
        return True

    with Store(":memory:") as store:
        _seed(store, *logins, user_type=user_type)
        async with make_client(fake) as client:
            async with Enricher(
                client, store, http=http, host_check=host_check or allow, **kw
            ) as enricher:
                summary = await enricher.run()
        emails = {
            (r["login"], r["email"], r["source"], r["confidence"])
            for r in store._conn.execute("SELECT * FROM emails")
        }
        discovered = {
            r["login"]: r["discovered_at"] for r in store._conn.execute("SELECT * FROM users")
        }
        profiles = {r["login"]: dict(r) for r in store._conn.execute("SELECT * FROM users")}
    return summary, emails, discovered, profiles


def test_profile_email_wins_and_short_circuits(fake, make_client):
    fake.add("/users/alice", profile("alice", name="Alice A", email="Alice@Acme.dev"))
    summary, emails, _, profiles = run(_enrich(fake, make_client, ["alice"]))
    assert emails == {("alice", "alice@acme.dev", "profile", 0.95)}
    assert summary.with_email == 1
    assert fake.paths() == ["/users/alice"]  # no further API calls spent
    assert profiles["alice"]["name"] == "Alice A"


def test_events_source_with_name_match(fake, make_client):
    fake.add("/users/bob", profile("bob", name="Bob Builder"))
    fake.add(
        "/users/bob/events/public",
        [
            {"type": "WatchEvent", "payload": {}},
            {
                "type": "PushEvent",
                "payload": {"commits": [{"author": {"email": "bob@bob.dev", "name": "Bob Builder"}}]},
            },
        ],
    )
    _, emails, _, _ = run(_enrich(fake, make_client, ["bob"]))
    assert emails == {("bob", "bob@bob.dev", "events", 0.8)}


def test_events_source_without_name_match_is_penalised(fake, make_client):
    fake.add("/users/bob", profile("bob", name="Bob Builder"))
    fake.add(
        "/users/bob/events/public",
        [{"type": "PushEvent", "payload": {"commits": [{"author": {"email": "x@y.dev", "name": "Zed"}}]}}],
    )
    _, emails, _, _ = run(_enrich(fake, make_client, ["bob"]))
    assert emails == {("bob", "x@y.dev", "events", 0.4)}


def test_events_fallback_when_payload_has_no_commit_list(fake, make_client):
    fake.add("/users/bob", profile("bob"))
    fake.add(
        "/users/bob/events/public",
        [{"type": "PushEvent", "repo": {"name": "bob/proj"}, "payload": {"head": "abc123"}}],
    )
    fake.add(
        "/repos/bob/proj/commits/abc123",
        {"author": {"login": "bob"}, "commit": {"author": {"email": "b@bob.dev", "name": "bob"}}},
    )
    _, emails, _, _ = run(_enrich(fake, make_client, ["bob"]))
    assert emails == {("bob", "b@bob.dev", "events", 0.8)}


def test_events_fallback_ignores_commits_by_other_accounts(fake, make_client):
    fake.add("/users/bob", profile("bob"))
    fake.add(
        "/users/bob/events/public",
        [{"type": "PushEvent", "repo": {"name": "org/proj"}, "payload": {"head": "abc"}}],
    )
    fake.add(
        "/repos/org/proj/commits/abc",
        {"author": {"login": "someone-else"}, "commit": {"author": {"email": "other@x.dev"}}},
    )
    _, emails, _, _ = run(_enrich(fake, make_client, ["bob"]))
    assert emails == set()


def test_repo_commit_source_skips_forks(fake, make_client):
    fake.add("/users/carol", profile("carol"))
    fake.add("/users/carol/events/public", [])
    fake.add(
        "/users/carol/repos",
        [{"full_name": "x/forked", "fork": True}, {"full_name": "carol/tool", "fork": False}],
    )
    fake.add("/repos/carol/tool/commits", [{"commit": {"author": {"email": "carol@carol.dev"}}}])
    _, emails, _, _ = run(_enrich(fake, make_client, ["carol"]))
    assert emails == {("carol", "carol@carol.dev", "commit_api", 0.85)}
    assert "/repos/x/forked/commits" not in fake.paths()


def test_noreply_only_means_no_email_but_user_is_marked_done(fake, make_client):
    fake.add("/users/dina", profile("dina"))
    fake.add("/users/dina/events/public", [])
    fake.add("/users/dina/repos", [{"full_name": "dina/x", "fork": False}])
    fake.add(
        "/repos/dina/x/commits",
        [{"commit": {"author": {"email": "123+dina@users.noreply.github.com"}}}],
    )
    summary, emails, discovered, _ = run(_enrich(fake, make_client, ["dina"]))
    assert emails == set()
    assert summary.processed == 1 and summary.with_email == 0
    assert discovered["dina"] is not None


def test_deep_mode_runs_all_sources(fake, make_client):
    fake.add("/users/eve", profile("eve", email="eve@home.dev"))
    fake.add("/users/eve/events/public", [])
    fake.add("/users/eve/repos", [{"full_name": "eve/x", "fork": False}])
    fake.add("/repos/eve/x/commits", [{"commit": {"author": {"email": "eve@work.dev"}}}])
    _, emails, _, _ = run(_enrich(fake, make_client, ["eve"], deep=True))
    assert {e[1] for e in emails} == {"eve@home.dev", "eve@work.dev"}


def test_bots_are_never_processed(fake, make_client):
    summary, emails, _, profiles = run(_enrich(fake, make_client, ["robot[bot]"], user_type="Bot"))
    assert summary.processed == 0 and profiles == {}
    assert fake.calls == []


def test_deleted_user_is_recorded_as_missing(fake, make_client):
    summary, emails, discovered, profiles = run(_enrich(fake, make_client, ["ghosty"]))
    assert profiles["ghosty"]["profile_found"] == 0
    assert discovered["ghosty"] is not None and emails == set()


def test_api_failure_counts_as_failed_and_is_retried_later(fake, make_client):
    fake.add("/users/fay", {}, status=500)
    summary, _, discovered, _ = run(_enrich(fake, make_client, ["fay"]))
    assert summary.failed == 1 and summary.processed == 0
    assert "fay" not in discovered  # never marked done => picked up by the next run


# ----------------------------------------------------------------- website step


def _site(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _website_fixture(fake, blog="https://carol.dev"):
    fake.add("/users/carol", profile("carol", blog=blog))
    fake.add("/users/carol/events/public", [])
    fake.add("/users/carol/repos", [])


def test_website_scan_finds_mailto(fake, make_client):
    _website_fixture(fake)

    def handler(req: httpx.Request) -> httpx.Response:
        html = '<a href="mailto:Hello@carol.dev">me</a> <img src="a@2x.png">'
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})

    _, emails, _, _ = run(
        _enrich(fake, make_client, ["carol"], scan_websites=True, http=_site(handler))
    )
    assert emails == {("carol", "hello@carol.dev", "website", 0.6)}


def test_website_scan_is_opt_in(fake, make_client):
    _website_fixture(fake)
    hits = []

    def handler(req: httpx.Request) -> httpx.Response:
        hits.append(str(req.url))
        return httpx.Response(200, text="hello@carol.dev", headers={"content-type": "text/html"})

    _, emails, _, _ = run(_enrich(fake, make_client, ["carol"], http=_site(handler)))
    assert emails == set() and hits == []


def test_website_scan_blocks_private_hosts(fake, make_client):
    _website_fixture(fake, blog="http://intranet.local")
    hits = []

    def handler(req: httpx.Request) -> httpx.Response:
        hits.append(str(req.url))
        return httpx.Response(200, text="a@b.dev")

    async def deny(_host: str) -> bool:
        return False

    _, emails, _, _ = run(
        _enrich(
            fake, make_client, ["carol"], scan_websites=True, http=_site(handler), host_check=deny
        )
    )
    assert emails == set() and hits == []


def test_website_redirect_to_private_host_is_blocked(fake, make_client):
    _website_fixture(fake)
    hits = []

    def handler(req: httpx.Request) -> httpx.Response:
        hits.append(req.url.host)
        if req.url.host == "carol.dev":
            return httpx.Response(302, headers={"location": "http://internal.corp/secret"})
        return httpx.Response(200, text="leak@internal.corp", headers={"content-type": "text/html"})

    async def only_public(host: str) -> bool:
        return host != "internal.corp"

    _, emails, _, _ = run(
        _enrich(
            fake,
            make_client,
            ["carol"],
            scan_websites=True,
            http=_site(handler),
            host_check=only_public,
        )
    )
    assert emails == set()
    assert "internal.corp" not in hits


def test_website_scan_never_sends_github_token(fake, make_client):
    _website_fixture(fake)
    seen_auth = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen_auth.append(req.headers.get("authorization"))
        return httpx.Response(200, text="x@carol.dev", headers={"content-type": "text/html"})

    run(_enrich(fake, make_client, ["carol"], scan_websites=True, http=_site(handler)))
    assert seen_auth and all(a is None for a in seen_auth)
