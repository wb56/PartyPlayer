"""Main-process run persistence adapter for metadata analysis package 6A."""

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from uuid import uuid4

from party_player.database.connection import Database
from party_player.metadata_analysis_contracts import (
    FileSnapshot,
    MetadataAnalysisBackendKind,
    MetadataAnalysisJob,
    MetadataAnalysisOutcome,
    MetadataAnalysisRequest,
    MetadataAnalysisResult,
    MetadataFieldSuggestion,
)
from party_player.metadata_analysis_profiles import (
    ALGORITHM_VERSION,
    HIGH_CONFIDENCE,
    PROFILE_CONFIGURATIONS,
    TEMPO_CHANGE_STABILITY,
    MetadataAnalysisProfile,
)
from party_player.metadata_persistence import (
    AnalysisRunRepository,
    AnalysisRunStatus,
    serialize_metadata_value,
)
from party_player.metadata_rules import MetadataFieldKey, MetadataReviewStatus


class SqliteAnalysisRunPersistencePort:
    """Persist run lifecycle in the main process; never pass it to a worker."""

    def __init__(self, database: Database) -> None:
        self._database = database
        self._runs = AnalysisRunRepository(database)

    def create_job(self, request: MetadataAnalysisRequest) -> MetadataAnalysisJob:
        snapshot = request.input_snapshot
        run = self._runs.create(
            request.track_id,
            request.analysis_profile,
            request.analysis_version,
            snapshot.normalized_path,
            snapshot.size,
            snapshot.modified_ns,
            priority=request.priority,
            fingerprint=snapshot.fingerprint,
        )
        return MetadataAnalysisJob(
            str(uuid4()),
            run.run_id,
            request.track_id,
            snapshot,
            request.analysis_profile,
            request.analysis_version,
            request.requested_kinds,
            request.priority,
            request.timeout_seconds,
            datetime.now(timezone.utc).isoformat(),
            request.backend,
            request.technical_options,
        )

    def recover_interrupted_runs(self) -> int:
        """Never treat a RUNNING row left by a prior process as successful."""
        with self._database.connect() as connection:
            cursor = connection.execute(
                """UPDATE metadata_analysis_runs
                   SET status='FAILED', finished_at=CURRENT_TIMESTAMP,
                       error_code='PROCESS_INTERRUPTED',
                       error_text='Analyse wurde durch einen früheren Programmabbruch unterbrochen.'
                   WHERE status='RUNNING'"""
            )
        return max(0, cursor.rowcount)

    def mark_running(self, job: MetadataAnalysisJob) -> None:
        run = self._runs.start(job.run_id)
        if run.status is not AnalysisRunStatus.RUNNING:
            raise RuntimeError("Persistenter Analyseauftrag ist nicht mehr wartend")

    def finish(self, result: MetadataAnalysisResult) -> None:
        if result.outcome in {
            MetadataAnalysisOutcome.SUCCESS,
            MetadataAnalysisOutcome.PARTIAL_SUCCESS,
        }:
            status = AnalysisRunStatus.COMPLETED
        elif result.outcome is MetadataAnalysisOutcome.CANCELLED:
            status = AnalysisRunStatus.CANCELLED
        else:
            status = AnalysisRunStatus.FAILED
        self._runs.finish(
            result.run_id,
            status,
            error_code=result.error_code or None,
            error_text=result.error_text or None,
        )

    def pending_jobs(
        self, ffmpeg: Path, ffprobe: Path, *, limit: int = 1000
    ) -> tuple[MetadataAnalysisJob, ...]:
        with self._database.connect() as connection:
            rows = connection.execute(
                """SELECT id,track_id,analysis_profile,analysis_version,priority,
                          file_path_snapshot,file_size,file_modified_ns,fingerprint,attempt_count
                   FROM metadata_analysis_runs WHERE status='PENDING'
                   ORDER BY priority DESC,created_at,id LIMIT ?""",
                (max(1, min(limit, 10_000)),),
            ).fetchall()
        jobs = []
        for row in rows:
            try:
                profile = MetadataAnalysisProfile(str(row["analysis_profile"]))
            except ValueError:
                continue
            configuration = PROFILE_CONFIGURATIONS[profile]
            jobs.append(
                MetadataAnalysisJob(
                    f"resume-{int(row['id'])}-{int(row['attempt_count'])}",
                    int(row["id"]),
                    int(row["track_id"]),
                    FileSnapshot(
                        str(Path(str(row["file_path_snapshot"])).resolve()),
                        int(row["file_size"]),
                        int(row["file_modified_ns"]),
                        str(row["fingerprint"]) if row["fingerprint"] is not None else None,
                    ),
                    profile.value,
                    str(row["analysis_version"]),
                    configuration.requested_kinds,
                    int(row["priority"]),
                    configuration.timeout_seconds,
                    datetime.now(timezone.utc).isoformat(),
                    MetadataAnalysisBackendKind.FFMPEG_TEMPO,
                    (
                        ("ffmpeg", str(ffmpeg)),
                        ("ffprobe", str(ffprobe)),
                        ("segment_strategy", configuration.segment_strategy),
                    ),
                )
            )
        return tuple(jobs)


class SqliteAnalysisResultPersistencePort:
    """Atomically persist valid suggestions, bounded metrics, ranges and run completion."""

    _FIELD_MAP = {
        "bpm": MetadataFieldKey.BPM,
        "alternative_bpm": MetadataFieldKey.ALTERNATIVE_BPM,
        "energy_experimental": MetadataFieldKey.ENERGY,
    }
    _METRIC_KEYS = frozenset(
        {
            "rms_mean",
            "rms_variability",
            "peak",
            "crest_factor",
            "transient_density",
            "bpm",
            "energy_experimental",
        }
    )

    def __init__(self, database: Database) -> None:
        self._database = database

    def persist_valid_result(self, result: MetadataAnalysisResult) -> None:
        if result.outcome not in {
            MetadataAnalysisOutcome.SUCCESS,
            MetadataAnalysisOutcome.PARTIAL_SUCCESS,
        }:
            raise ValueError("Nur gültige Erfolgsresultate dürfen persistiert werden")
        if (
            result.analysis_version != ALGORITHM_VERSION
            or result.backend_version != ALGORITHM_VERSION
        ):
            raise ValueError("Analyseversion stimmt nicht mit dem Produktivbackend überein")
        with self._database.transaction() as connection:
            run = connection.execute(
                """SELECT track_id,analysis_profile,analysis_version,status,
                          file_path_snapshot,file_size,file_modified_ns,fingerprint
                   FROM metadata_analysis_runs WHERE id=?""",
                (result.run_id,),
            ).fetchone()
            if run is None or str(run["status"]) != "RUNNING":
                raise ValueError("Analyselauf ist nicht aktiv")
            snapshot = result.input_snapshot
            if (
                int(run["track_id"]) != result.track_id
                or str(run["analysis_profile"]) != result.analysis_profile
                or str(run["analysis_version"]) != result.analysis_version
                or str(run["file_path_snapshot"]) != snapshot.normalized_path
                or int(run["file_size"]) != snapshot.size
                or int(run["file_modified_ns"]) != snapshot.modified_ns
                or (str(run["fingerprint"]) if run["fingerprint"] is not None else None)
                != snapshot.fingerprint
            ):
                raise ValueError("Ergebnis gehört nicht zum aktiven Dateisnapshot")
            self._persist_ranges(connection, result)
            self._persist_metrics(connection, result)
            for suggestion in result.suggestions:
                self._persist_suggestion(connection, result, suggestion)
            connection.execute(
                """UPDATE metadata_analysis_runs
                   SET status='COMPLETED', finished_at=CURRENT_TIMESTAMP,
                       error_code=NULL,error_text=NULL WHERE id=? AND status='RUNNING'""",
                (result.run_id,),
            )

    def _persist_ranges(
        self, connection: sqlite3.Connection, result: MetadataAnalysisResult
    ) -> None:
        if len(result.analyzed_ranges) > 8:
            raise ValueError("Zu viele Analysebereiche")
        for index, region in enumerate(result.analyzed_ranges):
            connection.execute(
                """INSERT INTO metadata_analysis_run_ranges
                       (run_id,range_index,start_seconds,duration_seconds)
                   VALUES (?,?,?,?)""",
                (result.run_id, index, region.start_seconds, region.duration_seconds),
            )

    def _persist_metrics(
        self, connection: sqlite3.Connection, result: MetadataAnalysisResult
    ) -> None:
        metrics = {item.name: (item.value, item.unit) for item in result.technical_metrics}
        if set(metrics) - self._METRIC_KEYS:
            raise ValueError("Unbekannter technischer Messwert")
        metrics["rhythm_stability"] = (result.rhythm_stability, "ratio")
        for suggestion in result.suggestions:
            if suggestion.field_key == "bpm":
                metrics["bpm"] = (self._number(suggestion), "BPM")
            elif suggestion.field_key == "energy_experimental":
                metrics["energy_experimental"] = (
                    self._number(suggestion),
                    "percent",
                )
        for key, (value, unit) in metrics.items():
            connection.execute(
                """INSERT INTO metadata_analysis_run_metrics
                       (run_id,metric_key,metric_value,unit,algorithm_version,experimental)
                   VALUES (?,?,?,?,?,?)""",
                (
                    result.run_id,
                    key,
                    value,
                    unit,
                    result.analysis_version,
                    int(key == "energy_experimental"),
                ),
            )

    def _persist_suggestion(
        self,
        connection: sqlite3.Connection,
        result: MetadataAnalysisResult,
        suggestion: MetadataFieldSuggestion,
    ) -> None:
        field_name = suggestion.field_key
        field_key = self._FIELD_MAP.get(field_name)
        if field_key is None:
            raise ValueError("Unbekannter Analysevorschlag")
        serialized = serialize_metadata_value(field_key, suggestion.canonical_value)
        identical = connection.execute(
            """SELECT id FROM track_metadata_suggestions
               WHERE track_id=? AND field_key=? AND source_type='AUDIO_ANALYSIS'
                 AND serialized_value=? AND status='PENDING' LIMIT 1""",
            (result.track_id, field_key.value, serialized),
        ).fetchone()
        if identical is not None:
            return
        connection.execute(
            """UPDATE track_metadata_suggestions
               SET status='SUPERSEDED',decided_at=CURRENT_TIMESTAMP,
                   decision_reason='Durch neuere Audioanalyse abgelöst'
               WHERE track_id=? AND field_key=? AND source_type='AUDIO_ANALYSIS'
                 AND status='PENDING'""",
            (result.track_id, field_key.value),
        )
        confidence = suggestion.confidence
        review = (
            MetadataReviewStatus.SUGGESTED
            if confidence >= HIGH_CONFIDENCE and result.rhythm_stability >= TEMPO_CHANGE_STABILITY
            else MetadataReviewStatus.REVIEW_REQUIRED
        )
        detail = (
            "energy_experimental; " if field_name == "energy_experimental" else ""
        ) + result.backend_name
        connection.execute(
            """INSERT INTO track_metadata_suggestions
                   (track_id,analysis_run_id,field_key,serialized_value,source_type,
                    source_detail,confidence,review_status)
               VALUES (?,?,?,?,'AUDIO_ANALYSIS',?,?,?)""",
            (
                result.track_id,
                result.run_id,
                field_key.value,
                serialized,
                detail[:200],
                confidence,
                review.value,
            ),
        )

    @staticmethod
    def _number(suggestion: MetadataFieldSuggestion) -> float:
        value = suggestion.canonical_value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("Numerischer Analysevorschlag ist ungültig")
        return float(value)
