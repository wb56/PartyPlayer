"""Shared structured operational logging fields."""

import logging


def log_queue_event(
    logger: logging.Logger,
    event_code: str,
    *,
    session_id: int,
    queue_id: int | None,
    track_id: int | None,
    source: str | None,
    status: str,
    reason_code: str,
) -> None:
    """Emit one searchable queue/history event with a stable field contract."""
    logger.info(
        event_code,
        extra={
            "event_code": event_code,
            "session_id": session_id,
            "queue_id": queue_id,
            "track_id": track_id,
            "source": source,
            "status": status,
            "reason_code": reason_code,
        },
    )
