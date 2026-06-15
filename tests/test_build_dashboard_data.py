#!/usr/bin/env python3
"""Tests for build_dashboard_data.py — Tasks 1 and 2 of plan 05.

Network is never hit: fetch_json / post_form / post_json are monkeypatched.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

# Insert scripts/ so we can import the module without installing it.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_dashboard_data as bdd  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_entry(
    mac: str = "aa:bb:cc:dd:ee:ff",
    installed: str = "",
    pending: str = "",
    label: str = "Test Device",
    hardware: str = "Sensor hub v1.4 / ESP32-S3-WROOM-1U-N16",
    track: str = "sensor-hub",
    ota_override: bool | None = None,
) -> dict:
    entry: dict = {
        "mac": mac,
        "label": label,
        "hardware": hardware,
        "track": track,
    }
    if installed:
        entry["installed_firmware_version"] = installed
    if pending:
        entry["pending_ota_version"] = pending
    if ota_override is not None:
        entry["ota_override"] = ota_override
    return entry


def _make_track(latest: str = "v3.20.0") -> dict:
    return {"id": "sensor-hub", "label": "Sensor Hub", "latest_version": latest}


CAPABILITIES: dict = {
    "Sensor hub v1.4 / ESP32-S3-WROOM-1U-N16": {"ota_min_firmware": "v3.4.0"},
}

# ---------------------------------------------------------------------------
# Task 1 — Live telemetry
# ---------------------------------------------------------------------------


class TestBuildAuth(unittest.TestCase):
    """build_auth() tries POST /auth/token; falls back to Basic on failure."""

    def test_returns_bearer_on_success(self) -> None:
        with (
            patch.dict("os.environ", {"API_USERNAME": "user", "API_PASSWORD": "pass"}),
            patch.object(bdd, "post_form", return_value={"token": "jwt-abc"}) as mock_post,
        ):
            headers = bdd.build_auth("https://api.example.com")
        mock_post.assert_called_once()
        self.assertEqual(headers.get("Authorization"), "Bearer jwt-abc")

    def test_falls_back_to_basic_on_http_error(self) -> None:
        err = HTTPError("url", 401, "Unauthorized", {}, None)
        with (
            patch.dict("os.environ", {"API_USERNAME": "user", "API_PASSWORD": "pass"}),
            patch.object(bdd, "post_form", side_effect=err),
        ):
            headers = bdd.build_auth("https://api.example.com")
        self.assertIn("Authorization", headers)
        self.assertTrue(headers["Authorization"].startswith("Basic "))

    def test_falls_back_to_basic_on_url_error(self) -> None:
        from urllib.error import URLError
        with (
            patch.dict("os.environ", {"API_USERNAME": "user", "API_PASSWORD": "pass"}),
            patch.object(bdd, "post_form", side_effect=URLError("timeout")),
        ):
            headers = bdd.build_auth("https://api.example.com")
        self.assertIn("Authorization", headers)
        self.assertTrue(headers["Authorization"].startswith("Basic "))

    def test_no_creds_returns_no_auth_header(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            headers = bdd.build_auth("https://api.example.com")
        self.assertNotIn("Authorization", headers)


class TestFetchDeviceDirectory(unittest.TestCase):
    """fetch_device_directory handles both response shapes; returns {} on error."""

    def test_bare_list_shape(self) -> None:
        devices = [
            {"macAddress": "AABBCCDDEEFF", "isOnline": True},
            {"macAddress": "112233445566", "isOnline": False},
        ]
        with patch.object(bdd, "fetch_json", return_value=devices):
            result = bdd.fetch_device_directory("https://api.example.com", {})
        self.assertIn("aa:bb:cc:dd:ee:ff", result)
        self.assertIn("11:22:33:44:55:66", result)

    def test_wrapped_devices_shape(self) -> None:
        payload = {"devices": [{"macAddress": "AABBCCDDEEFF", "isOnline": True}]}
        with patch.object(bdd, "fetch_json", return_value=payload):
            result = bdd.fetch_device_directory("https://api.example.com", {})
        self.assertIn("aa:bb:cc:dd:ee:ff", result)

    def test_http_error_returns_empty(self) -> None:
        err = HTTPError("url", 401, "Unauthorized", {}, None)
        with patch.object(bdd, "fetch_json", side_effect=err):
            result = bdd.fetch_device_directory("https://api.example.com", {})
        self.assertEqual(result, {})

    def test_url_error_returns_empty(self) -> None:
        from urllib.error import URLError
        with patch.object(bdd, "fetch_json", side_effect=URLError("offline")):
            result = bdd.fetch_device_directory("https://api.example.com", {})
        self.assertEqual(result, {})


class TestVersionPrefersReportedOverRegistry(unittest.TestCase):
    """Reported firmware from lastLog beats registry installed_firmware_version."""

    def _run_summary(
        self,
        entry: dict,
        directory_entry: dict | None = None,
        firmware_data: dict | None = None,
    ) -> dict:
        """Invoke fetch_device_summary with a minimal plumbed environment."""
        device_data = {
            "macAddress": entry.get("mac", "aa:bb:cc:dd:ee:ff"),
            "lastLog": directory_entry.get("lastLog", {}) if directory_entry else {},
            "isOnline": (directory_entry or {}).get("isOnline"),
            "lastReportedAt": (directory_entry or {}).get("lastReportedAt"),
        }
        track_map = {"sensor-hub": _make_track("v3.20.0")}

        def fake_fetch(url: str, headers: dict) -> dict:
            if "/firmwares/latest" in url:
                if firmware_data is not None:
                    return firmware_data
                raise HTTPError(url, 404, "Not Found", {}, None)
            if "/logs/latest" in url:
                raise HTTPError(url, 404, "Not Found", {}, None)
            return device_data

        dir_entry_for_mac = directory_entry or {}

        with patch.object(bdd, "fetch_json", side_effect=fake_fetch):
            return bdd.fetch_device_summary(
                "https://api.example.com",
                entry,
                track_map,
                {},
                CAPABILITIES,
                directory_entry=dir_entry_for_mac,
            )

    def test_version_prefers_reported_over_registry(self) -> None:
        entry = _make_entry(installed="v3.16.6")
        dir_entry = {
            "macAddress": "aa:bb:cc:dd:ee:ff",
            "isOnline": True,
            "lastLog": {"firmwareVersion": "3.19.0", "createdAt": "2026-06-13T08:00:00Z"},
        }
        result = self._run_summary(entry, directory_entry=dir_entry)
        self.assertEqual(result["version"], "v3.19.0")
        self.assertEqual(result["version_source"], "reported")
        self.assertTrue(result["version_mismatch"])

    def test_version_falls_back_to_registry(self) -> None:
        entry = _make_entry(installed="v3.16.6")
        dir_entry = {
            "macAddress": "aa:bb:cc:dd:ee:ff",
            "isOnline": False,
            "lastLog": {},  # no firmwareVersion
        }
        result = self._run_summary(entry, directory_entry=dir_entry)
        self.assertEqual(result["version"], "v3.16.6")
        self.assertEqual(result["version_source"], "registry")
        self.assertFalse(result["version_mismatch"])

    def test_device_id_captured_from_directory_entry(self) -> None:
        entry = _make_entry()
        dir_entry = {
            "macAddress": "aa:bb:cc:dd:ee:ff",
            "id": "2399e27e-f9ef-487f-99e0-47f49f53e377",
            "isOnline": True,
            "lastLog": {"firmwareVersion": "3.20.0", "createdAt": "2026-06-13T08:00:00Z"},
        }
        result = self._run_summary(entry, directory_entry=dir_entry)
        self.assertEqual(result["device_id"], "2399e27e-f9ef-487f-99e0-47f49f53e377")

    def test_device_id_empty_when_absent(self) -> None:
        entry = _make_entry()
        dir_entry = {"macAddress": "aa:bb:cc:dd:ee:ff", "isOnline": True, "lastLog": {}}
        result = self._run_summary(entry, directory_entry=dir_entry)
        self.assertEqual(result["device_id"], "")

    def test_battery_level_extracted(self) -> None:
        entry = _make_entry()
        dir_entry = {
            "macAddress": "aa:bb:cc:dd:ee:ff",
            "isOnline": True,
            "lastLog": {"batteryLevel": 72.5, "createdAt": "2026-06-13T08:00:00Z"},
        }
        result = self._run_summary(entry, directory_entry=dir_entry)
        self.assertEqual(result["battery_level"], 72.5)

    def test_battery_level_null_when_absent(self) -> None:
        entry = _make_entry()
        dir_entry = {"macAddress": "aa:bb:cc:dd:ee:ff", "isOnline": True, "lastLog": {}}
        result = self._run_summary(entry, directory_entry=dir_entry)
        self.assertIsNone(result["battery_level"])


class TestRepoDeviceSummariesDeviceId(unittest.TestCase):
    """The local-repo build path must also populate device_id from the API.

    Devices backed by the sensorhub-data-collector submodule are built by
    build_repo_device_summaries(), which makes no live-telemetry call. It must
    fetch the console UUID from the per-device endpoint (reachable with the
    device-scoped Basic credential) so the OTA pill can link to the console.
    """

    MAC = "3C:0F:02:C7:EB:90"
    UUID = "9d1234ee-e518-4b2f-930f-1d564ab86279"

    def _make_repo(self, tmp: str) -> Path:
        repo = Path(tmp)
        device_dir = repo / "devices" / "ventilation-boost-button"
        device_dir.mkdir(parents=True)
        (device_dir / "config.json").write_text(
            json.dumps({"device": {"name": "Ventilation Boost Button"}}), encoding="utf-8"
        )
        (device_dir / "version.json").write_text(
            json.dumps({"mac_address": self.MAC, "installed_firmware_version": "v3.16.4"}),
            encoding="utf-8",
        )
        return repo

    def _registry_map(self) -> dict:
        return {
            bdd.normalize_mac(self.MAC): {
                "mac": self.MAC,
                "label": "Ventilation Boost Button",
                "track": "sensor-hub",
                "hardware": "Sensor hub v1.4 / ESP32-S3-WROOM-1U-N16",
                "installed_firmware_version": "v3.16.4",
            }
        }

    def test_device_id_fetched_from_per_device_endpoint(self) -> None:
        def fake_fetch(url: str, headers: dict) -> dict:
            if url.endswith("/devices/" + bdd.quote(self.MAC, safe="")):
                return {"id": self.UUID, "macAddress": self.MAC}
            raise HTTPError(url, 404, "Not Found", {}, None)

        with tempfile.TemporaryDirectory() as tmp:
            repo = self._make_repo(tmp)
            with patch.object(bdd, "fetch_json", side_effect=fake_fetch):
                result = bdd.build_repo_device_summaries(
                    repo, _make_track("v3.22.1"), CAPABILITIES, self._registry_map(),
                    api_base="https://api.example.com", headers={"Authorization": "Basic x"},
                )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["device_id"], self.UUID)

    def test_device_id_empty_when_api_unavailable(self) -> None:
        # No api_base supplied (offline build) -> device_id stays empty, no crash.
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._make_repo(tmp)
            result = bdd.build_repo_device_summaries(
                repo, _make_track("v3.22.1"), CAPABILITIES, self._registry_map(),
            )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["device_id"], "")

    def test_device_id_empty_on_api_error(self) -> None:
        def fake_fetch(url: str, headers: dict) -> dict:
            raise HTTPError(url, 401, "Unauthorized", {}, None)

        with tempfile.TemporaryDirectory() as tmp:
            repo = self._make_repo(tmp)
            with patch.object(bdd, "fetch_json", side_effect=fake_fetch):
                result = bdd.build_repo_device_summaries(
                    repo, _make_track("v3.22.1"), CAPABILITIES, self._registry_map(),
                    api_base="https://api.example.com", headers={},
                )
        self.assertEqual(result[0]["device_id"], "")


class TestFetchLatestStartupVersion(unittest.TestCase):
    """fetch_latest_startup_version parses the events wrapper and SYSTEM_STARTUP."""

    def _events(self, *triples):
        # triples: (createdAt, code, firmwareVersion)
        return {
            "events": [
                {
                    "createdAt": ts,
                    "eventType": {"code": code, "name": "SYSTEM_STARTUP" if code == 100 else "OTHER"},
                    "data": {"firmwareVersion": fw} if fw is not None else {},
                }
                for ts, code, fw in triples
            ],
            "page": 1,
            "totalResults": len(triples),
        }

    def test_returns_newest_startup_version_normalized(self) -> None:
        events = self._events(
            ("2026-05-01T00:00:00", 100, "3.16.4"),
            ("2026-06-15T10:10:48", 100, "3.22.1"),  # newest startup
            ("2026-06-15T11:00:00", 117, None),       # newer but not a startup
        )
        with patch.object(bdd, "fetch_json", return_value=events):
            v = bdd.fetch_latest_startup_version("https://api", "3C:0F:02:C7:EB:90", {})
        self.assertEqual(v, "v3.22.1")

    def test_returns_empty_when_no_startup_events(self) -> None:
        events = {"events": [{"createdAt": "x", "eventType": {"code": 117}, "data": {}}]}
        with patch.object(bdd, "fetch_json", return_value=events):
            v = bdd.fetch_latest_startup_version("https://api", "aa:bb", {})
        self.assertEqual(v, "")

    def test_returns_empty_on_http_error(self) -> None:
        def boom(url, headers):
            raise HTTPError(url, 401, "Unauthorized", {}, None)

        with patch.object(bdd, "fetch_json", side_effect=boom):
            v = bdd.fetch_latest_startup_version("https://api", "aa:bb", {})
        self.assertEqual(v, "")

    def test_returns_empty_without_api_base(self) -> None:
        self.assertEqual(bdd.fetch_latest_startup_version("", "aa:bb", {}), "")


class TestRepoDeviceSummariesReportedVersion(unittest.TestCase):
    """The repo path adopts the reported SYSTEM_STARTUP version over the registry."""

    MAC = "3C:0F:02:C7:EB:90"
    UUID = "9d1234ee-e518-4b2f-930f-1d564ab86279"

    def _make_repo(self, tmp: str) -> Path:
        device_dir = Path(tmp) / "devices" / "vent"
        device_dir.mkdir(parents=True)
        (device_dir / "config.json").write_text(json.dumps({"device": {"name": "Vent"}}), encoding="utf-8")
        (device_dir / "version.json").write_text(
            json.dumps({"mac_address": self.MAC, "installed_firmware_version": "v3.16.4"}), encoding="utf-8"
        )
        return Path(tmp)

    def _registry_map(self) -> dict:
        return {
            bdd.normalize_mac(self.MAC): {
                "mac": self.MAC,
                "label": "Vent",
                "track": "sensor-hub",
                "hardware": "Sensor hub v1.4 / ESP32-S3-WROOM-1U-N16",
                "installed_firmware_version": "v3.16.4",
            }
        }

    def test_reported_version_beats_registry(self) -> None:
        events = {
            "events": [
                {
                    "createdAt": "2026-06-15T10:10:48",
                    "eventType": {"code": 100, "name": "SYSTEM_STARTUP"},
                    "data": {"firmwareVersion": "3.22.1"},
                }
            ]
        }

        def fake_fetch(url: str, headers: dict):
            if url.endswith("/events"):
                return events
            if url.endswith("/devices/" + bdd.quote(self.MAC, safe="")):
                return {"id": self.UUID, "macAddress": self.MAC}
            raise HTTPError(url, 404, "Not Found", {}, None)

        with tempfile.TemporaryDirectory() as tmp:
            repo = self._make_repo(tmp)
            with patch.object(bdd, "fetch_json", side_effect=fake_fetch):
                result = bdd.build_repo_device_summaries(
                    repo, _make_track("v3.22.1"), CAPABILITIES, self._registry_map(),
                    api_base="https://api.example.com", headers={"Authorization": "Bearer x"},
                )
        dev = result[0]
        self.assertEqual(dev["version"], "v3.22.1")
        self.assertEqual(dev["version_source"], "reported")
        self.assertEqual(dev["reported_version"], "v3.22.1")
        self.assertEqual(dev["declared_deployment_version"], "v3.16.4")
        self.assertTrue(dev["version_mismatch"])

    def test_falls_back_to_registry_when_no_startup(self) -> None:
        def fake_fetch(url: str, headers: dict):
            if url.endswith("/events"):
                return {"events": []}
            if url.endswith("/devices/" + bdd.quote(self.MAC, safe="")):
                return {"id": self.UUID, "macAddress": self.MAC}
            raise HTTPError(url, 404, "Not Found", {}, None)

        with tempfile.TemporaryDirectory() as tmp:
            repo = self._make_repo(tmp)
            with patch.object(bdd, "fetch_json", side_effect=fake_fetch):
                result = bdd.build_repo_device_summaries(
                    repo, _make_track("v3.22.1"), CAPABILITIES, self._registry_map(),
                    api_base="https://api.example.com", headers={"Authorization": "Bearer x"},
                )
        dev = result[0]
        self.assertEqual(dev["version"], "v3.16.4")
        self.assertEqual(dev["version_source"], "registry")
        self.assertEqual(dev["reported_version"], "")
        self.assertFalse(dev["version_mismatch"])


class TestDeviceListFetchFallback(unittest.TestCase):
    """When directory fetch fails, per-device /logs/latest is tried."""

    def test_list_failure_falls_back_to_per_device_logs_latest(self) -> None:
        """Directory empty → fetch_device_summary calls /logs/latest for the device."""
        entry = _make_entry(mac="aa:bb:cc:dd:ee:ff", installed="v3.16.6")
        track_map = {"sensor-hub": _make_track("v3.20.0")}

        log_data = {
            "firmwareVersion": "3.19.0",
            "createdAt": "2026-06-13T08:00:00Z",
            "batteryLevel": 50.0,
        }

        call_log: list[str] = []

        def fake_fetch(url: str, headers: dict) -> dict:
            call_log.append(url)
            if "/firmwares/latest" in url:
                raise HTTPError(url, 404, "Not Found", {}, None)
            if "/logs/latest" in url:
                return log_data
            # GET /devices/{mac}
            return {"macAddress": "aa:bb:cc:dd:ee:ff"}

        with patch.object(bdd, "fetch_json", side_effect=fake_fetch):
            result = bdd.fetch_device_summary(
                "https://api.example.com",
                entry,
                track_map,
                {},
                CAPABILITIES,
                directory_entry={},  # empty = no directory entry
            )

        logs_latest_called = any("/logs/latest" in url for url in call_log)
        self.assertTrue(logs_latest_called, f"Expected /logs/latest call; got: {call_log}")
        self.assertEqual(result["version"], "v3.19.0")
        self.assertEqual(result["version_source"], "reported")


class TestStatusUsesEffectiveVersion(unittest.TestCase):
    """A device behind in the registry but current per reported version is Up to date."""

    def test_reported_version_current_beats_stale_registry(self) -> None:
        """Registry says v3.16.6 but device reports v3.20.0 (= latest) → not 'behind'."""
        entry = _make_entry(installed="v3.16.6")
        dir_entry = {
            "macAddress": "aa:bb:cc:dd:ee:ff",
            "isOnline": True,
            "lastLog": {"firmwareVersion": "3.20.0", "createdAt": "2026-06-13T08:00:00Z"},
        }
        track_map = {"sensor-hub": _make_track("v3.20.0")}

        def fake_fetch(url: str, headers: dict) -> dict:
            if "/firmwares/latest" in url:
                raise HTTPError(url, 404, "Not Found", {}, None)
            if "/logs/latest" in url:
                raise HTTPError(url, 404, "Not Found", {}, None)
            return {"macAddress": "aa:bb:cc:dd:ee:ff"}

        with patch.object(bdd, "fetch_json", side_effect=fake_fetch):
            result = bdd.fetch_device_summary(
                "https://api.example.com",
                entry,
                track_map,
                {},
                CAPABILITIES,
                directory_entry=dir_entry,
            )

        self.assertEqual(result["version"], "v3.20.0")
        # version matches target → would be "Up to date" in UI
        self.assertEqual(result["version"], result["target_version"])


class TestHardwareFallback(unittest.TestCase):
    """hardware = registry entry OR device_data.settings.hardwareInfo.board"""

    def test_hardware_from_settings_when_registry_empty(self) -> None:
        entry = _make_entry(hardware="")  # no registry hardware
        entry.pop("hardware", None)
        dir_entry = {"macAddress": "aa:bb:cc:dd:ee:ff", "isOnline": True, "lastLog": {}}
        track_map = {"sensor-hub": _make_track("v3.20.0")}

        def fake_fetch(url: str, headers: dict) -> dict:
            if "/firmwares/latest" in url:
                raise HTTPError(url, 404, "Not Found", {}, None)
            if "/logs/latest" in url:
                raise HTTPError(url, 404, "Not Found", {}, None)
            return {
                "macAddress": "aa:bb:cc:dd:ee:ff",
                "settings": {
                    "hardwareInfo": {"board": "sensor-hub-v1.5-esp32-s3-n16r8"}
                },
            }

        with patch.object(bdd, "fetch_json", side_effect=fake_fetch):
            result = bdd.fetch_device_summary(
                "https://api.example.com",
                entry,
                track_map,
                {},
                {},
                directory_entry=dir_entry,
            )

        self.assertEqual(result["hardware"], "sensor-hub-v1.5-esp32-s3-n16r8")

    def test_hardware_settings_none_does_not_crash(self) -> None:
        """settings may be None from API — must not crash."""
        entry = _make_entry(hardware="")
        entry.pop("hardware", None)
        dir_entry = {"macAddress": "aa:bb:cc:dd:ee:ff", "isOnline": True, "lastLog": {}}
        track_map = {"sensor-hub": _make_track("v3.20.0")}

        def fake_fetch(url: str, headers: dict) -> dict:
            if "/firmwares/latest" in url:
                raise HTTPError(url, 404, "Not Found", {}, None)
            if "/logs/latest" in url:
                raise HTTPError(url, 404, "Not Found", {}, None)
            return {"macAddress": "aa:bb:cc:dd:ee:ff", "settings": None}

        with patch.object(bdd, "fetch_json", side_effect=fake_fetch):
            result = bdd.fetch_device_summary(
                "https://api.example.com",
                entry,
                track_map,
                {},
                {},
                directory_entry=dir_entry,
            )
        # Should not raise and hardware should be empty string
        self.assertEqual(result["hardware"], "")


class TestBuildFailureDeviceNewFields(unittest.TestCase):
    """build_failure_device must include the new Task 1 fields as null/empty defaults."""

    def test_new_fields_present_with_defaults(self) -> None:
        entry = _make_entry(installed="v3.16.6")
        track_map = {"sensor-hub": _make_track()}
        result = bdd.build_failure_device(entry, track_map, CAPABILITIES)
        self.assertEqual(result["reported_version"], "")
        self.assertEqual(result["version_source"], "")
        self.assertFalse(result["version_mismatch"])
        self.assertIsNone(result["battery_level"])
        # Task 2 fields
        self.assertEqual(result["ota_history"], [])
        self.assertEqual(result["pending_ota_source"], "")


# ---------------------------------------------------------------------------
# Task 2 — OTA audit + derived pending
# ---------------------------------------------------------------------------


class TestOtaHistoryExtraction(unittest.TestCase):
    """fetch_ota_history keeps only code-119, newest first, max 10."""

    def _make_event(self, code: int, date: str, version: str, version_code: int, use_nested: bool = False) -> dict:
        if use_nested:
            return {
                "eventType": {"code": code, "name": "SomeEvent"},
                "createdAt": date,
                "payload": {"firmwareVersion": version, "newVersionCode": version_code},
            }
        return {
            "code": code,
            "createdAt": date,
            "payload": {"firmwareVersion": version, "newVersionCode": version_code},
        }

    def test_filters_to_code_119_only(self) -> None:
        events = [
            self._make_event(100, "2026-05-01T00:00:00Z", "3.16.0", 3160000),
            self._make_event(119, "2026-05-16T20:12:00Z", "3.16.6", 3160600),
            self._make_event(200, "2026-05-17T00:00:00Z", "3.16.6", 3160600),
        ]
        with patch.object(bdd, "fetch_json", return_value=events):
            result = bdd.fetch_ota_history("https://api.example.com", "aa:bb:cc:dd:ee:ff", {})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["version"], "v3.16.6")
        self.assertEqual(result[0]["version_code"], 3160600)

    def test_handles_nested_event_type_code(self) -> None:
        events = [
            self._make_event(119, "2026-05-16T20:12:00Z", "3.16.6", 3160600, use_nested=True),
        ]
        with patch.object(bdd, "fetch_json", return_value=events):
            result = bdd.fetch_ota_history("https://api.example.com", "aa:bb:cc:dd:ee:ff", {})
        self.assertEqual(len(result), 1)

    def test_newest_first_max_10(self) -> None:
        events = [
            self._make_event(119, f"2026-0{(i % 9) + 1}-{(i % 28) + 1:02d}T00:00:00Z", f"3.{i}.0", i * 10000)
            for i in range(1, 15)  # 14 events
        ]
        with patch.object(bdd, "fetch_json", return_value=events):
            result = bdd.fetch_ota_history("https://api.example.com", "aa:bb:cc:dd:ee:ff", {})
        self.assertLessEqual(len(result), 10)
        # Verify newest-first ordering
        if len(result) > 1:
            dates = [r["date"] for r in result]
            self.assertEqual(dates, sorted(dates, reverse=True))

    def test_http_error_returns_empty_list(self) -> None:
        err = HTTPError("url", 401, "Unauthorized", {}, None)
        with patch.object(bdd, "fetch_json", side_effect=err):
            result = bdd.fetch_ota_history("https://api.example.com", "aa:bb:cc:dd:ee:ff", {})
        self.assertEqual(result, [])

    def test_403_returns_empty_list(self) -> None:
        err = HTTPError("url", 403, "Forbidden", {}, None)
        with patch.object(bdd, "fetch_json", side_effect=err):
            result = bdd.fetch_ota_history("https://api.example.com", "aa:bb:cc:dd:ee:ff", {})
        self.assertEqual(result, [])

    def test_404_returns_empty_list(self) -> None:
        err = HTTPError("url", 404, "Not Found", {}, None)
        with patch.object(bdd, "fetch_json", side_effect=err):
            result = bdd.fetch_ota_history("https://api.example.com", "aa:bb:cc:dd:ee:ff", {})
        self.assertEqual(result, [])


class TestDerivePendingOta(unittest.TestCase):
    """derive_pending_ota logic."""

    def test_returns_pending_when_latest_newer_and_ota_capable(self) -> None:
        result = bdd.derive_pending_ota("v3.16.6", "v3.20.0", ota_capable=True)
        self.assertEqual(result, "v3.20.0")

    def test_returns_empty_when_not_ota_capable(self) -> None:
        result = bdd.derive_pending_ota("v3.16.6", "v3.20.0", ota_capable=False)
        self.assertEqual(result, "")

    def test_returns_empty_when_ota_capable_none(self) -> None:
        result = bdd.derive_pending_ota("v3.16.6", "v3.20.0", ota_capable=None)
        self.assertEqual(result, "")

    def test_returns_empty_when_already_at_latest(self) -> None:
        result = bdd.derive_pending_ota("v3.20.0", "v3.20.0", ota_capable=True)
        self.assertEqual(result, "")

    def test_returns_empty_when_reported_newer(self) -> None:
        result = bdd.derive_pending_ota("v3.21.0", "v3.20.0", ota_capable=True)
        self.assertEqual(result, "")

    def test_returns_empty_when_reported_version_missing(self) -> None:
        result = bdd.derive_pending_ota("", "v3.20.0", ota_capable=True)
        self.assertEqual(result, "")

    def test_returns_empty_when_latest_missing(self) -> None:
        result = bdd.derive_pending_ota("v3.16.6", "", ota_capable=True)
        self.assertEqual(result, "")


class TestPendingOtaDerivedWhenLatestFirmwareNewer(unittest.TestCase):
    """Integration: fetch_device_summary derives pending_ota_version from API."""

    def _run_summary(
        self,
        reported_firmware: str,
        latest_firmware: str,
        registry_pending: str = "",
        ota_capable_override: bool | None = True,
    ) -> dict:
        mac = "aa:bb:cc:dd:ee:ff"
        entry = _make_entry(mac=mac, installed="v3.16.6", pending=registry_pending)
        if ota_capable_override is not None:
            entry["ota_override"] = ota_capable_override
        dir_entry = {
            "macAddress": mac,
            "isOnline": True,
            "lastLog": {
                "firmwareVersion": reported_firmware,
                "createdAt": "2026-06-13T08:00:00Z",
            },
        }
        track_map = {"sensor-hub": _make_track(latest_firmware)}

        ota_events = [
            {
                "code": 119,
                "createdAt": "2026-05-16T20:12:00Z",
                "payload": {"firmwareVersion": "3.16.6", "newVersionCode": 3160600},
            }
        ]

        def fake_fetch(url: str, headers: dict) -> dict | list:
            if "/events" in url:
                return ota_events
            if "/firmwares/latest" in url:
                return {"version": latest_firmware, "buildDate": "2026-06-01T00:00:00Z"}
            if "/logs/latest" in url:
                raise HTTPError(url, 404, "Not Found", {}, None)
            return {"macAddress": mac}

        with patch.object(bdd, "fetch_json", side_effect=fake_fetch):
            return bdd.fetch_device_summary(
                "https://api.example.com",
                entry,
                track_map,
                {},
                CAPABILITIES,
                directory_entry=dir_entry,
            )

    def test_pending_ota_derived_when_latest_firmware_newer(self) -> None:
        result = self._run_summary("3.16.6", "v3.20.0", ota_capable_override=True)
        self.assertEqual(result["pending_ota_version"], "v3.20.0")
        self.assertEqual(result["pending_ota_source"], "api")

    def test_pending_ota_not_derived_when_not_ota_capable(self) -> None:
        result = self._run_summary("3.16.6", "v3.20.0", ota_capable_override=False)
        # ota_capable=False → no derived pending
        self.assertEqual(result["pending_ota_version"], "")

    def test_pending_ota_registry_fallback_when_events_unavailable(self) -> None:
        """When events endpoint is 401, fall back to registry pending_ota_version."""
        mac = "aa:bb:cc:dd:ee:ff"
        entry = _make_entry(mac=mac, installed="v3.16.6", pending="v3.18.0")
        entry["ota_override"] = True
        dir_entry = {
            "macAddress": mac,
            "isOnline": True,
            "lastLog": {
                "firmwareVersion": "3.16.6",
                "createdAt": "2026-06-13T08:00:00Z",
            },
        }
        track_map = {"sensor-hub": _make_track("v3.16.6")}  # latest == reported → no API derivation

        def fake_fetch(url: str, headers: dict) -> dict | list:
            if "/events" in url:
                raise HTTPError(url, 401, "Unauthorized", {}, None)
            if "/firmwares/latest" in url:
                raise HTTPError(url, 404, "Not Found", {}, None)
            if "/logs/latest" in url:
                raise HTTPError(url, 404, "Not Found", {}, None)
            return {"macAddress": mac}

        with patch.object(bdd, "fetch_json", side_effect=fake_fetch):
            result = bdd.fetch_device_summary(
                "https://api.example.com",
                entry,
                track_map,
                {},
                CAPABILITIES,
                directory_entry=dir_entry,
            )

        # latest == reported → API derive_pending_ota returns "" → fall back to registry
        self.assertEqual(result["pending_ota_version"], "v3.18.0")
        self.assertEqual(result["pending_ota_source"], "registry")

    def test_pending_cleared_when_reported_version_already_at_pending(self) -> None:
        """Device already received the OTA → pending cleared."""
        result = self._run_summary("3.20.0", "v3.20.0", registry_pending="v3.20.0", ota_capable_override=True)
        # reported >= latest → no pending
        self.assertEqual(result["pending_ota_version"], "")


class TestOtaHistoryInDeviceSummary(unittest.TestCase):
    """ota_history is wired into fetch_device_summary output."""

    def test_ota_history_present_in_summary(self) -> None:
        mac = "aa:bb:cc:dd:ee:ff"
        entry = _make_entry(mac=mac, installed="v3.16.6")
        dir_entry = {
            "macAddress": mac,
            "isOnline": True,
            "lastLog": {"firmwareVersion": "3.16.6", "createdAt": "2026-06-13T08:00:00Z"},
        }
        track_map = {"sensor-hub": _make_track("v3.20.0")}

        ota_events = [
            {
                "code": 119,
                "createdAt": "2026-05-16T20:12:00Z",
                "payload": {"firmwareVersion": "3.16.6", "newVersionCode": 3160600},
            }
        ]

        def fake_fetch(url: str, headers: dict) -> dict | list:
            if "/events" in url:
                return ota_events
            if "/firmwares/latest" in url:
                raise HTTPError(url, 404, "Not Found", {}, None)
            if "/logs/latest" in url:
                raise HTTPError(url, 404, "Not Found", {}, None)
            return {"macAddress": mac}

        with patch.object(bdd, "fetch_json", side_effect=fake_fetch):
            result = bdd.fetch_device_summary(
                "https://api.example.com",
                entry,
                track_map,
                {},
                CAPABILITIES,
                directory_entry=dir_entry,
            )

        self.assertIn("ota_history", result)
        self.assertEqual(len(result["ota_history"]), 1)
        self.assertEqual(result["ota_history"][0]["version"], "v3.16.6")


if __name__ == "__main__":
    unittest.main()
