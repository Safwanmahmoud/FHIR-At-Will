"""Policy types for narrative de-identification."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class DeidMode(StrEnum):
    OFF = "off"
    ADVISORY = "advisory"
    ENFORCED = "enforced"


class DeidProfile(StrEnum):
    HIPAA_SAFE_HARBOR = "hipaa_safe_harbor"
    HIPAA_LIMITED_DATA_SET = "hipaa_limited_data_set"


class _SettingsLike(Protocol):
    @property
    def deid_mode(self) -> DeidMode: ...

    @property
    def deid_profile(self) -> DeidProfile: ...

    @property
    def deid_allow_audio_egress(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class DeidPolicy:
    mode: DeidMode
    profile: DeidProfile
    allow_audio_egress: bool

    @property
    def enabled(self) -> bool:
        return self.mode is not DeidMode.OFF

    @property
    def enforced(self) -> bool:
        return self.mode is DeidMode.ENFORCED

    @classmethod
    def from_settings(cls, settings: _SettingsLike) -> DeidPolicy:
        return cls(
            mode=settings.deid_mode,
            profile=settings.deid_profile,
            allow_audio_egress=settings.deid_allow_audio_egress,
        )


__all__ = ["DeidMode", "DeidPolicy", "DeidProfile"]
