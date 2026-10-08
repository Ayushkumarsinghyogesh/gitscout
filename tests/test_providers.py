"""Optional providers: off by default, and conservative when on."""
from __future__ import annotations

import asyncio

import httpx

from gitscout.config import Settings
from gitscout.models import Profile
from gitscout.providers import load_providers, run_providers
from gitscout.providers.hunter import (
    MAX_CONFIDENCE,
    HunterProvider,
    confidence_from,
    domain_from_profile,
    split_name,
)


def run(coro):
    return asyncio.run(coro)


def client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ----------------------------------------------------------------- registration


def test_no_providers_without_keys():
    assert load_providers(Settings()) == []
    assert Settings().providers_enabled == ()


def test_hunter_is_enabled_by_its_key():
    settings = Settings(hunter_api_key="k")
    providers = load_providers(settings)
    assert [p.name for p in providers] == ["hunter"]
    assert settings.providers_enabled == ("hunter",)


def test_placeholder_keys_are_treated_as_absent(monkeypatch):
    from gitscout.config import load_settings

    monkeypatch.setenv("HUNTER_API_KEY", "<your-key-here>")
    monkeypatch.setenv("GITHUB_TOKENS", "your_token")
    settings = load_settings(":memory:")
    assert settings.hunter_api_key is None
    assert settings.tokens == ()


# -------------------------------------------------------------- domain picking


def test_company_domain_is_taken_from_the_linked_website():
    assert domain_from_profile(Profile(login="a", blog="https://acme.dev/about")) == "acme.dev"
    assert domain_from_profile(Profile(login="a", blog="www.acme.dev")) == "acme.dev"


def test_free_mail_and_platform_hosts_are_refused():
    """Asking Hunter for 'gmail.com' or someone's GitHub Pages site wastes a credit."""
    for blog in (
        "https://gmail.com",
        "https://alice.github.io",
        "https://github.com/alice",
        "https://medium.com/@alice",
        "https://alice.vercel.app",
    ):
        assert domain_from_profile(Profile(login="a", blog=blog)) is None


def test_no_website_means_no_domain():
    assert domain_from_profile(Profile(login="a")) is None
    assert domain_from_profile(Profile(login="a", blog="   ")) is None
    assert domain_from_profile(Profile(login="a", blog="not a url")) is None


def test_split_name():
    assert split_name("Alice Anderson") == ("Alice", "Anderson")
    assert split_name("Alice van der Berg") == ("Alice", "Berg")
    assert split_name("Alice") == ("Alice", None)
    assert split_name(None) == (None, None)
    assert split_name("   ") == (None, None)


def test_confidence_is_rescaled_and_capped():
    assert confidence_from(100) == MAX_CONFIDENCE
    assert confidence_from(0) < confidence_from(90)
    assert confidence_from(None) > 0
    assert confidence_from("junk") > 0
    assert all(0 < confidence_from(v) <= MAX_CONFIDENCE for v in (0, 25, 50, 75, 100))


def test_derived_addresses_never_outrank_github_sourced_ones():
    from gitscout.emails import SOURCE_BASE_CONFIDENCE

    assert MAX_CONFIDENCE < SOURCE_BASE_CONFIDENCE["commit_api"]
    assert MAX_CONFIDENCE < SOURCE_BASE_CONFIDENCE["profile"]


# ------------------------------------------------------------------- lookups


def test_hunter_returns_a_scored_candidate():
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"data": {"email": "Alice@Acme.dev", "score": 90}})

    async def go():
        provider = HunterProvider("key", http=client(handler))
        found = await provider.find(
            Profile(login="alice", name="Alice Anderson", blog="https://acme.dev")
        )
        await provider.aclose()
        return found

    found = run(go())
    assert len(found) == 1
    assert found[0].email == "alice@acme.dev"  # normalised
    assert found[0].source == "hunter"
    assert 0 < found[0].confidence <= MAX_CONFIDENCE
    assert seen["domain"] == "acme.dev" and seen["first_name"] == "Alice"


def test_hunter_is_not_called_without_a_name_or_domain():
    def handler(request):  # pragma: no cover - must never run
        raise AssertionError("Hunter should not have been called")

    async def go():
        provider = HunterProvider("key", http=client(handler))
        no_domain = await provider.find(Profile(login="a", name="Alice Anderson"))
        no_name = await provider.find(Profile(login="a", blog="https://acme.dev"))
        one_name = await provider.find(Profile(login="a", name="Alice", blog="https://acme.dev"))
        await provider.aclose()
        return no_domain, no_name, one_name, provider.calls

    no_domain, no_name, one_name, calls = run(go())
    assert no_domain == [] and no_name == [] and one_name == []
    assert calls == 0


def test_hunter_failures_are_quiet():
    for status, body in ((401, {}), (429, {}), (500, {}), (200, {"data": {"email": None}})):

        def handler(request, status=status, body=body):
            return httpx.Response(status, json=body)

        async def go():
            provider = HunterProvider("key", http=client(handler))
            found = await provider.find(
                Profile(login="a", name="Alice Anderson", blog="https://acme.dev")
            )
            await provider.aclose()
            return found

        assert run(go()) == []


def test_run_providers_stops_at_the_first_hit_and_survives_failures():
    class Boom:
        name = "boom"
        enabled = True

        async def find(self, profile):
            raise RuntimeError("down")

        async def aclose(self):
            pass

    class Hit:
        name = "hit"
        enabled = True

        def __init__(self):
            self.calls = 0

        async def find(self, profile):
            from gitscout.models import EmailCandidate

            self.calls += 1
            return [EmailCandidate("x@y.dev", "hit", 0.5)]

        async def aclose(self):
            pass

    class Disabled(Hit):
        name = "disabled"
        enabled = False

    first, second = Hit(), Hit()
    found = run(run_providers([Boom(), Disabled(), first, second], Profile(login="a")))
    assert [c.email for c in found] == ["x@y.dev"]
    assert (first.calls, second.calls) == (1, 0)  # stopped after the first hit


def test_run_providers_with_nothing_enabled():
    assert run(run_providers([], Profile(login="a"))) == []
