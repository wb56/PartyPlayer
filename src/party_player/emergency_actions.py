"""Operator-selected protected emergency action profiles."""

from enum import StrEnum


class EmergencyActionProfile(StrEnum):
    MUTE_ALL = "MUTE_ALL"
    STOP_ALL = "STOP_ALL"
    PLAY_EMERGENCY = "PLAY_EMERGENCY"
    SAFE_RESET = "SAFE_RESET"
