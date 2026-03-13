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
        "User-Agent": "3a-console-sensor-device-status-builder/2.0",
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


def safe_string(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def parse_timestamp(value: str) -> datetime:
    text = safe_string(value)
    if not text:
        return datetime.min.replace(tzinfo=timezone.utc)

    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d")
            return parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def extract_components(device_data: dict[str, Any], entry: dict[str, Any]) -> list[str]:
    explicit_components = entry.get("components")
    if isinstance(explicit_components, list):
        components = sorted({safe_string(component) for component in explicit_components if safe_string(component)})
        if "core" not in components:
            components.append("core")
        return components

    sensors = device_data.get("settings", {}).get("sensors", []) or []
    sensor_types = sorted(
        {
            safe_string(sensor.get("type"))
            for sensor in sensors
            if isinstance(sensor, dict) and safe_string(sensor.get("type"))
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
        safe_string(sensor_location.get("description"))
        or safe_string((device_data.get("location") or {}).get("name"))
        or ""
    )


def extract_hardware(entry: dict[str, Any]) -> str:
    return safe_string(entry.get("hardware"))


def get_registry_sections(device_registry: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    default_section = safe_string(device_registry.get("default_section")) or "sensor-hub"

    if isinstance(device_registry.get("sections"), list):
        return default_section, device_registry["sections"]

    legacy_api_base = safe_string(device_registry.get("api_base"))
    legacy_devices = device_registry.get("devices", []) or []
    return (
        default_section,
        [
            {
                "id": "sensor-hub",
                "label": "Sensor Hub",
                "description": "ESP32 sensor-hub devices based on glaecier-sensorhub-data-collector.",
                "api_base": legacy_api_base,
                "tracks": [
                    {
                        "id": "sensor-hub",
                        "label": "Sensor Hub",
                        "repo_name": "glaecier-sensorhub-data-collector",
                    }
                ],
                "devices": [
                    {
                        **entry,
                        "track": safe_string(entry.get("track")) or "sensor-hub",
                    }
                    for entry in legacy_devices
                    if isinstance(entry, dict)
                ],
            }
        ],
    )


def get_version_section_map(version_data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    sections = version_data.get("sections")
    if isinstance(sections, list):
        return {
            safe_string(section.get("id")): section
            for section in sections
            if isinstance(section, dict) and safe_string(section.get("id"))
        }

    return {
        "sensor-hub": {
            "id": "sensor-hub",
            "label": "Sensor Hub",
            "repo_name": safe_string(version_data.get("repo_name")) or "glaecier-sensorhub-data-collector",
            "latest_version": normalize_version(version_data.get("latest_version")),
            "last_commit_date": safe_string(version_data.get("last_commit_date")),
            "version_changes": version_data.get("version_changes", []) or [],
            "tracks": [
                {
                    "id": "sensor-hub",
                    "label": "Sensor Hub",
                    "repo_name": safe_string(version_data.get("repo_name")) or "glaecier-sensorhub-data-collector",
                    "latest_version": normalize_version(version_data.get("latest_version")),
                    "last_commit_date": safe_string(version_data.get("last_commit_date")),
                    "version_changes": version_data.get("version_changes", []) or [],
                }
            ],
        }
    }


def normalize_track(track: dict[str, Any], fallback_id: str, fallback_label: str) -> dict[str, Any]:
    return {
        "id": safe_string(track.get("id")) or fallback_id,
        "label": safe_string(track.get("label")) or fallback_label,
        "repo_name": safe_string(track.get("repo_name")),
        "latest_version": normalize_version(track.get("latest_version")),
        "last_commit_date": safe_string(track.get("last_commit_date")),
        "version_changes": track.get("version_changes", []) or [],
    }


def build_section_tracks(registry_section: dict[str, Any], version_section: dict[str, Any]) -> list[dict[str, Any]]:
    version_tracks = version_section.get("tracks")
    if isinstance(version_tracks, list) and version_tracks:
        track_map = {
            safe_string(track.get("id")): normalize_track(track, safe_string(track.get("id")), safe_string(track.get("label")))
            for track in version_tracks
            if isinstance(track, dict) and safe_string(track.get("id"))
        }
    else:
        base_track_id = safe_string(registry_section.get("id")) or "default-track"
        track_map = {
            base_track_id: normalize_track(
                {
                    "id": base_track_id,
                    "label": safe_string(version_section.get("label")) or safe_string(registry_section.get("label")) or "Default track",
                    "repo_name": safe_string(version_section.get("repo_name")),
                    "latest_version": version_section.get("latest_version"),
                    "last_commit_date": version_section.get("last_commit_date"),
                    "version_changes": version_section.get("version_changes", []) or [],
                },
                base_track_id,
                safe_string(registry_section.get("label")) or "Default track",
            )
        }

    registry_tracks = registry_section.get("tracks")
    if isinstance(registry_tracks, list):
        for registry_track in registry_tracks:
            if not isinstance(registry_track, dict):
                continue
            track_id = safe_string(registry_track.get("id"))
            if not track_id:
                continue
            existing = track_map.get(track_id, {})
            track_map[track_id] = {
                **existing,
                "id": track_id,
                "label": safe_string(registry_track.get("label")) or existing.get("label") or track_id,
                "repo_name": safe_string(registry_track.get("repo_name")) or existing.get("repo_name", ""),
                "latest_version": existing.get("latest_version", ""),
                "last_commit_date": existing.get("last_commit_date", ""),
                "version_changes": existing.get("version_changes", []),
            }

    return list(track_map.values())


def combine_section_changes(tracks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    combined: list[dict[str, Any]] = []
    for track in tracks:
        for change in track.get("version_changes", []) or []:
            if not isinstance(change, dict):
                continue
            combined.append(
                {
                    **change,
                    "track": track["id"],
                    "track_label": track["label"],
                    "repo_name": track.get("repo_name", ""),
                }
            )
    combined.sort(key=lambda item: parse_timestamp(safe_string(item.get("date"))), reverse=True)
    return combined


def build_section_meta(
    registry_section: dict[str, Any],
    version_section: dict[str, Any],
    tracks: list[dict[str, Any]],
    last_updated: str,
) -> dict[str, Any]:
    latest_versions = [track["latest_version"] for track in tracks if track.get("latest_version")]
    single_track = len(tracks) == 1
    latest_version = latest_versions[0] if single_track and latest_versions else ""
    latest_version_label = latest_version or ("Multiple tracks" if len(tracks) > 1 else "Unknown")
    repo_name = (
        safe_string(version_section.get("repo_name"))
        or safe_string(registry_section.get("repo_name"))
        or (tracks[0].get("repo_name", "") if single_track else "")
        or safe_string(registry_section.get("label"))
        or safe_string(version_section.get("label"))
        or "Unknown"
    )
    last_commit_candidates = [
        safe_string(version_section.get("last_commit_date")),
        *(track.get("last_commit_date", "") for track in tracks),
    ]
    last_commit_date = max(last_commit_candidates, key=parse_timestamp)

    return {
        "id": safe_string(registry_section.get("id")) or safe_string(version_section.get("id")) or "unknown",
        "label": safe_string(registry_section.get("label")) or safe_string(version_section.get("label")) or "Unnamed section",
        "description": safe_string(registry_section.get("description")) or safe_string(version_section.get("description")),
        "repo_name": repo_name,
        "latest_version": latest_version,
        "latest_version_label": latest_version_label,
        "last_commit_date": last_commit_date,
        "last_updated": last_updated,
        "tracks": tracks,
        "version_changes": combine_section_changes(tracks),
        "devices": [],
    }


def fetch_device_summary(
    api_base: str,
    entry: dict[str, Any],
    track_map: dict[str, dict[str, Any]],
    headers: dict[str, str],
) -> dict[str, Any]:
    mac = safe_string(entry.get("mac"))
    label = safe_string(entry.get("label"))
    track_id = safe_string(entry.get("track"))
    track = track_map.get(track_id, {})
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
    firmware_build_date = safe_string((firmware_data or {}).get("buildDate"))

    return {
        "name": label or safe_string(device_data.get("deviceName")) or mac,
        "mac": safe_string(device_data.get("macAddress")) or mac,
        "version": firmware_version,
        "components": extract_components(device_data, entry),
        "hardware": extract_hardware(entry),
        "location": extract_location(device_data),
        "last_deployed": firmware_build_date.split("T")[0] if firmware_build_date else "",
        "initial_deployed": safe_string(entry.get("initial_deployed")),
        "track": track_id,
        "track_label": track.get("label", track_id),
        "target_version": track.get("latest_version", ""),
    }


def build_failure_device(entry: dict[str, Any], track_map: dict[str, dict[str, Any]]) -> dict[str, Any]:
    mac = safe_string(entry.get("mac"))
    label = safe_string(entry.get("label"))
    track_id = safe_string(entry.get("track"))
    track = track_map.get(track_id, {})
    explicit_components = entry.get("components")
    components = (
        sorted({safe_string(component) for component in explicit_components if safe_string(component)})
        if isinstance(explicit_components, list)
        else []
    )
    if "core" not in components:
        components.append("core")

    return {
        "name": label or mac,
        "mac": mac,
        "version": "",
        "components": components,
        "hardware": extract_hardware(entry),
        "location": "",
        "last_deployed": "",
        "initial_deployed": safe_string(entry.get("initial_deployed")),
        "track": track_id,
        "track_label": track.get("label", track_id),
        "target_version": track.get("latest_version", ""),
    }


def main() -> int:
    if not DEVICES_PATH.exists():
        print(f"Missing required file: {DEVICES_PATH}", file=sys.stderr)
        return 1

    device_registry = load_json(DEVICES_PATH)
    version_data = load_json(VERSION_CHANGES_PATH) if VERSION_CHANGES_PATH.exists() else {}
    default_section, registry_sections = get_registry_sections(device_registry)
    version_section_map = get_version_section_map(version_data)
    headers = build_headers()
    last_updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    output_sections: list[dict[str, Any]] = []
    failures: list[str] = []

    for registry_section in registry_sections:
        if not isinstance(registry_section, dict):
            continue

        section_id = safe_string(registry_section.get("id"))
        if not section_id:
            continue

        version_section = version_section_map.get(section_id, {"id": section_id})
        tracks = build_section_tracks(registry_section, version_section)
        track_map = {track["id"]: track for track in tracks}
        section_output = build_section_meta(registry_section, version_section, tracks, last_updated)

        api_base = safe_string(registry_section.get("api_base")) or safe_string(device_registry.get("api_base"))
        device_entries = registry_section.get("devices", []) or []

        if device_entries and not api_base:
            print(f"Section '{section_id}' is missing api_base", file=sys.stderr)
            return 1

        for entry in device_entries:
            if not isinstance(entry, dict):
                continue

            mac = safe_string(entry.get("mac"))
            if not mac:
                continue

            try:
                section_output["devices"].append(fetch_device_summary(api_base, entry, track_map, headers))
            except HTTPError as error:
                failures.append(f"{section_id}:{mac}: HTTP {error.code}")
                section_output["devices"].append(build_failure_device(entry, track_map))
            except URLError as error:
                failures.append(f"{section_id}:{mac}: {error.reason}")
                section_output["devices"].append(build_failure_device(entry, track_map))

        output_sections.append(section_output)

    if not output_sections:
        print("No dashboard sections were produced", file=sys.stderr)
        return 1

    default_section_data = next(
        (section for section in output_sections if section["id"] == default_section),
        output_sections[0],
    )

    output = {
        "default_section": default_section_data["id"],
        "last_updated": last_updated,
        "sections": output_sections,
        # Backward-compatible fields mirrored from the default section.
        "latest_version": default_section_data.get("latest_version", ""),
        "latest_version_label": default_section_data.get("latest_version_label", ""),
        "repo_name": default_section_data.get("repo_name", "Unknown"),
        "last_commit_date": default_section_data.get("last_commit_date", ""),
        "version_changes": default_section_data.get("version_changes", []),
        "devices": default_section_data.get("devices", []),
    }

    OUTPUT_PATH.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH}")

    if failures:
        print("Completed with device fetch failures:", file=sys.stderr)
        for failure in failures:
            print(f" - {failure}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
