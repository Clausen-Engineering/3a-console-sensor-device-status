#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY_PATH = ROOT / "data" / "devices.json"


def safe_string(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def normalize_mac(raw_mac: Any) -> str:
    text = "".join(character for character in safe_string(raw_mac) if character.isalnum())
    if len(text) == 12:
        return ":".join(text[index:index + 2] for index in range(0, 12, 2)).upper()
    return safe_string(raw_mac).upper()


def normalize_version(raw_version: str) -> str:
    version = safe_string(raw_version)
    if not version:
        return ""
    return f"v{version.removeprefix('v')}"


def load_registry(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return data


def find_device(registry: dict[str, Any], mac: str) -> dict[str, Any] | None:
    normalized_mac = normalize_mac(mac)
    for section in registry.get("sections", []) or []:
        if not isinstance(section, dict):
            continue
        for device in section.get("devices", []) or []:
            if not isinstance(device, dict):
                continue
            if normalize_mac(device.get("mac")) == normalized_mac:
                return device
    return None


def prune_empty_optional_fields(device: dict[str, Any]) -> None:
    for field in ("pending_ota_version", "pending_ota_created"):
        if field in device and not safe_string(device.get(field)):
            del device[field]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update the dashboard device registry by MAC address.")
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY_PATH)
    parser.add_argument("--mac", required=True)
    parser.add_argument("--installed-firmware-version")
    parser.add_argument("--last-firmware-update")
    parser.add_argument("--first-installed-if-empty")
    parser.add_argument("--location")
    parser.add_argument("--clear-pending-ota", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    registry_path = args.registry.resolve()

    try:
        registry = load_registry(registry_path)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"Unable to read registry {registry_path}: {error}", file=sys.stderr)
        return 1

    device = find_device(registry, args.mac)
    if device is None:
        print(f"No dashboard registry device found for MAC {normalize_mac(args.mac)}", file=sys.stderr)
        return 1

    if safe_string(args.installed_firmware_version):
        device["installed_firmware_version"] = normalize_version(args.installed_firmware_version)
    if safe_string(args.last_firmware_update):
        device["last_firmware_update"] = safe_string(args.last_firmware_update)
    if safe_string(args.first_installed_if_empty) and not safe_string(device.get("first_installed")):
        device["first_installed"] = safe_string(args.first_installed_if_empty)
    if safe_string(args.location):
        device["location"] = safe_string(args.location)

    if args.clear_pending_ota:
        device.pop("pending_ota_version", None)
        device.pop("pending_ota_created", None)

    prune_empty_optional_fields(device)

    try:
        registry_path.write_text(json.dumps(registry, indent=2) + "\n", encoding="utf-8")
    except OSError as error:
        print(f"Unable to write registry {registry_path}: {error}", file=sys.stderr)
        return 1

    print(f"Updated dashboard registry for {safe_string(device.get('label')) or normalize_mac(args.mac)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
