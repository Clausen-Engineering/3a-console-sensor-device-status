#!/usr/bin/env python3
from __future__ import annotations

import base64
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
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


def post_json(url: str, data: dict[str, Any], headers: dict[str, str]) -> Any:
    """POST a JSON body and return the parsed response."""
    body = json.dumps(data).encode("utf-8")
    req_headers = {**headers, "Content-Type": "application/json"}
    request = Request(url, data=body, headers=req_headers, method="POST")
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def post_form(url: str, fields: dict[str, str], headers: dict[str, str]) -> Any:
    """POST an application/x-www-form-urlencoded body and return the parsed response."""
    body = urlencode(fields).encode("utf-8")
    req_headers = {**headers, "Content-Type": "application/x-www-form-urlencoded"}
    request = Request(url, data=body, headers=req_headers, method="POST")
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def build_auth(api_base: str) -> dict[str, str]:
    """Return auth headers, preferring Bearer JWT; falls back to Basic.

    Tries POST {api_base}/auth/token with form fields username/password from
    env API_USERNAME/API_PASSWORD.  On any failure returns the same Basic-auth
    headers that build_headers() would produce (or an empty dict when no
    credentials are configured).
    """
    base_headers: dict[str, str] = {
        "Accept": "application/json",
        "User-Agent": "3a-console-sensor-device-status-builder/2.0",
    }
    username = os.getenv("API_USERNAME", "").strip()
    password = os.getenv("API_PASSWORD", "").strip()
    if not username or not password:
        return base_headers

    # Attempt Bearer token first.
    try:
        response = post_form(
            f"{api_base}/auth/token",
            {"username": username, "password": password},
            base_headers,
        )
        token = safe_string(response.get("token") or response.get("access_token"))
        if token:
            return {**base_headers, "Authorization": f"Bearer {token}"}
    except (HTTPError, URLError, Exception):
        pass

    # Fall back to HTTP Basic.
    encoded = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return {**base_headers, "Authorization": f"Basic {encoded}"}


def fetch_device_directory(api_base: str, headers: dict[str, str]) -> dict[str, dict[str, Any]]:
    """Fetch GET /devices?limit=500 and return a dict keyed by normalized MAC.

    Handles both a bare list and a {"devices": [...]} wrapper shape.
    Returns {} on HTTPError / URLError so callers fall back to per-device fetches.
    """
    try:
        data = fetch_json(f"{api_base}/devices?limit=500", headers)
    except (HTTPError, URLError):
        return {}

    if isinstance(data, list):
        devices = data
    elif isinstance(data, dict):
        devices = data.get("devices") or []
    else:
        devices = []

    return {
        normalize_mac(entry.get("macAddress") or entry.get("mac")): entry
        for entry in devices
        if isinstance(entry, dict)
        and (safe_string(entry.get("macAddress")) or safe_string(entry.get("mac")))
    }


def fetch_device_events(api_base: str, mac: str, headers: dict[str, str]) -> list[dict[str, Any]]:
    """Fetch a device's events; return the events list, or [] on any failure.

    GET /devices/{mac}/events returns a {"events": [...]} wrapper (older/other
    shapes may return a bare list) — both are handled.  This endpoint requires a
    Bearer token; it rejects Basic auth.
    """
    if not api_base or not mac:
        return []
    try:
        data = fetch_json(f"{api_base}/devices/{quote(mac, safe='')}/events", headers)
    except (HTTPError, URLError):
        return []
    events = data.get("events") if isinstance(data, dict) else data
    return events if isinstance(events, list) else []


def _event_code(event: dict[str, Any]) -> Any:
    """Event code, whether nested under eventType.code or at the top level."""
    event_type = event.get("eventType")
    if isinstance(event_type, dict) and event_type.get("code") is not None:
        return event_type.get("code")
    return event.get("code")


def extract_ota_history(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Code-119 (OTA_UPDATE_COMPLETED) events, newest-first, max 10.

    The version payload lives under event["data"].
    """
    ota_events: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict) or _event_code(event) != 119:
            continue
        data = event.get("data") or {}
        ota_events.append(
            {
                "date": safe_string(event.get("createdAt")),
                "version": normalize_version(safe_string(data.get("firmwareVersion"))),
                "version_code": data.get("newVersionCode"),
            }
        )
    # Sort newest-first by date string (ISO-8601 sorts lexicographically).
    ota_events.sort(key=lambda e: e.get("date", ""), reverse=True)
    return ota_events[:10]


def extract_startup_version(events: list[dict[str, Any]]) -> str:
    """Firmware version from the newest SYSTEM_STARTUP event (eventType code 100).

    SYSTEM_STARTUP fires after both OTA and cable updates; the version lives at
    event["data"]["firmwareVersion"].  Returns "" when no such event is present.
    """
    candidates: list[tuple[str, str]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        event_type = event.get("eventType") if isinstance(event.get("eventType"), dict) else {}
        if _event_code(event) != 100 and event_type.get("name") != "SYSTEM_STARTUP":
            continue
        version = safe_string((event.get("data") or {}).get("firmwareVersion"))
        if version:
            candidates.append((safe_string(event.get("createdAt")), version))
    if not candidates:
        return ""
    # Newest first (ISO-8601 timestamps sort lexicographically).
    candidates.sort(key=lambda item: item[0], reverse=True)
    return normalize_version(candidates[0][1])


def fetch_ota_history(api_base: str, mac: str, headers: dict[str, str]) -> list[dict[str, Any]]:
    """OTA history (code-119 events) for a device, newest-first, max 10."""
    return extract_ota_history(fetch_device_events(api_base, mac, headers))


def derive_pending_ota(
    reported_version: str,
    latest_firmware_version: str,
    ota_capable: bool | None,
) -> str:
    """Return pending OTA version when latest > reported AND ota_capable is True.

    Uses version_tuple for comparison; returns "" for any unknown/ineligible case.
    """
    if not ota_capable:
        return ""
    reported_t = version_tuple(reported_version)
    latest_t = version_tuple(latest_firmware_version)
    if reported_t is None or latest_t is None:
        return ""
    if latest_t > reported_t:
        return normalize_version(latest_firmware_version)
    return ""


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

    settings_obj = device_data.get("settings") or {}
    sensors = settings_obj.get("sensors", []) or []
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


def extract_location(device_data: dict[str, Any], entry: dict[str, Any]) -> str:
    settings = device_data.get("settings") or {}
    sensor_location = settings.get("sensorLocation") or {}
    return (
        safe_string(entry.get("location"))
        or safe_string(sensor_location.get("description"))
        or safe_string((device_data.get("location") or {}).get("name"))
        or ""
    )


def extract_hardware(entry: dict[str, Any]) -> str:
    return safe_string(entry.get("hardware"))


def entry_value(entry: dict[str, Any], *field_names: str) -> str:
    for field_name in field_names:
        value = safe_string(entry.get(field_name))
        if value:
            return value
    return ""


def entry_version(entry: dict[str, Any]) -> str:
    return normalize_version(
        entry_value(
            entry,
            "installed_firmware_version",
            "declared_deployment_version",
            "deployment_version",
            "deploymentVersion",
        )
    )


def entry_version_field(entry: dict[str, Any], *field_names: str) -> str:
    return normalize_version(entry_value(entry, *field_names))


def entry_last_firmware_update(entry: dict[str, Any]) -> str:
    return entry_value(entry, "last_firmware_update", "last_deployed", "deployment_date")


def entry_first_installed(entry: dict[str, Any]) -> str:
    return entry_value(entry, "first_installed", "initial_deployed", "initial_deployment_date")


def get_hardware_capabilities(device_registry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    capabilities = device_registry.get("hardware_capabilities")
    if not isinstance(capabilities, dict):
        return {}
    return {
        safe_string(name): value
        for name, value in capabilities.items()
        if isinstance(value, dict) and safe_string(name)
    }


def version_tuple(version: str) -> tuple[int, int, int] | None:
    match = re.match(r"v?(\d+)\.(\d+)\.(\d+)", safe_string(version))
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def resolve_ota_capable(
    entry: dict[str, Any],
    hardware: str,
    capabilities: dict[str, dict[str, Any]],
    installed_version: str,
) -> bool | None:
    """OTA needs both axes: a board the OTA firmware builds for, and that
    firmware actually installed. ota_min_firmware is the first release with
    an OTA client for the board; absent means no release ever shipped OTA
    for it."""
    override = entry.get("ota_override")
    if isinstance(override, bool):
        return override
    capability = capabilities.get(hardware)
    if not isinstance(capability, dict) or not capability:
        return None
    minimum = version_tuple(safe_string(capability.get("ota_min_firmware")))
    if minimum is None:
        return False
    installed = version_tuple(installed_version)
    if installed is None:
        return None
    return installed >= minimum


def resolve_hardware_eol(
    hardware: str,
    capabilities: dict[str, dict[str, Any]],
    target_version: str,
) -> bool | None:
    """True when the tracked target release no longer builds for this board,
    so no update path (OTA or cable) can reach it without a hardware swap."""
    capability = capabilities.get(hardware)
    if not isinstance(capability, dict) or not capability:
        return None
    maximum = version_tuple(safe_string(capability.get("max_firmware")))
    if maximum is None:
        return False
    target = version_tuple(target_version)
    if target is None:
        return None
    return target > maximum


def hardware_capability_fields(
    entry: dict[str, Any],
    hardware: str,
    capabilities: dict[str, dict[str, Any]],
    installed_version: str,
    target_version: str,
) -> dict[str, Any]:
    capability = capabilities.get(hardware)
    max_firmware = normalize_version((capability or {}).get("max_firmware")) if isinstance(capability, dict) else ""
    return {
        "ota_capable": resolve_ota_capable(entry, hardware, capabilities, installed_version),
        "hardware_eol": resolve_hardware_eol(hardware, capabilities, target_version),
        "hardware_max_firmware": max_firmware,
    }


# Candidate field names for a last-contact timestamp on GET /devices/{mac}.
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


def fetch_console_device_id(api_base: str, mac: str, headers: dict[str, str]) -> str:
    """Return the console device UUID via GET /devices/{mac}.

    Uses the per-device endpoint, which is reachable with the device-scoped
    Basic credential (no JWT / device-list access required).  Returns "" on any
    failure so callers degrade gracefully to an empty device_id.
    """
    if not api_base or not mac:
        return ""
    try:
        data = fetch_json(f"{api_base}/devices/{quote(mac, safe='')}", headers)
    except (HTTPError, URLError):
        return ""
    return safe_string(data.get("id")) if isinstance(data, dict) else ""


def fetch_latest_startup_version(api_base: str, mac: str, headers: dict[str, str]) -> str:
    """Reported version from the device's latest SYSTEM_STARTUP event ("" if none)."""
    return extract_startup_version(fetch_device_events(api_base, mac, headers))


def build_repo_device_summaries(
    repo_path: Path,
    track: dict[str, Any],
    capabilities: dict[str, dict[str, Any]],
    registry_entry_map: dict[str, dict[str, Any]],
    api_base: str = "",
    headers: dict[str, str] | None = None,
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
        if not mac:
            continue

        registry_entry = registry_entry_map.get(mac, {})
        if registry_entry_map and not registry_entry:
            continue

        hardware = (
            extract_hardware(registry_entry)
            or safe_string(version_data.get("hardware_target"))
            or safe_string(version_data.get("hardware"))
        )
        configured_components = extract_components(registry_entry, registry_entry) if registry_entry else []
        installed_version = entry_version(registry_entry) or choose_repo_device_version(version_data)

        registry_pending = entry_version_field(registry_entry, "pending_ota_version", "pendingOtaVersion")
        # The repo path makes no live-telemetry call, so query the per-device
        # endpoints explicitly.  device_id comes from GET /devices/{mac} (accepts
        # Basic or Bearer); the reported version comes from the latest
        # SYSTEM_STARTUP event (Bearer only).  Both degrade to "" on failure.
        api_mac = safe_string(registry_entry.get("mac")) or safe_string(version_data.get("mac_address"))
        api_headers = headers or {}
        device_id = fetch_console_device_id(api_base, api_mac, api_headers)
        # One events call yields both the reported version and the OTA history.
        events = fetch_device_events(api_base, api_mac, api_headers)
        reported_version = extract_startup_version(events)
        ota_history = extract_ota_history(events)
        # Effective version: reported (device truth) beats registry.
        if reported_version:
            version = reported_version
            version_source = "reported"
        else:
            version = installed_version
            version_source = "registry" if installed_version else ""
        version_mismatch = bool(
            reported_version and installed_version and reported_version != installed_version
        )
        output.append(
            {
                "name": safe_string(registry_entry.get("label")) or safe_string(device_meta.get("name")) or humanize_slug(device_dir.name),
                "mac": mac,
                "device_id": device_id,
                "version": version,
                "components": configured_components or extract_components_from_config(config_data),
                "hardware": hardware,
                **hardware_capability_fields(registry_entry, hardware, capabilities, version, track.get("latest_version", "")),
                "reported_version": reported_version,
                "version_source": version_source,
                "version_mismatch": version_mismatch,
                "battery_level": None,
                "location": entry_value(registry_entry, "location") or safe_string(version_data.get("deployment_location")),
                "last_deployed": entry_last_firmware_update(registry_entry) or safe_string(version_data.get("deployment_date")),
                "initial_deployed": entry_first_installed(registry_entry) or safe_string(version_data.get("initial_deployment_date")),
                "declared_deployment_version": installed_version,
                "pending_ota_version": registry_pending,
                "pending_ota_source": "registry" if registry_pending else "",
                "pending_ota_created": entry_value(registry_entry, "pending_ota_created", "pendingOtaCreated"),
                "ota_history": ota_history,
                "sensor_summary": entry_value(registry_entry, "sensor_summary") or safe_string(version_data.get("sensor_summary")),
                "notes": entry_value(registry_entry, "notes") or safe_string(version_data.get("notes")),
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
        track_map = {}
        if not isinstance(registry_section.get("tracks"), list) or not registry_section["tracks"]:
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
                "latest_version": existing.get("latest_version", "") or normalize_version(registry_track.get("latest_version")),
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
    *,
    directory_entry: dict[str, Any] | None = None,
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

    # --- Live telemetry from directory entry (Task 1) ---
    # If no directory_entry was supplied (empty dict = no match in directory),
    # attempt a per-device fallback via /logs/latest.
    dir_entry: dict[str, Any] = directory_entry if directory_entry else {}
    log_data: dict[str, Any] | None = None
    if not dir_entry:
        try:
            log_data = fetch_json(f"{api_base}/devices/{encoded_mac}/logs/latest", headers)
        except HTTPError as error:
            if error.code not in (401, 403, 404):
                raise
            log_data = None
        except URLError:
            log_data = None

    # Derive reported_version from lastLog (directory entry, device_data, or log_data).
    last_log: dict[str, Any] = {}
    for source in (dir_entry, device_data, log_data or {}):
        candidate = source.get("lastLog") if isinstance(source, dict) else None
        if isinstance(candidate, dict) and candidate:
            last_log = candidate
            break
    # If log_data itself is a log object (not a device wrapper), use it directly.
    if not last_log and isinstance(log_data, dict) and log_data.get("firmwareVersion"):
        last_log = log_data

    raw_reported = safe_string(last_log.get("firmwareVersion"))
    reported_version = normalize_version(raw_reported) if raw_reported else ""
    battery_level: float | None = last_log.get("batteryLevel") if isinstance(last_log.get("batteryLevel"), (int, float)) else None

    # Events (Bearer-only) supply the OTA history and, since firmwareVersion is
    # emitted only on SYSTEM_STARTUP events, the reported version when the latest
    # log carries none.
    events = fetch_device_events(api_base, mac, headers)
    if not reported_version:
        reported_version = extract_startup_version(events)

    # Console device UUID (links the OTA pill to the firmware config page).
    # Registry entry's device_id is a manual fallback for devices the API omits.
    device_id = safe_string(dir_entry.get("id")) or safe_string(device_data.get("id")) or safe_string(entry.get("device_id"))

    # --- Version resolution (reported beats registry beats firmware API) ---
    registry_version = entry_version(entry)
    api_firmware_version = normalize_version((firmware_data or {}).get("version"))
    # Effective version: reported > registry > firmware-API
    if reported_version:
        version = reported_version
        version_source = "reported"
    elif registry_version:
        version = registry_version
        version_source = "registry"
    elif api_firmware_version:
        version = api_firmware_version
        version_source = "registry"
    else:
        version = ""
        version_source = ""

    # version_mismatch: only when BOTH reported and registry exist and differ.
    version_mismatch = bool(
        reported_version and registry_version and reported_version != registry_version
    )

    # Hardware: registry entry wins; fall back to API device settings.hardwareInfo.board.
    hardware = extract_hardware(entry)
    if not hardware:
        settings = device_data.get("settings")
        if isinstance(settings, dict):
            hardware_info = settings.get("hardwareInfo") or {}
            hardware = safe_string(hardware_info.get("board")) if isinstance(hardware_info, dict) else ""

    firmware_build_date = safe_string((firmware_data or {}).get("buildDate"))
    last_deployed = entry_last_firmware_update(entry) or (firmware_build_date.split("T")[0] if firmware_build_date else "")

    # Effective installed version for OTA capability check uses version (device truth).
    hw_cap_fields = hardware_capability_fields(entry, hardware, capabilities, version, track.get("latest_version", ""))
    ota_capable = hw_cap_fields.get("ota_capable")

    # --- OTA audit trail (Task 2) ---
    ota_history = extract_ota_history(events)

    # --- Derive pending OTA (Task 2) ---
    latest_firmware_version = track.get("latest_version", "")
    api_derived_pending = derive_pending_ota(version, latest_firmware_version, ota_capable)

    # Registry fallback: use registry pending if API derivation came up empty.
    registry_pending = entry_version_field(entry, "pending_ota_version", "pendingOtaVersion")

    if api_derived_pending:
        # Clear if device has already received this update.
        reported_t = version_tuple(version)
        pending_t = version_tuple(api_derived_pending)
        if reported_t is not None and pending_t is not None and reported_t >= pending_t:
            pending_ota_version = ""
            pending_ota_source = ""
        else:
            pending_ota_version = api_derived_pending
            pending_ota_source = "api"
    elif registry_pending:
        # Clear registry pending if device is already at or above it.
        reported_t = version_tuple(version)
        pending_t = version_tuple(registry_pending)
        if reported_t is not None and pending_t is not None and reported_t >= pending_t:
            pending_ota_version = ""
            pending_ota_source = ""
        else:
            pending_ota_version = registry_pending
            pending_ota_source = "registry"
    else:
        pending_ota_version = ""
        pending_ota_source = ""

    return {
        "name": label or safe_string(device_data.get("deviceName")) or mac,
        "mac": safe_string(device_data.get("macAddress")) or mac,
        "device_id": device_id,
        "version": version,
        "components": extract_components(device_data, entry),
        "hardware": hardware,
        **hw_cap_fields,
        "reported_version": reported_version,
        "version_source": version_source,
        "version_mismatch": version_mismatch,
        "battery_level": battery_level,
        "location": extract_location(device_data, entry),
        "last_deployed": last_deployed,
        "initial_deployed": entry_first_installed(entry),
        "declared_deployment_version": registry_version,
        "pending_ota_version": pending_ota_version,
        "pending_ota_source": pending_ota_source,
        "pending_ota_created": entry_value(entry, "pending_ota_created", "pendingOtaCreated"),
        "ota_history": ota_history,
        "sensor_summary": safe_string(entry.get("sensor_summary")),
        "notes": safe_string(entry.get("notes")),
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
    registry_version = entry_version(entry)

    return {
        "name": label or mac,
        "mac": mac,
        "device_id": safe_string(entry.get("device_id")),
        "version": registry_version,
        "components": components,
        "hardware": hardware,
        **hardware_capability_fields(entry, hardware, capabilities, registry_version, track.get("latest_version", "")),
        "reported_version": "",
        "version_source": "",
        "version_mismatch": False,
        "battery_level": None,
        "location": safe_string(entry.get("location")),
        "last_deployed": entry_last_firmware_update(entry),
        "initial_deployed": entry_first_installed(entry),
        "declared_deployment_version": registry_version,
        "pending_ota_version": entry_version_field(entry, "pending_ota_version", "pendingOtaVersion"),
        "pending_ota_source": "registry" if entry_version_field(entry, "pending_ota_version", "pendingOtaVersion") else "",
        "pending_ota_created": entry_value(entry, "pending_ota_created", "pendingOtaCreated"),
        "ota_history": [],
        "sensor_summary": safe_string(entry.get("sensor_summary")),
        "notes": safe_string(entry.get("notes")),
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

    # Determine the primary api_base for auth (use the first section's api_base).
    primary_api_base = ""
    for _section in registry_sections:
        if isinstance(_section, dict):
            _base = safe_string(_section.get("api_base")) or safe_string(device_registry.get("api_base"))
            if _base:
                primary_api_base = _base
                break

    # Try Bearer auth; fall back to Basic.
    headers = build_auth(primary_api_base) if primary_api_base else build_headers()
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
        api_base = safe_string(registry_section.get("api_base")) or safe_string(device_registry.get("api_base"))
        repo_devices: list[dict[str, Any]] = []
        local_repo_paths: list[str] = []
        for track in tracks:
            repo_name = safe_string(track.get("repo_name"))
            if not repo_name:
                continue
            local_repo_path = resolve_local_repo_path([repo_name])
            if not local_repo_path:
                continue
            track_devices = build_repo_device_summaries(
                local_repo_path, track, hardware_capabilities, registry_entry_map,
                api_base=api_base, headers=headers,
            )
            if not track_devices:
                continue
            repo_devices.extend(track_devices)
            local_repo_paths.append(str(local_repo_path))

        if repo_devices:
            resolved_paths = ", ".join(dict.fromkeys(local_repo_paths))
            print(f"Loaded {len(repo_devices)} devices for section '{section_id}' from {resolved_paths}")
            section_output["devices"].extend(repo_devices)

        device_entries = registry_section.get("devices", []) or []
        represented_macs = {
            normalize_mac(device.get("mac"))
            for device in section_output["devices"]
            if isinstance(device, dict) and safe_string(device.get("mac"))
        }

        if device_entries and not api_base:
            print(f"Section '{section_id}' is missing api_base", file=sys.stderr)
            return 1

        # Fetch device directory once per section for live telemetry (Task 1).
        # Falls back gracefully to {} when the endpoint is unavailable.
        device_directory: dict[str, dict[str, Any]] = {}
        if api_base:
            device_directory = fetch_device_directory(api_base, headers)
            if device_directory:
                print(f"Fetched device directory for section '{section_id}': {len(device_directory)} devices")
            else:
                print(f"Device directory unavailable for section '{section_id}', using per-device fallback")

        for entry in device_entries:
            if not isinstance(entry, dict):
                continue

            mac = safe_string(entry.get("mac"))
            if not mac:
                continue
            if normalize_mac(mac) in represented_macs:
                continue

            dir_entry = device_directory.get(normalize_mac(mac))

            try:
                device_summary = fetch_device_summary(
                    api_base, entry, track_map, headers, hardware_capabilities,
                    directory_entry=dir_entry,
                )
            except HTTPError as error:
                failures.append(f"{section_id}:{mac}: HTTP {error.code}")
                device_summary = build_failure_device(entry, track_map, hardware_capabilities)
            except URLError as error:
                failures.append(f"{section_id}:{mac}: {error.reason}")
                device_summary = build_failure_device(entry, track_map, hardware_capabilities)

            section_output["devices"].append(device_summary)
            represented_macs.add(normalize_mac(mac))

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
