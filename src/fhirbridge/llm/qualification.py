"""Model qualification tiers (AGENTS.md 7.5).

A model must clear ``MIN_QUALIFICATION_TIER`` before the gateway will send it
clinical narrative. This module answers "what tier is this model?" from a small
static registry. Anything not listed is :attr:`QualificationTier.UNQUALIFIED`,
which the default ``bronze`` minimum rejects.

This is deliberately conservative. The real qualification harness — running a
goldset against a candidate model and measuring it — is milestone M5. Until then
these tiers are a manually curated allowlist, not a measured claim, and the
honest default is to refuse an unvetted model. An operator who wants to run an
arbitrary BYOM model opts in explicitly by lowering ``MIN_QUALIFICATION_TIER`` to
``unqualified``; they are not silently permitted.
"""

from __future__ import annotations

from typing import Final

from fhirbridge.config import QualificationTier

_REGISTRY: Final[dict[str, QualificationTier]] = {
    # OpenAI (via OpenRouter or direct)
    "openai/gpt-4o": QualificationTier.GOLD,
    "openai/gpt-4o-mini": QualificationTier.SILVER,
    "openai/gpt-4.1": QualificationTier.GOLD,
    "openai/gpt-4.1-mini": QualificationTier.SILVER,
    # Anthropic
    "anthropic/claude-3.5-sonnet": QualificationTier.GOLD,
    "anthropic/claude-3.7-sonnet": QualificationTier.GOLD,
    "anthropic/claude-3.5-haiku": QualificationTier.SILVER,
    # Google
    "google/gemini-2.0-flash-001": QualificationTier.SILVER,
    "google/gemini-2.5-pro": QualificationTier.GOLD,
}
"""Provisional, manually curated tiers pending the M5 qualification harness."""


def _normalize(model: str) -> str:
    """Reduce a model id to its registry key.

    litellm's OpenRouter models carry an ``openrouter/`` routing prefix that is
    not part of the vendor's own name, so it is stripped before lookup.
    """
    return model.strip().lower().removeprefix("openrouter/")


def resolve_tier(model: str) -> QualificationTier:
    """Return the qualification tier for ``model``; unknown models are unqualified."""
    return _REGISTRY.get(_normalize(model), QualificationTier.UNQUALIFIED)


def is_registered(model: str) -> bool:
    """Whether this build has an explicit tier for ``model``."""
    return _normalize(model) in _REGISTRY


__all__ = ["is_registered", "resolve_tier"]
