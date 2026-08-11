"""ReplayGain persistence and safe playback-time gain resolution."""

from dataclasses import dataclass
import logging
import math

from party_player.database.connection import Database


@dataclass(frozen=True, slots=True)
class TrackLoudness:
    track_id: int
    replaygain_track_gain_db: float | None = None
    replaygain_track_peak: float | None = None
    replaygain_album_gain_db: float | None = None
    replaygain_album_peak: float | None = None
    manual_gain_db: float | None = None
    replaygain_scanned_at: str | None = None
    metadata_status: str = "NOT_ANALYSED"
    integrated_loudness_lufs: float | None = None
    loudness_range_lu: float | None = None
    true_peak_dbfs: float | None = None
    analysis_source: str = "NONE"
    analysis_version: str | None = None
    analysed_at: str | None = None
    analysis_method: str | None = None
    analysis_status: str = "NOT_ANALYSED"
    analysis_error: str | None = None


@dataclass(frozen=True, slots=True)
class ResolvedLoudnessSettings:
    requested_gain_db: float
    effective_gain_db: float
    linear_gain_factor: float
    source: str
    peak_limited: bool
    normalization_mode: str
    runtime_clip_protection_enabled: bool = False
    output_peak_ceiling_dbfs: float = 0.0


class LoudnessRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def get(self, track_id: int) -> TrackLoudness:
        with self._database.connect() as connection:
            row = connection.execute(
                """SELECT track_id, replaygain_track_gain_db, replaygain_track_peak,
                          replaygain_album_gain_db, replaygain_album_peak, manual_gain_db,
                          replaygain_scanned_at, metadata_status
                          , integrated_loudness_lufs, loudness_range_lu, true_peak_dbfs,
                          analysis_source, analysis_version, analysed_at,
                          analysis_method, analysis_status, analysis_error
                   FROM track_loudness WHERE track_id = ?""",
                (track_id,),
            ).fetchone()
        return TrackLoudness(**dict(row)) if row else TrackLoudness(track_id)

    def save_replaygain(
        self,
        track_id: int,
        track_gain_db: float | None,
        track_peak: float | None,
        album_gain_db: float | None,
        album_peak: float | None,
    ) -> None:
        complete = (
            track_gain_db is not None
            and track_peak is not None
            or album_gain_db is not None
            and album_peak is not None
        )
        status = "COMPLETE" if complete else "INCOMPLETE"
        source = (
            "REPLAYGAIN_TAG" if track_gain_db is not None or album_gain_db is not None else "NONE"
        )
        with self._database.connect() as connection:
            connection.execute(
                """INSERT INTO track_loudness
                       (track_id, replaygain_track_gain_db, replaygain_track_peak,
                        replaygain_album_gain_db, replaygain_album_peak, analysis_source,
                        replaygain_scanned_at, metadata_status)
                   VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
                   ON CONFLICT(track_id) DO UPDATE SET
                       replaygain_track_gain_db = COALESCE(
                           excluded.replaygain_track_gain_db,
                           track_loudness.replaygain_track_gain_db
                       ),
                       replaygain_track_peak = COALESCE(
                           excluded.replaygain_track_peak,
                           track_loudness.replaygain_track_peak
                       ),
                       replaygain_album_gain_db = COALESCE(
                           excluded.replaygain_album_gain_db,
                           track_loudness.replaygain_album_gain_db
                       ),
                       replaygain_album_peak = COALESCE(
                           excluded.replaygain_album_peak,
                           track_loudness.replaygain_album_peak
                       ),
                       replaygain_scanned_at = CURRENT_TIMESTAMP,
                       metadata_status = excluded.metadata_status,
                       analysis_source = CASE
                           WHEN excluded.replaygain_track_gain_db IS NOT NULL
                             OR excluded.replaygain_album_gain_db IS NOT NULL
                           THEN 'REPLAYGAIN_TAG' ELSE track_loudness.analysis_source END,
                       updated_at = CURRENT_TIMESTAMP""",
                (
                    track_id,
                    track_gain_db,
                    track_peak,
                    album_gain_db,
                    album_peak,
                    source,
                    status,
                ),
            )

    def mark_replaygain_failed(self, track_id: int) -> None:
        """Record a read failure without deleting previously valid gain values."""
        with self._database.connect() as connection:
            connection.execute(
                """INSERT INTO track_loudness (track_id, metadata_status)
                   VALUES (?, 'FAILED')
                   ON CONFLICT(track_id) DO UPDATE SET
                       metadata_status = 'FAILED',
                       updated_at = CURRENT_TIMESTAMP""",
                (track_id,),
            )

    def save_manual_gain(self, track_id: int, gain_db: float | None) -> None:
        with self._database.connect() as connection:
            connection.execute(
                """INSERT INTO track_loudness (track_id, manual_gain_db)
                   VALUES (?, ?)
                   ON CONFLICT(track_id) DO UPDATE SET
                       manual_gain_db = excluded.manual_gain_db,
                       updated_at = CURRENT_TIMESTAMP""",
                (track_id, gain_db),
            )

    def save_analysis(
        self,
        track_id: int,
        *,
        integrated_loudness_lufs: float,
        loudness_range_lu: float,
        true_peak_dbfs: float,
        source: str,
        version: str,
        method: str,
    ) -> None:
        """Persist offline measurements separately from source-file metadata."""
        with self._database.connect() as connection:
            connection.execute(
                """INSERT INTO track_loudness
                       (track_id, integrated_loudness_lufs, loudness_range_lu,
                        true_peak_dbfs, analysis_source, analysis_version,
                        analysed_at, analysis_method, analysis_status, analysis_error)
                   VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?, 'COMPLETE', NULL)
                   ON CONFLICT(track_id) DO UPDATE SET
                       integrated_loudness_lufs = excluded.integrated_loudness_lufs,
                       loudness_range_lu = excluded.loudness_range_lu,
                       true_peak_dbfs = excluded.true_peak_dbfs,
                       analysis_source = excluded.analysis_source,
                       analysis_version = excluded.analysis_version,
                       analysed_at = CURRENT_TIMESTAMP,
                       analysis_method = excluded.analysis_method,
                       analysis_status = 'COMPLETE',
                       analysis_error = NULL,
                       updated_at = CURRENT_TIMESTAMP""",
                (
                    track_id,
                    integrated_loudness_lufs,
                    loudness_range_lu,
                    true_peak_dbfs,
                    source,
                    version,
                    method,
                ),
            )

    def mark_analysis_failed(self, track_id: int, error: str) -> None:
        """Store a bounded failure message without deleting older measurements."""
        with self._database.connect() as connection:
            connection.execute(
                """INSERT INTO track_loudness
                       (track_id, analysis_status, analysis_error, analysed_at)
                   VALUES (?, 'FAILED', ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(track_id) DO UPDATE SET
                       analysis_status = 'FAILED',
                       analysis_error = excluded.analysis_error,
                       analysed_at = CURRENT_TIMESTAMP,
                       updated_at = CURRENT_TIMESTAMP""",
                (track_id, error[:500]),
            )


class LoudnessService:
    """Resolve independent per-track gain without touching source files."""

    def __init__(
        self,
        repository: LoudnessRepository,
        *,
        enabled: bool = True,
        clip_protection_enabled: bool = True,
        mode: str = "TRACK",
        maximum_positive_gain_db: float = 8.0,
        maximum_negative_gain_db: float = -12.0,
        maximum_output_peak_db: float = 0.0,
        headroom_db: float = 1.0,
        fallback_positive_gain_db: float = 3.0,
        target_loudness_lufs: float = -14.0,
        maximum_backend_volume_factor: float = 2.0,
    ) -> None:
        self.repository = repository
        self.enabled = enabled
        self.clip_protection_enabled = clip_protection_enabled
        self.mode = mode if mode in {"OFF", "TRACK", "ALBUM"} else "TRACK"
        self.maximum_positive_gain_db = maximum_positive_gain_db
        self.maximum_negative_gain_db = maximum_negative_gain_db
        self.maximum_output_peak_db = maximum_output_peak_db
        self.headroom_db = headroom_db
        self.fallback_positive_gain_db = fallback_positive_gain_db
        self.target_loudness_lufs = target_loudness_lufs
        self.maximum_backend_volume_factor = max(1.0, maximum_backend_volume_factor)
        self._logger = logging.getLogger(__name__)

    def get(self, track_id: int) -> TrackLoudness:
        return self.repository.get(track_id)

    def save_manual_gain(self, track_id: int, gain_db: float | None) -> None:
        if gain_db is not None and (not math.isfinite(gain_db) or not -12.0 <= gain_db <= 12.0):
            raise ValueError("Die manuelle Anpassung muss zwischen -12 dB und +12 dB liegen.")
        self.repository.save_manual_gain(track_id, gain_db)

    def resolve(self, track_id: int) -> ResolvedLoudnessSettings:
        """Resolve manual > selected ReplayGain scope with an explicit album fallback.

        ALBUM prefers a finite album gain and otherwise falls back to finite track
        gain. TRACK intentionally never consumes album gain. A manual value has
        priority in both modes.
        """
        data = self.repository.get(track_id)
        if not self.enabled or self.mode == "OFF":
            return self._resolved(0.0, 0.0, "NONE", False)
        requested: float | None = self._finite(data.manual_gain_db)
        source = "MANUAL" if requested is not None else "NONE"
        peak: float | None = data.replaygain_track_peak if requested is not None else None
        album_gain = self._finite(data.replaygain_album_gain_db)
        track_gain = self._finite(data.replaygain_track_gain_db)
        if requested is None and self.mode == "ALBUM" and album_gain is not None:
            requested = album_gain
            peak = data.replaygain_album_peak
            source = "REPLAYGAIN_TAG"
        if requested is None and track_gain is not None:
            requested = track_gain
            peak = data.replaygain_track_peak
            source = "REPLAYGAIN_TAG"
        if (
            requested is None
            and data.integrated_loudness_lufs is not None
            and math.isfinite(data.integrated_loudness_lufs)
        ):
            requested = self.target_loudness_lufs - data.integrated_loudness_lufs
            peak = (
                10.0 ** (data.true_peak_dbfs / 20.0)
                if data.true_peak_dbfs is not None and math.isfinite(data.true_peak_dbfs)
                else None
            )
            source = "ANALYSIS"
        if requested is None or not math.isfinite(requested):
            return self._resolved(0.0, 0.0, "NONE", False)
        unclamped = requested
        effective = max(
            self.maximum_negative_gain_db,
            min(
                requested,
                self.maximum_positive_gain_db,
                20.0 * math.log10(self.maximum_backend_volume_factor),
            ),
        )
        limited = effective != requested
        if effective > 0 and self.clip_protection_enabled:
            if peak is not None and math.isfinite(peak) and peak > 0:
                peak_dbfs = 20.0 * math.log10(peak)
                safe_output_peak_db = self.maximum_output_peak_db - self.headroom_db
                safe_gain = safe_output_peak_db - peak_dbfs
                if effective > safe_gain:
                    effective = max(0.0, safe_gain)
                    limited = True
            elif effective > self.fallback_positive_gain_db:
                effective = self.fallback_positive_gain_db
                limited = True
        if limited:
            self._logger.info(
                "Lautheits-Gain für Titel %s begrenzt: %.2f dB auf %.2f dB",
                track_id,
                unclamped,
                effective,
            )
        return self._resolved(unclamped, effective, source, limited)

    @staticmethod
    def _finite(value: float | None) -> float | None:
        return value if value is not None and math.isfinite(value) else None

    def _resolved(
        self, requested: float, effective: float, source: str, limited: bool
    ) -> ResolvedLoudnessSettings:
        return ResolvedLoudnessSettings(
            requested,
            effective,
            10.0 ** (effective / 20.0),
            source,
            limited,
            self.mode if self.enabled else "OFF",
            self.clip_protection_enabled,
            self.maximum_output_peak_db - self.headroom_db,
        )
