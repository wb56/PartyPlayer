"""Read-only Windows audio endpoint discovery without creating audio players."""

from typing import Any
import sys

from party_player.system_diagnostic_service import AudioDeviceProbe


_RENDER_ENDPOINTS_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Render"
_FRIENDLY_NAME_PROPERTY = "{a45c254e-df1c-4efd-8020-67d146a850e0},2"
_DEVICE_DESCRIPTION_PROPERTY = "{b3f8fa53-0004-438e-9003-51a46e139bfc},6"
_DEVICE_STATE_ACTIVE = 1


class WindowsAudioDeviceProvider:
    """Enumerate endpoints without guessing a default absent from this registry view."""

    def __init__(self, registry: Any | None = None) -> None:
        self._registry = registry

    def __call__(self) -> AudioDeviceProbe:
        if sys.platform != "win32":
            return AudioDeviceProbe(())
        registry = self._registry
        if registry is None:
            import winreg

            registry = winreg
        devices: list[tuple[str, str]] = []
        access = registry.KEY_READ
        with registry.OpenKey(
            registry.HKEY_LOCAL_MACHINE, _RENDER_ENDPOINTS_KEY, 0, access
        ) as root:
            for endpoint_id in self._subkeys(registry, root):
                try:
                    with registry.OpenKey(root, endpoint_id, 0, access) as endpoint:
                        state = int(registry.QueryValueEx(endpoint, "DeviceState")[0])
                        if state != _DEVICE_STATE_ACTIVE:
                            continue
                        with registry.OpenKey(endpoint, "Properties", 0, access) as properties:
                            label = self._property(
                                registry,
                                properties,
                                _FRIENDLY_NAME_PROPERTY,
                            ) or self._property(
                                registry,
                                properties,
                                _DEVICE_DESCRIPTION_PROPERTY,
                            )
                except OSError:
                    continue
                devices.append((endpoint_id, label or endpoint_id))
        return AudioDeviceProbe(tuple(sorted(devices, key=lambda item: item[1].casefold())))

    @staticmethod
    def _subkeys(registry: Any, key: Any) -> tuple[str, ...]:
        names: list[str] = []
        index = 0
        while True:
            try:
                names.append(str(registry.EnumKey(key, index)))
            except OSError:
                return tuple(names)
            index += 1

    @staticmethod
    def _property(registry: Any, key: Any, name: str) -> str:
        try:
            value = registry.QueryValueEx(key, name)[0]
        except OSError:
            return ""
        return str(value).strip() if value is not None else ""
