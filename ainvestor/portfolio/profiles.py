"""Paper portfolio profiles."""

from __future__ import annotations

PROFILE_CONSERVATIVE = "conservative"  # legacy — no longer active
PROFILE_AGGRESSIVE = "aggressive"  # legacy — maps to extreme
PROFILE_EXTREME = "extreme"
DEFAULT_PROFILE = PROFILE_EXTREME

PROFILES: tuple[str, ...] = (PROFILE_EXTREME,)

PROFILE_LABELS: dict[str, str] = {
    PROFILE_EXTREME: "Extrema",
}


def normalize_profile(profile: str | None) -> str:
    if profile in PROFILES:
        return profile
    if profile in (PROFILE_CONSERVATIVE, PROFILE_AGGRESSIVE):
        return PROFILE_EXTREME
    return DEFAULT_PROFILE
