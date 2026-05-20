"""Parse available PlatformIO build environments from the MeshCore variant ini files."""
import glob
import os
import re
from configparser import RawConfigParser
from dataclasses import dataclass

_SUFFIX_MAP = {
    "_repeater": "repeater",
    "_companion_radio_usb": "companion_usb",
    "_companion_radio_ble": "companion_ble",
    "_companion_radio_wifi": "companion_wifi",
}

_SKIP_IF_CONTAINS = ["_bridge_", "_kiss_modem", "_terminal_chat"]

_ROLE_LABELS = {
    "repeater": "Repeater",
    "companion_usb": "Companion (USB)",
    "companion_ble": "Companion (BLE)",
    "companion_wifi": "Companion (WiFi)",
}

_VARIANT_LABELS = {"tft": "TFT"}

# Brand tokens that need non-title-case treatment
_BRAND_TOKENS = {"rak": "RAK", "lilygo": "LilyGo"}


@dataclass
class BoardEnv:
    env_name: str
    board_id: str   # folder name, e.g. "heltec_v4"
    role: str       # repeater | companion_usb | companion_ble | companion_wifi
    label: str      # human-readable env label, e.g. "Repeater (TFT)"
    platform: str = ""


def _firmware_type(env_name: str) -> str | None:
    lower = env_name.lower()
    if any(s in lower for s in _SKIP_IF_CONTAINS):
        return None
    for suffix, ftype in _SUFFIX_MAP.items():
        if lower.endswith(suffix):
            return ftype
    return None


def _variant_from_env(env_name: str, board_id: str) -> str:
    """Return the variant token between board_id and the role suffix, e.g. 'tft'."""
    middle = env_name[len(board_id):]   # e.g. "_tft_repeater"
    for suffix in _SUFFIX_MAP:
        if middle.endswith(suffix):
            inner = middle[1:-len(suffix)]  # strip leading "_" and role suffix
            return inner
    return ""


def _env_label(role: str, variant: str) -> str:
    base = _ROLE_LABELS[role]
    if not variant:
        return base
    var_str = _VARIANT_LABELS.get(
        variant.lower(),
        variant.upper() if len(variant) <= 4 else variant.replace("_", " ").title(),
    )
    return f"{base} ({var_str})"


def _prettify_token(token: str) -> str:
    if token in _BRAND_TOKENS:
        return _BRAND_TOKENS[token]
    for brand, display in _BRAND_TOKENS.items():
        if token.startswith(brand):
            return display + token[len(brand):].upper()
    if re.match(r"^[a-z]{0,4}\d", token):  # v3, v4, t096, gat562, 4631, t3s3
        return token.upper()
    return token.title()


def prettify_board_name(board_id: str) -> str:
    """Convert a folder name like 'heltec_v4' to a display name like 'Heltec V4'."""
    return " ".join(_prettify_token(p) for p in board_id.split("_"))


def _normalize_platform(raw: str) -> str:
    """Extract a short platform id from a raw platformio platform value.

    Handles:
      nordicnrf52                                 -> nordicnrf52
      platformio/espressif32@6.11.0               -> espressif32
      https://github.com/.../platform-raspberrypi.git -> raspberrypi
    """
    raw = raw.strip().lower().split("@")[0].strip()
    if "/" in raw:
        name = raw.rstrip("/").rsplit("/", 1)[-1]
        name = re.sub(r"\.(zip|git)$", "", name)
        if name.startswith("platform-"):
            name = name[len("platform-"):]
        return name
    return raw


def _resolve_platform(section: str, variant_cp: RawConfigParser,
                      root_cp: RawConfigParser, visited: set | None = None) -> str:
    """Walk extends chains across both the variant and root ini to find platform."""
    if visited is None:
        visited = set()
    if section in visited:
        return ""
    visited.add(section)

    for cp in (variant_cp, root_cp):
        if not cp.has_section(section):
            continue
        if cp.has_option(section, "platform"):
            return _normalize_platform(cp.get(section, "platform"))
        if cp.has_option(section, "extends"):
            parent = cp.get(section, "extends").strip()
            result = _resolve_platform(parent, variant_cp, root_cp, visited)
            if result:
                return result

    return ""


def load_environments(meshcore_path: str | None = None) -> list[BoardEnv]:
    path = meshcore_path or os.environ.get("MESHCORE_PATH", "/meshcore")

    root_cp = RawConfigParser(strict=False)
    root_cp.read(os.path.join(path, "platformio.ini"))

    pattern = os.path.join(path, "variants", "*", "platformio.ini")
    envs: list[BoardEnv] = []

    for ini_path in sorted(glob.glob(pattern)):
        board_id = os.path.basename(os.path.dirname(ini_path))
        cp = RawConfigParser(strict=False)
        cp.read(ini_path)
        for section in cp.sections():
            if not section.startswith("env:"):
                continue
            env_name = section[4:]
            role = _firmware_type(env_name)
            if role is None:
                continue
            platform = _resolve_platform(section, cp, root_cp)
            variant = _variant_from_env(env_name, board_id)
            envs.append(BoardEnv(
                env_name=env_name,
                board_id=board_id,
                role=role,
                label=_env_label(role, variant),
                platform=platform,
            ))

    return envs


def group_by_folder(envs: list[BoardEnv]) -> dict[str, list[BoardEnv]]:
    result: dict[str, list[BoardEnv]] = {}
    for env in envs:
        result.setdefault(env.board_id, []).append(env)
    return result
