"""Storage policy for emergency media that must remain independent from NAS/cloud."""

import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class EmergencyDriveKind(StrEnum):
    FIXED = "FIXED"
    REMOVABLE = "REMOVABLE"
    NETWORK = "NETWORK"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class EmergencyStorageDecision:
    allowed: bool
    code: str = ""
    reason: str = ""


DriveClassifier = Callable[[Path], EmergencyDriveKind]


class EmergencyStoragePolicy:
    """Require an approved local root and reject unsafe storage classes."""

    def __init__(
        self,
        approved_local_ssd_roots: Iterable[str | Path],
        *,
        approved_removable_roots: Iterable[str | Path] = (),
        cloud_roots: Iterable[str | Path] = (),
        drive_classifier: DriveClassifier | None = None,
    ) -> None:
        self._local_roots = self._normalize_roots(approved_local_ssd_roots)
        self._removable_roots = self._normalize_roots(approved_removable_roots)
        detected_cloud = [
            value
            for name in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer")
            if (value := os.environ.get(name))
        ]
        self._cloud_roots = self._normalize_roots((*cloud_roots, *detected_cloud))
        self._drive_classifier = drive_classifier or self._classify_drive

    def evaluate(self, path: str | Path) -> EmergencyStorageDecision:
        candidate = self._normalize(Path(path))
        if self._is_unc(candidate):
            return self._reject("NETWORK_STORAGE", "NAS- und Netzwerkpfade sind nicht erlaubt")
        if self._under_any(candidate, self._cloud_roots):
            return self._reject("CLOUD_STORAGE", "Cloud-Speicher ist nicht erlaubt")
        kind = self._drive_classifier(candidate)
        if kind == EmergencyDriveKind.NETWORK:
            return self._reject("NETWORK_STORAGE", "Netzlaufwerke sind nicht erlaubt")
        if kind == EmergencyDriveKind.REMOVABLE:
            if self._under_any(candidate, self._removable_roots):
                return EmergencyStorageDecision(True)
            return self._reject(
                "REMOVABLE_STORAGE_NOT_APPROVED",
                "Wechselmedien müssen ausdrücklich freigegeben werden",
            )
        if kind != EmergencyDriveKind.FIXED:
            return self._reject(
                "STORAGE_TYPE_UNVERIFIED", "Der lokale Speichertyp konnte nicht bestätigt werden"
            )
        if not self._local_roots:
            return self._reject(
                "NO_APPROVED_LOCAL_SSD_ROOT",
                "Es ist kein lokaler SSD-Ordner für Notfallmedien freigegeben",
            )
        if not self._under_any(candidate, self._local_roots):
            return self._reject(
                "OUTSIDE_APPROVED_LOCAL_SSD_ROOT",
                "Die Datei liegt außerhalb der freigegebenen lokalen SSD-Ordner",
            )
        return EmergencyStorageDecision(True)

    @staticmethod
    def _normalize(path: Path) -> Path:
        return Path(os.path.abspath(os.path.expandvars(os.path.expanduser(str(path)))))

    @classmethod
    def _normalize_roots(cls, roots: Iterable[str | Path]) -> tuple[Path, ...]:
        return tuple(cls._normalize(Path(root)) for root in roots if str(root).strip())

    @staticmethod
    def _is_unc(path: Path) -> bool:
        return str(path).startswith(("\\\\", "//"))

    @staticmethod
    def _under_any(path: Path, roots: tuple[Path, ...]) -> bool:
        path_text = os.path.normcase(str(path))
        for root in roots:
            try:
                if os.path.commonpath((path_text, os.path.normcase(str(root)))) == os.path.normcase(
                    str(root)
                ):
                    return True
            except ValueError:
                continue
        return False

    @staticmethod
    def _classify_drive(path: Path) -> EmergencyDriveKind:
        if os.name != "nt":
            return EmergencyDriveKind.FIXED
        import ctypes

        root = path.anchor or str(path)
        drive_type = ctypes.windll.kernel32.GetDriveTypeW(root)
        return {
            2: EmergencyDriveKind.REMOVABLE,
            3: EmergencyDriveKind.FIXED,
            4: EmergencyDriveKind.NETWORK,
        }.get(drive_type, EmergencyDriveKind.UNKNOWN)

    @staticmethod
    def _reject(code: str, reason: str) -> EmergencyStorageDecision:
        return EmergencyStorageDecision(False, code, reason)
