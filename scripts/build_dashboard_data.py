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
VERSION_HISTORY_PATH = DATA_DIR / "version-history.json"
VERSION_HISTORY_RETENTION = 730
SENSORHUB_REPO_NAMES = {
    "glaecier-sensorhub-data-collector",
    "sensorhub-data-collector",
}


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_json_if_present(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}

    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError:
        return {}

    if not content:
        return {}

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return {}

    return data if isinstance(data, dict) else {}


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


def humanize_slug(value: str) -> str:
    words = [part for part in safe_string(value).replace("_", "-").split("-") if part]
    return " ".join(word.capitalize() for word in words)


def normalize_mac(raw_mac: Any) -> str:
    text = "".join(character for character in safe_string(raw_mac) if character.isalnum())
    if len(text) == 12:
        return ":".join(text[index:index + 2] for index in range(0, 12, 2)).lower()
    return safe_string(raw_mac).lower()


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


def get_hardware_capabilities(device_registry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    capabilities = device_registry.get("hardware_capabilities")
    if not isinstance(capabilities, dict):
        return {}
    return {
        safe_string(name): value
        for name, value in capabilities.items()
        if isinstance(value, dict) and safe_string(name)
    }


def resolve_ota_capable(
    entry: dict[str, Any],
    hardware: str,
    capabilities: dict[str, dict[str, Any]],
) -> bool | None:
    override = entry.get("ota_override")
    if isinstance(override, bool):
        return override
    capability = capabilities.get(hardware)
    if isinstance(capability, dict) and isinstance(capability.get("ota"), bool):
        return capability["ota"]
    return None


# Candidate field names for a last-contact timestamp on GET /devices/{mac}.
# As of 2026-06 the API returns none of these; kept so the field starts
# flowing automatically if the API adds one.
LAST_SEEN_FIELDS = (
    "lastSeen",
    "last_seen",
    "lastContact",
    "lastReportTime",
    "lastReportedAt",
    "lastLogAt",
    "updatedAt",
)


def extract_last_seen(device_data: dict[str, Any]) -> str | None:
    for field in LAST_SEEN_FIELDS:
        value = safe_string(device_data.get(field))
        if value and parse_timestamp(value) != datetime.min.replace(tzinfo=timezone.utc):
            return parse_timestamp(value).isoformat()
    return None


def extract_components_from_config(config_data: dict[str, Any]) -> list[str]:
    sensors = config_data.get("sensors", []) or []
    if not isinstance(sensors, list):
        sensors = []

    sensor_types = sorted(
        {
            safe_string(sensor.get("type"))
            for sensor in sensors
            if isinstance(sensor, dict) and safe_string(sensor.get("type"))
        }
    )
    has_virtual = any(
        isinstance(sensor, dict) and sensor.get("virtual") not in (None, False, 0, "0")
        for sensor in sensors
    )
    components = sensor_types + (["virtual"] if has_virtual else [])
    if "core" not in components:
        components.append("core")
    return components


def choose_repo_device_version(version_data: dict[str, Any]) -> str:
    deployment_version = normalize_version(
        safe_string(version_data.get("deployment_version")) or safe_string(version_data.get("deploymentVersion"))
    )
    if deployment_version and deployment_version not in {"v0.0.0", "vnot-deployed"}:
        return deployment_version

    has_deployment_record = any(
        safe_string(version_data.get(field))
        for field in ("deployment_date", "initial_deployment_date", "mac_address")
    )
    template_version = normalize_version(
        safe_string(version_data.get("template_version")) or safe_string(version_data.get("templateVersion"))
    )
    if has_deployment_record and template_version:
        return template_version

    return ""


def resolve_local_repo_path(repo_names: list[str]) -> Path | None:
    candidates: list[Path] = []
    configured_path = safe_string(os.getenv("SENSORHUB_DATA_COLLECTOR_PATH"))
    if configured_path:
        candidates.append(Path(configured_path).expanduser())

    normalized_repo_names = {safe_string(name) for name in repo_names if safe_string(name)}
    if normalized_repo_names & SENSORHUB_REPO_NAMES:
        for base_path in (ROOT, ROOT.parent):
            candidates.append(base_path / "sensorhub-data-collector")
            candidates.append(base_path / "glaecier-sensorhub-data-collector")

    for repo_name in normalized_repo_names:
        stripped_repo_name = repo_name.removeprefix("glaecier-")
        for base_path in (ROOT, ROOT.parent):
            candidates.append(base_path / repo_name)
            if stripped_repo_name and stripped_repo_name != repo_name:
                candidates.append(base_path / stripped_repo_name)

    seen: set[str] = set()
    for candidate in candidates:
        resolved = candidate.resolve(strict=False)
        resolved_key = str(resolved).lower()
        if resolved_key in seen:
            continue
        seen.add(resolved_key)
        if resolved.is_dir():
            return resolved

    return None


def build_repo_device_summaries(
    repo_path: Path,
    track: dict[str, Any],
    capabilities: dict[str, dict[str, Any]],
    registry_entry_map: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    devices_dir = repo_path / "devices"
    if not devices_dir.is_dir():
        return []

    output: list[dict[str, Any]] = []
    for device_dir in sorted(path for path in devices_dir.iterdir() if path.is_dir()):
        config_data = load_json_if_present(device_dir / "config.json")
        version_data = load_json_if_present(device_dir / "version.json")
        device_meta = config_data.get("device")
        if not isinstance(device_meta, dict):
            device_meta = {}

        mac = normalize_mac(version_data.get("mac_address"))
        registry_entry = registry_entry_map.get(mac, {})
        hardware = (
            safe_string(version_data.get("hardware_target"))
            or safe_string(version_data.get("hardware"))
            or extract_hardware(registry_entry)
        )

        output.append(
            {
                "name": safe_string(device_meta.get("name")) or humanize_slug(device_dir.name),
                "mac": mac,
                "version": choose_repo_device_version(version_data),
                "components": extract_components_from_config(config_data),
                "hardware": hardware,
                "ota_capable": resolve_ota_capable(registry_entry, hardware, capabilities),
                "hardware_note": safe_string(registry_entry.get("hardware_note")),
                "last_seen": None,
                "location": safe_string(version_data.get("deployment_location")),
                "last_deployed": safe_string(version_data.get("deployment_date")),
                "initial_deployed": safe_string(version_data.get("initial_deployment_date")),
                "deployment_environment": safe_string(version_data.get("deployment_environment")),
                "declared_deployment_version": (
                    safe_string(version_data.get("deployment_version")) or safe_string(version_data.get("deploymentVersion"))
                ),
                "sensor_summary": safe_string(version_data.get("sensor_summary")),
                "notes": safe_string(version_data.get("notes")),
                "updated_by": safe_string(version_data.get("updated_by")),
                "version_last_updated": safe_string(version_data.get("last_updated")),
                "track": track.get("id", ""),
                "track_label": track.get("label", ""),
                "target_version": track.get("latest_version", ""),
            }
        )

    return output


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
    capabilities: dict[str, dict[str, Any]],
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
    hardware = extract_hardware(entry)

    return {
        "name": label or safe_string(device_data.get("deviceName")) or mac,
        "mac": safe_string(device_data.get("macAddress")) or mac,
        "version": firmware_version,
        "components": extract_components(device_data, entry),
        "hardware": hardware,
        "ota_capable": resolve_ota_capable(entry, hardware, capabilities),
        "hardware_note": safe_string(entry.get("hardware_note")),
        "last_seen": extract_last_seen(device_data),
        "location": extract_location(device_data),
        "last_deployed": firmware_build_date.split("T")[0] if firmware_build_date else "",
        "initial_deployed": safe_string(entry.get("initial_deployed")),
        "deployment_environment": safe_string(entry.get("deployment_environment")),
        "declared_deployment_version": safe_string(entry.get("declared_deployment_version")),
        "sensor_summary": safe_string(entry.get("sensor_summary")),
        "notes": safe_string(entry.get("notes")),
        "updated_by": safe_string(entry.get("updated_by")),
        "version_last_updated": safe_string(entry.get("version_last_updated")),
        "track": track_id,
        "track_label": track.get("label", track_id),
        "target_version": track.get("latest_version", ""),
    }


def build_failure_device(
    entry: dict[str, Any],
    track_map: dict[str, dict[str, Any]],
    capabilities: dict[str, dict[str, Any]],
) -> dict[str, Any]:
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
    hardware = extract_hardware(entry)

    return {
        "name": label or mac,
        "mac": mac,
        "version": "",
        "components": components,
        "hardware": hardware,
        "ota_capable": resolve_ota_capable(entry, hardware, capabilities),
        "hardware_note": safe_string(entry.get("hardware_note")),
        "last_seen": None,
        "location": "",
        "last_deployed": "",
        "initial_deployed": safe_string(entry.get("initial_deployed")),
        "deployment_environment": safe_string(entry.get("deployment_environment")),
        "declared_deployment_version": safe_string(entry.get("declared_deployment_version")),
        "sensor_summary": safe_string(entry.get("sensor_summary")),
        "notes": safe_string(entry.get("notes")),
        "updated_by": safe_string(entry.get("updated_by")),
        "version_last_updated": safe_string(entry.get("version_last_updated")),
        "track": track_id,
        "track_label": track.get("label", track_id),
        "target_version": track.get("latest_version", ""),
    }


def count_versions_by_section(sections: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    section_counts: dict[str, dict[str, int]] = {}
    for section in sections:
        section_id = safe_string(section.get("id"))
        if not section_id:
            continue

        version_counts: dict[str, int] = {}
        for device in section.get("devices", []) or []:
            if not isinstance(device, dict):
                continue
            version = normalize_version(device.get("version"))
            version_counts[version] = version_counts.get(version, 0) + 1
        section_counts[section_id] = dict(sorted(version_counts.items()))

    return section_counts


def normalize_version_history(data: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    snapshots = data.get("snapshots")
    if not isinstance(snapshots, list):
        return {"snapshots": []}

    normalized_snapshots: list[dict[str, Any]] = []
    for snapshot in snapshots:
        if not isinstance(snapshot, dict):
            continue

        date = safe_string(snapshot.get("date"))
        sections = snapshot.get("sections")
        if not date or not isinstance(sections, dict):
            continue

        normalized_sections: dict[str, dict[str, int]] = {}
        for section_id, counts in sections.items():
            normalized_section_id = safe_string(section_id)
            if not normalized_section_id or not isinstance(counts, dict):
                continue

            normalized_counts: dict[str, int] = {}
            for version, count in counts.items():
                try:
                    numeric_count = int(count)
                except (TypeError, ValueError):
                    continue
                if numeric_count < 0:
                    continue
                normalized_counts[normalize_version(safe_string(version))] = numeric_count

            normalized_sections[normalized_section_id] = dict(sorted(normalized_counts.items()))

        normalized_snapshots.append(
            {
                "date": date,
                "sections": dict(sorted(normalized_sections.items())),
            }
        )

    normalized_snapshots.sort(key=lambda item: item["date"])
    return {"snapshots": normalized_snapshots[-VERSION_HISTORY_RETENTION:]}


def upsert_version_history_snapshot(sections: list[dict[str, Any]], snapshot_date: str) -> None:
    history = normalize_version_history(load_json_if_present(VERSION_HISTORY_PATH))
    next_snapshot = {
        "date": snapshot_date,
        "sections": count_versions_by_section(sections),
    }
    snapshots_by_date = {
        safe_string(snapshot.get("date")): snapshot
        for snapshot in history["snapshots"]
        if isinstance(snapshot, dict) and safe_string(snapshot.get("date"))
    }
    snapshots_by_date[snapshot_date] = next_snapshot
    snapshots = [snapshots_by_date[date] for date in sorted(snapshots_by_date)]
    history = {"snapshots": snapshots[-VERSION_HISTORY_RETENTION:]}
    VERSION_HISTORY_PATH.write_text(json.dumps(history, indent=2), encoding="utf-8")
    print(f"Wrote {VERSION_HISTORY_PATH}")


def main() -> int:
    if not DEVICES_PATH.exists():
        print(f"Missing required file: {DEVICES_PATH}", file=sys.stderr)
        return 1

    device_registry = load_json(DEVICES_PATH)
    version_data = load_json(VERSION_CHANGES_PATH) if VERSION_CHANGES_PATH.exists() else {}
    default_section, registry_sections = get_registry_sections(device_registry)
    version_section_map = get_version_section_map(version_data)
    hardware_capabilities = get_hardware_capabilities(device_registry)
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
        registry_entry_map = {
            normalize_mac(entry.get("mac")): entry
            for entry in registry_section.get("devices", []) or []
            if isinstance(entry, dict) and safe_string(entry.get("mac"))
        }
        repo_devices: list[dict[str, Any]] = []
        local_repo_paths: list[str] = []
        for track in tracks:
            repo_name = safe_string(track.get("repo_name"))
            if not repo_name:
                continue
            local_repo_path = resolve_local_repo_path([repo_name])
            if not local_repo_path:
                continue
            track_devices = build_repo_device_summaries(local_repo_path, track, hardware_capabilities, registry_entry_map)
            if not track_devices:
                continue
            repo_devices.extend(track_devices)
            local_repo_paths.append(str(local_repo_path))

        if repo_devices:
            resolved_paths = ", ".join(dict.fromkeys(local_repo_paths))
            print(f"Loaded {len(repo_devices)} devices for section '{section_id}' from {resolved_paths}")
            section_output["devices"].extend(repo_devices)
            output_sections.append(section_output)
            continue

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
                section_output["devices"].append(fetch_device_summary(api_base, entry, track_map, headers, hardware_capabilities))
            except HTTPError as error:
                failures.append(f"{section_id}:{mac}: HTTP {error.code}")
                section_output["devices"].append(build_failure_device(entry, track_map, hardware_capabilities))
            except URLError as error:
                failures.append(f"{section_id}:{mac}: {error.reason}")
                section_output["devices"].append(build_failure_device(entry, track_map, hardware_capabilities))

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
    upsert_version_history_snapshot(output_sections, datetime.now(timezone.utc).date().isoformat())

    if failures:
        print("Completed with device fetch failures:", file=sys.stderr)
        for failure in failures:
            print(f" - {failure}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
