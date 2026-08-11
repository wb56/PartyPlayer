"""Accessibility guardrails for the central Dark DJ color tokens."""

from party_player.ui import theme


def _relative_luminance(color: str) -> float:
    channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [
        channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast_ratio(first: str, second: str) -> float:
    lighter, darker = sorted(
        (_relative_luminance(first), _relative_luminance(second)),
        reverse=True,
    )
    return (lighter + 0.05) / (darker + 0.05)


def test_primary_and_muted_text_meet_normal_text_contrast() -> None:
    assert _contrast_ratio(theme.TEXT, theme.SURFACE) >= 4.5
    assert _contrast_ratio(theme.TEXT_MUTED, theme.SURFACE) >= 4.5


def test_status_and_deck_accents_meet_large_text_contrast() -> None:
    for accent in (*theme.DECK_ACCENTS.values(), theme.ON_AIR, theme.READY, theme.ERROR):
        assert _contrast_ratio(accent, theme.SURFACE_RAISED) >= 3.0
