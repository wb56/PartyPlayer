"""Stable history reason codes and their German presentation."""

from party_player.enums import HistoryReasonCode


_GERMAN_REASON_TEXT = {
    HistoryReasonCode.OPERATOR_SKIP: "Vom Operator übersprungen",
    HistoryReasonCode.DECK_EJECT: "Titel vom Deck ausgeworfen",
    HistoryReasonCode.DECK_STOP: "Wiedergabe am Deck gestoppt",
    HistoryReasonCode.APPLICATION_SHUTDOWN: "Anwendung während der Wiedergabe beendet",
    HistoryReasonCode.TRACK_REPLACED: "Titel auf dem Deck ersetzt",
    HistoryReasonCode.PLAYBACK_ERROR: "Wiedergabefehler",
    HistoryReasonCode.UNSPECIFIED: "Kein Grund angegeben",
    HistoryReasonCode.LEGACY_REASON: "Grund aus älterer Programmversion",
}


def history_reason_text(code: HistoryReasonCode | str | None) -> str:
    """Resolve a persisted reason code at the presentation boundary."""
    if code is None:
        return ""
    try:
        normalized = HistoryReasonCode(code)
    except ValueError:
        normalized = HistoryReasonCode.LEGACY_REASON
    return _GERMAN_REASON_TEXT[normalized]
