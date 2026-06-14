#!/usr/bin/env python3
"""Shared helpers for firmware rollout orchestration.

Stdlib-only.  Network access is funnelled through `fetch_json` / `post_json`
so tests can monkeypatch them; nothing here ever touches the network unless a
caller passes `execute=True` through the higher-level orchestration code.

This module NEVER uploads firmware binaries.  A rollout reuses the `fileUrl`
of an already-existing firmware record (created earlier by
`glaecier-sensorhub-data-collector/scripts/deploy-ota.sh` or the console UI)
and creates device-scoped records for the fan-out set.
"""

import base64
import io
import json
import os
import re
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

API_BASE = "https://monitoring-api.3aentreprise.com"


# --------------------------------------------------------------------------
# Network helpers (monkeypatched in tests)
# --------------------------------------------------------------------------

def fetch_json(url, headers):
    request = Request(url, headers=headers)
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def post_json(url, data, headers):
    body = json.dumps(data).encode("utf-8")
    req_headers = {**headers, "Content-Type": "application/json"}
    request = Request(url, data=body, headers=req_headers, method="POST")
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def post_form(url, fields, headers):
    body = urlencode(fields).encode("utf-8")
    req_headers = {**headers, "Content-Type": "application/x-www-form-urlencoded"}
    request = Request(url, data=body, headers=req_headers, method="POST")
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def build_auth(api_base=API_BASE):
    """Return auth headers, preferring Bearer JWT; fall back to HTTP Basic.

    Mirrors build_dashboard_data.build_auth.  Tries POST {api_base}/auth/token
    with form fields username/password from env API_USERNAME/API_PASSWORD.  On
    any failure returns Basic headers (or just Accept headers when no creds).
    """
    base_headers = {
        "Accept": "application/json",
        "User-Agent": "3a-console-rollout/1.0",
    }
    username = os.getenv("API_USERNAME", "").strip()
    password = os.getenv("API_PASSWORD", "").strip()
    if not username or not password:
        return base_headers
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
    encoded = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return {**base_headers, "Authorization": f"Basic {encoded}"}


# --------------------------------------------------------------------------
# Value helpers
# --------------------------------------------------------------------------

def safe_string(value):
    return str(value).strip() if value is not None else ""


def normalize_version(raw_version):
    if not raw_version:
        return ""
    version = str(raw_version).strip()
    if not version:
        return ""
    return f"v{version[1:] if version.startswith('v') else version}"


def normalize_mac(raw_mac):
    text = "".join(ch for ch in safe_string(raw_mac) if ch.isalnum())
    if len(text) == 12:
        return ":".join(text[i:i + 2] for i in range(0, 12, 2)).lower()
    return safe_string(raw_mac).lower()


def version_tuple(version):
    match = re.match(r"v?(\d+)\.(\d+)\.(\d+)", safe_string(version))
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def version_code(version, build=0):
    """versionCode = major*1000000 + minor*10000 + patch*100 + build.

    Returns None for unparseable versions.
    """
    parts = version_tuple(version)
    if parts is None:
        return None
    major, minor, patch = parts
    try:
        build_int = int(build)
    except (TypeError, ValueError):
        build_int = 0
    return major * 1000000 + minor * 10000 + patch * 100 + build_int


def parse_timestamp(value):
    text = safe_string(value)
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def now_utc():
    return datetime.now(timezone.utc)


def iso_now():
    return now_utc().replace(microsecond=0).isoformat()


# --------------------------------------------------------------------------
# State file IO
# --------------------------------------------------------------------------

EMPTY_STATE = {"active": None, "history": []}


def load_state(path):
    try:
        with io.open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (FileNotFoundError, ValueError):
        return {"active": None, "history": []}
    if not isinstance(data, dict):
        return {"active": None, "history": []}
    data.setdefault("active", None)
    data.setdefault("history", [])
    return data


def write_state(path, state):
    with io.open(path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2)
        handle.write("\n")


# --------------------------------------------------------------------------
# Dashboard data
# --------------------------------------------------------------------------

def load_dashboard(path):
    with io.open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def find_sensor_hub_section(dashboard):
    """Return the sensor-hub section dict, or {} if absent."""
    for section in dashboard.get("sections", []) or []:
        if not isinstance(section, dict):
            continue
        if normalize_mac(section.get("id")) in ("sensorhub", "sensor:hub"):
            return section
        if safe_string(section.get("id")).lower() in ("sensor-hub", "sensorhub", "sensor_hub"):
            return section
    return {}


# --------------------------------------------------------------------------
# API helpers (read-only)
# --------------------------------------------------------------------------

def find_source_firmware(api_base, target_code, headers):
    """Return the first existing firmware record whose versionCode == target.

    Filters client-side (server filter params are unverified).  Returns None
    when no such record exists -- the caller MUST refuse to start in that case
    (safety invariant #5: never fabricate a fileUrl).
    """
    try:
        records = fetch_json(f"{api_base}/firmwares", headers)
    except (HTTPError, URLError):
        return None
    if isinstance(records, dict):
        records = records.get("firmwares") or records.get("items") or []
    if not isinstance(records, list):
        return None
    for record in records:
        if not isinstance(record, dict):
            continue
        try:
            code = int(record.get("versionCode"))
        except (TypeError, ValueError):
            continue
        if code == target_code and safe_string(record.get("fileUrl")):
            return record
    return None


def resolve_device_uuid(api_base, mac, headers):
    """Resolve a device UUID from the API by MAC via GET /devices/{mac}.

    Returns None on 404 / error.
    """
    encoded = quote(mac, safe="")
    try:
        data = fetch_json(f"{api_base}/devices/{encoded}", headers)
    except (HTTPError, URLError):
        return None
    if not isinstance(data, dict):
        return None
    return safe_string(data.get("id")) or None


def latest_version_code_for_mac(api_base, mac, headers):
    """Return the device's current /firmwares/latest versionCode, or None."""
    encoded = quote(mac, safe="")
    try:
        data = fetch_json(f"{api_base}/firmwares/latest?deviceMac={encoded}", headers)
    except (HTTPError, URLError):
        return None
    if not isinstance(data, dict):
        return None
    code = data.get("versionCode")
    try:
        return int(code)
    except (TypeError, ValueError):
        return None


def latest_log_for_mac(api_base, mac, headers):
    """Return the device's latest log dict via GET /devices/{mac}/logs/latest."""
    encoded = quote(mac, safe="")
    try:
        data = fetch_json(f"{api_base}/devices/{encoded}/logs/latest", headers)
    except (HTTPError, URLError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def create_device_firmware_record(api_base, source_record, version, target_code,
                                   device_uuid, headers):
    """Create a device-scoped firmware record reusing the source fileUrl.

    NEVER uploads a binary -- it reuses source_record['fileUrl'].
    """
    payload = {
        "buildDate": safe_string(source_record.get("buildDate")) or iso_now(),
        "fileUrl": safe_string(source_record.get("fileUrl")),
        "versionCode": target_code,
        "version": normalize_version(version)[1:],  # API stores without leading v
        "deviceId": device_uuid,
    }
    return post_json(f"{api_base}/firmwares", payload, headers)
