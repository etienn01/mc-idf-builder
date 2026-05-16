"""Parse available PlatformIO build environments from the MeshCore variant ini files."""
import glob
import os
from configparser import RawConfigParser
from dataclasses import dataclass

_SUFFIX_MAP = {
    "_repeater": "repeater",
    "_companion_radio_usb": "companion_usb",
    "_companion_radio_ble": "companion_ble",
    "_companion_radio_wifi": "companion_wifi",
}

_SKIP_IF_CONTAINS = ["_bridge_", "_kiss_modem", "_terminal_chat"]


@dataclass
class BoardEnv:
    env_name: str
    board_variant: str   # env prefix with firmware suffix stripped, e.g. "heltec_v4_tft"
    firmware_type: str   # repeater | companion_usb | companion_ble | companion_wifi
    platform: str = ""


def _firmware_type(env_name: str) -> str | None:
    lower = env_name.lower()
    if any(s in lower for s in _SKIP_IF_CONTAINS):
        return None
    for suffix, ftype in _SUFFIX_MAP.items():
        if lower.endswith(suffix):
            return ftype
    return None


def _board_variant(env_name: str) -> str:
    lower = env_name.lower()
    for suffix in _SUFFIX_MAP:
        if lower.endswith(suffix):
            return env_name[: -len(suffix)]
    return env_name


def load_environments(meshcore_path: str = None) -> list[BoardEnv]:
    path = meshcore_path or os.environ.get("MESHCORE_PATH", "/meshcore")
    pattern = os.path.join(path, "variants", "*", "platformio.ini")
    envs: list[BoardEnv] = []

    for ini_path in sorted(glob.glob(pattern)):
        cp = RawConfigParser(strict=False)
        cp.read(ini_path)
        for section in cp.sections():
            if not section.startswith("env:"):
                continue
            env_name = section[4:]
            ftype = _firmware_type(env_name)
            if ftype is None:
                continue
            platform = cp.get(section, "platform", fallback="").strip().lower()
            envs.append(BoardEnv(
                env_name=env_name,
                board_variant=_board_variant(env_name),
                firmware_type=ftype,
                platform=platform,
            ))

    return envs


def group_by_board(envs: list[BoardEnv]) -> dict[str, list[BoardEnv]]:
    result: dict[str, list[BoardEnv]] = {}
    for env in envs:
        result.setdefault(env.board_variant, []).append(env)
    return result
