"""Public playback port for already resolved loudness settings."""

import logging
from typing import Protocol, runtime_checkable

from party_player.deck_controller import DeckController
from party_player.loudness import ResolvedLoudnessSettings


@runtime_checkable
class ResolvedLoudnessPlayback(Protocol):
    """Apply prepared gain without resolving metadata or analysis sources."""

    def apply_resolved_loudness(
        self,
        deck_id: str,
        settings: ResolvedLoudnessSettings | None,
    ) -> None: ...


class DeckResolvedLoudnessPlayback:
    """Map resolved settings to independent deck normalization factors."""

    def __init__(self, deck_a: DeckController, deck_b: DeckController) -> None:
        self._decks = {"A": deck_a, "B": deck_b}
        self._logger = logging.getLogger(__name__)

    def apply_resolved_loudness(
        self,
        deck_id: str,
        settings: ResolvedLoudnessSettings | None,
    ) -> None:
        try:
            deck = self._decks[deck_id]
        except KeyError as exc:
            raise ValueError(f"Unbekanntes Deck: {deck_id}") from exc
        deck.set_resolved_loudness(settings)
        if settings is not None:
            self._logger.info(
                "Deck %s: Lautheitsanpassung %.2f dB (%s)%s",
                deck_id,
                settings.effective_gain_db,
                settings.source,
                ", Clip-Schutz aktiv" if settings.peak_limited else "",
            )
