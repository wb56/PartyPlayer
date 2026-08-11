"""Central deterministic discovery of external runtime dependency candidates."""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
import os
from pathlib import Path
import shutil

from party_player.system_dependencies import (
    FFMPEG_STANDARD_DIRECTORIES,
    VLC_STANDARD_DIRECTORIES,
)


class DependencyCandidateSource(StrEnum):
    USER = "user"
    STANDARD = "standard"
    PATH = "path"
    DETECTED = "detected"


@dataclass(frozen=True, slots=True)
class DependencyCandidate:
    name: str
    installation_directory: Path
    source: DependencyCandidateSource
    priority: int
    expected_files: tuple[str, ...]
    user_selected: bool = False

    def file(self, name: str) -> Path:
        return self.installation_directory / name


WhichFunction = Callable[[str], str | None]


class DependencyLocator:
    """Collect and prioritize candidates without deciding whether they are valid."""

    _SOURCE_PRIORITY = {
        DependencyCandidateSource.USER: 0,
        DependencyCandidateSource.STANDARD: 10,
        DependencyCandidateSource.PATH: 20,
        DependencyCandidateSource.DETECTED: 30,
    }

    def __init__(
        self,
        *,
        environment: Mapping[str, str] | None = None,
        which: WhichFunction = shutil.which,
        vlc_standard_directories: Iterable[str | Path] | None = None,
        ffmpeg_standard_directories: Iterable[str | Path] | None = None,
    ) -> None:
        self._environment = dict(os.environ if environment is None else environment)
        self._which = which
        self._vlc_standard = tuple(
            VLC_STANDARD_DIRECTORIES
            if vlc_standard_directories is None
            else vlc_standard_directories
        )
        self._ffmpeg_standard = tuple(
            FFMPEG_STANDARD_DIRECTORIES
            if ffmpeg_standard_directories is None
            else ffmpeg_standard_directories
        )

    def locate_vlc(
        self,
        user_directory: str | Path | None = None,
        *,
        detected_directories: Iterable[str | Path] = (),
    ) -> tuple[DependencyCandidate, ...]:
        entries: list[tuple[str | Path, DependencyCandidateSource]] = []
        if user_directory is not None and str(user_directory).strip():
            entries.append((user_directory, DependencyCandidateSource.USER))
        entries.extend(
            (directory, DependencyCandidateSource.STANDARD) for directory in self._vlc_standard
        )
        path_candidate = self._which("vlc.exe") or self._which("vlc")
        if path_candidate:
            entries.append((Path(path_candidate).parent, DependencyCandidateSource.PATH))
        entries.extend(
            (directory, DependencyCandidateSource.DETECTED) for directory in detected_directories
        )
        return self._candidates("VLC", entries, ("vlc.exe", "libvlc.dll", "plugins"))

    def locate_ffmpeg(
        self,
        user_bin_directory: str | Path | None = None,
        *,
        detected_directories: Iterable[str | Path] = (),
    ) -> tuple[DependencyCandidate, ...]:
        entries: list[tuple[str | Path, DependencyCandidateSource]] = []
        if user_bin_directory is not None and str(user_bin_directory).strip():
            entries.append((user_bin_directory, DependencyCandidateSource.USER))
        entries.extend(
            (directory, DependencyCandidateSource.STANDARD) for directory in self._ffmpeg_standard
        )
        for executable in ("ffmpeg.exe", "ffprobe.exe", "ffmpeg", "ffprobe"):
            located = self._which(executable)
            if located:
                entries.append((Path(located).parent, DependencyCandidateSource.PATH))
        entries.extend(
            (directory, DependencyCandidateSource.DETECTED) for directory in detected_directories
        )
        return self._candidates("FFmpeg", entries, ("ffmpeg.exe", "ffprobe.exe"))

    def _candidates(
        self,
        name: str,
        entries: Iterable[tuple[str | Path, DependencyCandidateSource]],
        expected_files: tuple[str, ...],
    ) -> tuple[DependencyCandidate, ...]:
        deduplicated: dict[str, DependencyCandidate] = {}
        source_offsets: dict[DependencyCandidateSource, int] = {
            source: 0 for source in DependencyCandidateSource
        }
        for raw_directory, source in entries:
            directory = self._normalize_directory(raw_directory)
            key = os.path.normcase(str(directory))
            priority = self._SOURCE_PRIORITY[source] + source_offsets[source]
            source_offsets[source] += 1
            candidate = DependencyCandidate(
                name,
                directory,
                source,
                priority,
                expected_files,
                source == DependencyCandidateSource.USER,
            )
            current = deduplicated.get(key)
            if current is None or candidate.priority < current.priority:
                deduplicated[key] = candidate
        return tuple(
            sorted(
                deduplicated.values(),
                key=lambda candidate: (
                    candidate.priority,
                    os.path.normcase(str(candidate.installation_directory)),
                ),
            )
        )

    def _normalize_directory(self, value: str | Path) -> Path:
        text = os.path.expandvars(str(value).strip())
        for name, replacement in self._environment.items():
            text = text.replace(f"%{name}%", replacement)
        return Path(os.path.abspath(os.path.expanduser(text)))
