#!/usr/bin/env python3
from __future__ import annotations

import base64
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DEVICES_PATH = DATA_DIR / "devices.json"
VERSION_CHANGES_PATH = DATA_DIR / "version-changes.json"
OUTPUT_PATH = DATA_DIR / "dashboard-data.json"


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def build_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "User-Agent": "3a-console-sensor-device-status-builder/1.0",
    }

    username = os.getenv("API_USERNAME", "").strip()
    password = os.getenv("API_PASSWORD", "").strip()
    if username and password:
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {token}"

    return headers


def fetch_json(url: str, headers: dict[str, str]) -> Any:
    request = Request(url, headers=headers)
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def normalize_version(raw_version: str | None) -> str:
    if not raw_version:
        return ""
    version = str(raw_version).strip()
    if not version:
        return ""
    return f"v{version.removeprefix('v')}"


def extract_components(device_data: dict[str, Any]) -> list[str]:
    sensors = device_data.get("settings", {}).get("sensors", []) or []
    sensor_types = sorted(
        {
            str(sensor.get("type")).strip()
            for sensor in sensors
            if sensor.get("type")
        }
    )
    has_virtual = any(sensor.get("virtual") for sensor in sensors if isinstance(sensor, dict))
    components = sensor_types + (["virtual"] if has_virtual else [])
    if "core" not in components:
        components.append("core")
    return components


def extract_location(device_data: dict[str, Any]) -> str:
    settings = device_data.get("settings") or {}
    sensor_location = settings.get("sensorLocation") or {}
    return (
        sensor_location.get("description")
        or (device_data.get("location") or {}).get("name")
        or ""
    )


def fetch_device_summary(api_base: str, mac: str, label: str, headers: dict[str, str]) -> dict[str, Any]:
    encoded_mac = quote(mac, safe="")
    device_url = f"{api_base}/devices/{encoded_mac}"
    firmware_url = f"{api_base}/firmwares/latest?deviceMac={encoded_mac}"

    device_data = fetch_json(device_url, headers)
    firmware_data: dict[str, Any] | None = None

    try:
        firmware_data = fetch_json(firmware_url, headers)
    except HTTPError as error:
        if error.code not in (401, 403, 404):
            raise
    except URLError:
        firmware_data = None

    firmware_version = normalize_version((firmware_data or {}).get("version"))
    firmware_build_date = (firmware_data or {}).get("buildDate") or ""

    return {
        "name": label or device_data.get("deviceName") or mac,
        "mac": device_data.get("macAddress") or mac,
        "version": firmware_version,
        "components": extract_components(device_data),
        "location": extract_location(device_data),
        "last_deployed": firmware_build_date.split("T")[0] if firmware_build_date else "",
        "initial_deployed": "",
    }


def main() -> int:
    if not DEVICES_PATH.exists():
        print(f"Missing required file: {DEVICES_PATH}", file=sys.stderr)
        return 1

    device_registry = load_json(DEVICES_PATH)
    version_data = load_json(VERSION_CHANGES_PATH) if VERSION_CHANGES_PATH.exists() else {}

    api_base = str(device_registry.get("api_base", "")).rstrip("/")
    devices = device_registry.get("devices", [])
    headers = build_headers()

    if not api_base:
        print("devices.json is missing api_base", file=sys.stderr)
        return 1

    output = {
        "latest_version": version_data.get("latest_version", "Unknown"),
        "repo_name": version_data.get("repo_name", "Unknown"),
        "last_commit_date": version_data.get("last_commit_date", ""),
        "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "version_changes": version_data.get("version_changes", []),
        "devices": [],
    }

    failures: list[str] = []

    for entry in devices:
        mac = str(entry.get("mac", "")).strip()
        label = str(entry.get("label", "")).strip()
        if not mac:
            continue

        try:
            output["devices"].append(fetch_device_summary(api_base, mac, label, headers))
        except HTTPError as error:
            failures.append(f"{mac}: HTTP {error.code}")
            output["devices"].append(
                {
                    "name": label or mac,
                    "mac": mac,
                    "version": "",
                    "components": [],
                    "location": "",
                    "last_deployed": "",
                    "initial_deployed": "",
                }
            )
        except URLError as error:
            failures.append(f"{mac}: {error.reason}")
            output["devices"].append(
                {
                    "name": label or mac,
                    "mac": mac,
                    "version": "",
                    "components": [],
                    "location": "",
                    "last_deployed": "",
                    "initial_deployed": "",
                }
            )

    OUTPUT_PATH.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH}")

    if failures:
        print("Completed with device fetch failures:", file=sys.stderr)
        for failure in failures:
            print(f" - {failure}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
