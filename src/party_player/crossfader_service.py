"""Master volume and equal-power crossfader calculations."""

import math
from threading import RLock

from party_player.deck_controller import DeckController


class CrossfaderService:
    def __init__(
        self,
        deck_a: DeckController,
        deck_b: DeckController,
        position: float = 0.5,
        master_volume: float = 0.8,
    ) -> None:
        self.deck_a = deck_a
        self.deck_b = deck_b
        self.position = self._clamp(position)
        self.master_volume = self._clamp(master_volume)
        self.ducking_factor = 1.0
        self.master_muted = False
        self.panic_muted = False
        self._lock = RLock()
        self.apply()

    @staticmethod
    def _clamp(value: float) -> float:
        return max(0.0, min(value, 1.0))

    def factors(self) -> tuple[float, float]:
        with self._lock:
            return (
                math.cos(self.position * math.pi / 2),
                math.sin(self.position * math.pi / 2),
            )

    def effective_volumes(self) -> tuple[float, float]:
        with self._lock:
            factor_a, factor_b = self.factors()
            output_gate = 0.0 if self.output_muted else 1.0
            return (
                output_gate
                * (0.0 if self.deck_a.transition_muted or self.deck_a.emergency_muted else 1.0)
                * self.deck_a.normalization_factor
                * self.deck_a.fade_level
                * self.deck_a.model.volume
                * factor_a
                * self.ducking_factor
                * self.master_volume,
                output_gate
                * (0.0 if self.deck_b.transition_muted or self.deck_b.emergency_muted else 1.0)
                * self.deck_b.normalization_factor
                * self.deck_b.fade_level
                * self.deck_b.model.volume
                * factor_b
                * self.ducking_factor
                * self.master_volume,
            )

    def apply(self) -> None:
        with self._lock:
            volume_a, volume_b = self.effective_volumes()
            self.deck_a.apply_effective_volume(volume_a)
            self.deck_b.apply_effective_volume(volume_b)
            self.deck_a.update_on_air(volume_a)
            self.deck_b.update_on_air(volume_b)

    def set_ducking_factor(self, factor: float) -> None:
        """Apply transient overlay ducking without changing visible mixer values."""

        with self._lock:
            normalized = self._clamp(factor)
            if math.isclose(normalized, self.ducking_factor, abs_tol=0.0005):
                return
            self.ducking_factor = normalized
            self.apply()

    def set_position(self, position: float) -> None:
        with self._lock:
            self.position = self._clamp(position)
            self.apply()

    def set_master_volume(self, volume: float) -> None:
        with self._lock:
            self.master_volume = self._clamp(volume)
            self.apply()

    @property
    def output_muted(self) -> bool:
        return self.master_muted or self.panic_muted or self.master_volume <= 0

    def mute(self) -> None:
        with self._lock:
            self.master_muted = True
            self.apply()

    def unmute(self) -> None:
        with self._lock:
            self.master_muted = False
            self.apply()

    def set_panic_muted(self, muted: bool) -> None:
        with self._lock:
            normalized = bool(muted)
            if normalized == self.panic_muted:
                return
            self.panic_muted = normalized
            if normalized:
                self.deck_a.apply_effective_volume(0.0)
                self.deck_b.apply_effective_volume(0.0)
            self.apply()
