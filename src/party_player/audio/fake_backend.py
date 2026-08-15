"""Deterministic audio backend for tests."""

from pathlib import Path

from party_player.equalizer import ResolvedEqualizerPreset


class FakeAudioBackend:
    def __init__(self, duration: float = 180.0) -> None:
        self.file_path: Path | None = None
        self.position = 0.0
        self.duration = duration
        self.volume = 1.0
        self.playing = False
        self.paused = False
        self.finished = False
        self.volume_write_count = 0
        self.output_device = ""
        self.output_devices: list[tuple[str, str]] = []
        self.runtime_clip_protection_supported = False
        self.runtime_clip_protection: tuple[bool, float] | None = None
        self.equalizer_frequencies = (
            60.0,
            170.0,
            310.0,
            600.0,
            1000.0,
            3000.0,
            6000.0,
            12000.0,
            14000.0,
            16000.0,
        )
        self.equalizer: ResolvedEqualizerPreset | None = None
        self.equalizer_apply_count = 0
        self.equalizer_skip_count = 0

    def maximum_volume_factor(self) -> float:
        return 4.0

    def supports_runtime_clip_protection(self) -> bool:
        return self.runtime_clip_protection_supported

    def set_runtime_clip_protection(self, enabled: bool, ceiling_dbfs: float) -> bool:
        if not self.runtime_clip_protection_supported:
            return False
        self.runtime_clip_protection = (enabled, ceiling_dbfs)
        return True

    def list_output_devices(self) -> list[tuple[str, str]]:
        return list(self.output_devices)

    def set_output_device(self, device_id: str) -> None:
        self.output_device = device_id

    def equalizer_band_frequencies(self) -> tuple[float, ...]:
        return self.equalizer_frequencies

    def apply_equalizer(self, preset: ResolvedEqualizerPreset) -> bool:
        if preset == self.equalizer:
            self.equalizer_skip_count += 1
            return False
        self.equalizer = preset
        self.equalizer_apply_count += 1
        return True

    def prepare(self, file_path: Path) -> object:
        return file_path

    def load_prepared(self, file_path: Path, prepared: object) -> None:
        self.load(file_path)

    def release_prepared(self, prepared: object) -> None:
        pass

    def load(self, file_path: Path) -> None:
        self.file_path = file_path
        self.position = 0.0

    def play(self) -> None:
        if self.file_path is None:
            raise RuntimeError("Kein Titel geladen")
        self.playing, self.paused = True, False
        self.finished = False

    def pause(self) -> None:
        if self.playing:
            self.playing, self.paused = False, True

    def resume(self) -> None:
        if self.paused:
            self.playing, self.paused = True, False

    def stop(self) -> None:
        self.playing = self.paused = False
        self.finished = False
        self.position = 0.0

    def seek(self, position_seconds: float) -> None:
        self.position = max(0.0, min(position_seconds, self.duration))

    def set_volume(self, volume: float) -> None:
        self.volume_write_count += 1
        self.volume = max(0.0, min(volume, 4.0))

    def get_position(self) -> float:
        return self.position

    def get_duration(self) -> float:
        return self.duration

    def is_playing(self) -> bool:
        return self.playing

    def is_paused(self) -> bool:
        return self.paused

    def is_finished(self) -> bool:
        return self.finished

    def playback_state(self) -> str:
        if self.finished:
            return "ENDED"
        if self.paused:
            return "PAUSED"
        if self.playing:
            return "PLAYING"
        return "STOPPED"

    def close(self) -> None:
        self.stop()
