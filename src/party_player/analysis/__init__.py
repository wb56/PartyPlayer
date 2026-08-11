"""Offline audio-analysis contracts and implementations."""

from party_player.analysis.base import (
    AnalysisSegment,
    AudioAnalysisBackend,
    AudioFileInfo,
    CancellationToken,
    PcmChunk,
    plan_edge_segments,
)
from party_player.analysis.ffmpeg_backend import (
    AnalysisBackendUnavailableError,
    AudioDecodeError,
    FfmpegAudioAnalysisBackend,
    UnsupportedAudioFormatError,
)
from party_player.analysis.background import (
    AudioAnalysisJob,
    AudioAnalysisRunSummary,
    BackgroundAudioAnalysisRunner,
)
from party_player.analysis.levels import PcmLevelAnalyzer, PcmLevelWindow
from party_player.analysis.signal_detection import (
    SignalDetectionSettings,
    SignalRegion,
    StreamingSignalDetector,
)
from party_player.analysis.cue_estimation import (
    CueBoundaryEstimator,
    CueBoundarySettings,
    DetectedCueBoundaries,
)
from party_player.analysis.result import CueAnalysisResult
from party_player.analysis.service import CueAnalysisService, CueAnalysisServiceJob

__all__ = [
    "AnalysisSegment",
    "AudioAnalysisBackend",
    "AudioAnalysisJob",
    "AudioAnalysisRunSummary",
    "AudioFileInfo",
    "BackgroundAudioAnalysisRunner",
    "CancellationToken",
    "CueBoundaryEstimator",
    "CueBoundarySettings",
    "CueAnalysisResult",
    "CueAnalysisService",
    "CueAnalysisServiceJob",
    "DetectedCueBoundaries",
    "AnalysisBackendUnavailableError",
    "AudioDecodeError",
    "FfmpegAudioAnalysisBackend",
    "PcmChunk",
    "PcmLevelAnalyzer",
    "PcmLevelWindow",
    "SignalDetectionSettings",
    "SignalRegion",
    "StreamingSignalDetector",
    "plan_edge_segments",
    "UnsupportedAudioFormatError",
]
