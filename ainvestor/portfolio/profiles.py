"""Paper portfolio profiles for A/B comparison."""

from __future__ import annotations

PROFILE_CONSERVATIVE = "conservative"
PROFILE_AGGRESSIVE = "aggressive"
DEFAULT_PROFILE = PROFILE_CONSERVATIVE

PROFILES: tuple[str, ...] = (PROFILE_CONSERVATIVE, PROFILE_AGGRESSIVE)

PROFILE_LABELS: dict[str, str] = {
    PROFILE_CONSERVATIVE: "Conservadora",
    PROFILE_AGGRESSIVE: "Agresiva",
}


def normalize_profile(profile: str | None) -> str:
    if profile in PROFILES:
        return profile
    return DEFAULT_PROFILE
