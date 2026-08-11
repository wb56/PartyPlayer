from dataclasses import dataclass

from party_player.windows_audio_devices import WindowsAudioDeviceProvider


@dataclass
class FakeKey:
    path: str

    def __enter__(self) -> "FakeKey":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class FakeRegistry:
    KEY_READ = 1
    HKEY_LOCAL_MACHINE = "HKLM"

    def __init__(self) -> None:
        self.subkeys = {
            "HKLM/root": ("inactive", "speakers", "headset"),
        }
        self.values = {
            "HKLM/root/inactive:DeviceState": 4,
            "HKLM/root/speakers:DeviceState": 1,
            "HKLM/root/headset:DeviceState": 1,
            "HKLM/root/speakers/Properties:{a45c254e-df1c-4efd-8020-67d146a850e0},2": "Lautsprecher",
            "HKLM/root/headset/Properties:{b3f8fa53-0004-438e-9003-51a46e139bfc},6": "USB Headset",
        }

    def OpenKey(self, parent, name: str, *_args: object) -> FakeKey:
        base = parent.path if isinstance(parent, FakeKey) else str(parent)
        path = "HKLM/root" if base == "HKLM" else f"{base}/{name}"
        return FakeKey(path)

    def EnumKey(self, key: FakeKey, index: int) -> str:
        try:
            return self.subkeys[key.path][index]
        except (KeyError, IndexError) as exc:
            raise OSError from exc

    def QueryValueEx(self, key: FakeKey, name: str) -> tuple[object, int]:
        try:
            return self.values[f"{key.path}:{name}"], 1
        except KeyError as exc:
            raise OSError from exc


def test_provider_returns_only_active_render_endpoints(monkeypatch) -> None:
    monkeypatch.setattr("party_player.windows_audio_devices.sys.platform", "win32")

    result = WindowsAudioDeviceProvider(FakeRegistry())()

    assert result.devices == (
        ("speakers", "Lautsprecher"),
        ("headset", "USB Headset"),
    )
    assert result.default_device_id is None


def test_provider_is_empty_off_windows(monkeypatch) -> None:
    monkeypatch.setattr("party_player.windows_audio_devices.sys.platform", "linux")

    assert WindowsAudioDeviceProvider(FakeRegistry())().devices == ()
