"""Optional third-party email providers.

Every provider is **off unless its API key is set**, so the default install never
sends a GitHub user's details to a third party and never spends money. Enable one by
setting its key in ``.env``; ``gitscout doctor`` reports which are active.

A provider only ever sees users GitHub itself could not resolve, and only the fields
needed to look them up (name + company/website domain). The contract is deliberately
tiny -- a provider is anything with ``name``, ``enabled`` and ``find(profile)``.
"""
from __future__ import annotations

import logging
from typing import Protocol, Sequence, runtime_checkable

from ..config import Settings
from ..models import EmailCandidate, Profile

log = logging.getLogger(__name__)


@runtime_checkable
class EmailProvider(Protocol):
    """Finds an email for a person we could not resolve from GitHub alone."""

    name: str

    @property
    def enabled(self) -> bool: ...

    async def find(self, profile: Profile) -> list[EmailCandidate]: ...

    async def aclose(self) -> None: ...


def load_providers(settings: Settings) -> list[EmailProvider]:
    """Instantiate every provider whose credentials are present."""
    from .hunter import HunterProvider

    providers: list[EmailProvider] = []
    if settings.hunter_api_key:
        providers.append(HunterProvider(settings.hunter_api_key))
    if providers:
        log.info("Email providers enabled: %s", ", ".join(p.name for p in providers))
    return providers


async def run_providers(
    providers: Sequence[EmailProvider],
    profile: Profile,
) -> list[EmailCandidate]:
    """Try each provider in turn, stopping at the first hit. Failures are non-fatal."""
    for provider in providers:
        if not provider.enabled:
            continue
        try:
            found = await provider.find(profile)
        except Exception as exc:  # noqa: BLE001 - a paid API being down must not stop a run
            log.warning("provider %s failed for %s: %s", provider.name, profile.login, exc)
            continue
        if found:
            return found
    return []


__all__ = ["EmailProvider", "load_providers", "run_providers"]
