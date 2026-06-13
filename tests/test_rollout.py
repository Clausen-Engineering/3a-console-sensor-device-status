"""Tests for the firmware rollout orchestration scripts.

All network access goes through module-level helpers (`fetch_json`, `post_json`,
`build_auth`) which these tests monkeypatch -- nothing here ever touches the
network.  State is read/written from a temp file inside a per-test temp dir.

Run only this file (sibling test files may be added concurrently):
    python -m unittest tests.test_rollout -v
"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

# Make scripts/ importable.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import rollout_common  # noqa: E402
import rollout_firmware  # noqa: E402
import check_rollout  # noqa: E402


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

def make_dashboard():
    """A dashboard-data.json-shaped dict with a sensor-hub section.

    - eligible1 / eligible2: behind target, ota_capable, not eol  -> eligible
    - not-ota: behind but ota_capable False                       -> skipped
    - eol: hardware_eol True / hardware_max_firmware below target  -> skipped
    - uptodate: already at target                                  -> not eligible
    """
    return {
        "sections": [
            {
                "id": "controllers",
                "label": "Controllers",
                "latest_version": "",
                "devices": [
                    {"name": "Ctrl", "mac": "aa:aa:aa:aa:aa:aa", "version": "v1.0.0",
                     "ota_capable": True, "hardware_eol": False,
                     "hardware_max_firmware": "", "target_version": "v1.0.0"},
                ],
            },
            {
                "id": "sensor-hub",
                "label": "Sensor Hub",
                "latest_version": "v3.21.0",
                "devices": [
                    {"name": "Eligible One", "mac": "3c:0f:02:c7:eb:cc",
                     "version": "v3.19.0", "ota_capable": True,
                     "hardware_eol": False, "hardware_max_firmware": "",
                     "target_version": "v3.21.0"},
                    {"name": "Eligible Two", "mac": "e0:72:a1:f5:0d:cc",
                     "version": "v3.16.6", "ota_capable": True,
                     "hardware_eol": False, "hardware_max_firmware": "",
                     "target_version": "v3.21.0"},
                    {"name": "Not OTA", "mac": "11:22:33:44:55:66",
                     "version": "v3.10.0", "ota_capable": False,
                     "hardware_eol": False, "hardware_max_firmware": "",
                     "target_version": "v3.21.0"},
                    {"name": "EOL Stranded", "mac": "68:fe:71:16:a9:78",
                     "version": "v2.11.2", "ota_capable": False,
                     "hardware_eol": True, "hardware_max_firmware": "v2.16.1",
                     "target_version": "v3.21.0"},
                    {"name": "Up To Date", "mac": "99:99:99:99:99:99",
                     "version": "v3.21.0", "ota_capable": True,
                     "hardware_eol": False, "hardware_max_firmware": "",
                     "target_version": "v3.21.0"},
                ],
            },
        ],
    }


def write_json(path, obj):
    with io.open(path, "w", encoding="utf-8") as handle:
        json.dump(obj, handle)


def read_json(path):
    with io.open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


class FakeApi:
    """Records POSTs and serves canned GET responses by URL substring."""

    def __init__(self):
        self.posts = []           # list of (url, data)
        self.firmwares = []       # list returned by GET /firmwares
        self.latest_by_mac = {}   # mac -> versionCode for /firmwares/latest
        self.uuid_by_mac = {}     # mac -> device uuid
        self.logs_by_mac = {}     # mac -> log dict for /logs/latest
        self.next_record_id = 1000

    def fetch_json(self, url, headers):
        if "/firmwares/latest" in url:
            mac = _mac_from_url(url)
            code = self.latest_by_mac.get(mac)
            if code is None:
                return {}
            return {"versionCode": code}
        if "/firmwares" in url:
            return list(self.firmwares)
        if "/logs/latest" in url:
            mac = _url_segment(url, "devices")
            return self.logs_by_mac.get(mac, {})
        if "/devices/" in url:
            mac = _url_segment(url, "devices")
            uuid = self.uuid_by_mac.get(mac)
            if uuid is None:
                from urllib.error import HTTPError
                raise HTTPError(url, 404, "not found", {}, None)
            return {"id": uuid, "macAddress": mac}
        return {}

    def post_json(self, url, data, headers):
        self.posts.append((url, data))
        rec = {"id": "fw-%d" % self.next_record_id}
        self.next_record_id += 1
        rec.update(data)
        return rec


def _mac_from_url(url):
    # /firmwares/latest?deviceMac=aa:bb...
    if "deviceMac=" in url:
        return url.split("deviceMac=", 1)[1].split("&", 1)[0].replace("%3A", ":").replace("%3a", ":")
    return ""


def _url_segment(url, after):
    # Pull the path segment immediately after `/<after>/`.
    path = url.split("?", 1)[0]
    parts = path.split("/")
    if after in parts:
        idx = parts.index(after)
        if idx + 1 < len(parts):
            return parts[idx + 1].replace("%3A", ":").replace("%3a", ":")
    return ""


# --------------------------------------------------------------------------
# Base
# --------------------------------------------------------------------------

class RolloutTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = os.path.join(self.tmp.name, "rollout-state.json")
        self.dash_path = os.path.join(self.tmp.name, "dashboard-data.json")
        write_json(self.dash_path, make_dashboard())
        write_json(self.state_path, {"active": None, "history": []})

        self.api = FakeApi()
        # All target devices resolve to a UUID by default.
        self.api.uuid_by_mac = {
            "3c:0f:02:c7:eb:cc": "uuid-canary",
            "e0:72:a1:f5:0d:cc": "uuid-two",
        }
        # A source firmware record at the target versionCode exists.
        self.target_code = rollout_common.version_code("v3.21.0")
        self.api.firmwares = [
            {"id": "src-fw", "version": "3.21.0", "versionCode": self.target_code,
             "fileUrl": "https://files/fw-3.21.0.bin", "deviceId": "some-other"},
        ]

        # Monkeypatch network helpers in BOTH modules and the shared helper.
        for mod in (rollout_common, rollout_firmware, check_rollout):
            if hasattr(mod, "fetch_json"):
                mod.fetch_json = self.api.fetch_json
            if hasattr(mod, "post_json"):
                mod.post_json = self.api.post_json
            if hasattr(mod, "build_auth"):
                mod.build_auth = lambda api_base: {}

    def run_rollout(self, argv):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = rollout_firmware.main(argv)
        return rc, out.getvalue()

    def run_check(self, argv):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = check_rollout.main(argv)
        return rc, out.getvalue()


# --------------------------------------------------------------------------
# version_code formula
# --------------------------------------------------------------------------

class VersionCodeTests(unittest.TestCase):
    def test_formula(self):
        # major*1000000 + minor*10000 + patch*100 + build
        self.assertEqual(rollout_common.version_code("v3.21.0"), 3210000)
        self.assertEqual(rollout_common.version_code("3.16.6"), 3160600)
        self.assertEqual(rollout_common.version_code("v3.16.6", build=4), 3160604)

    def test_invalid_returns_none(self):
        self.assertIsNone(rollout_common.version_code("not-a-version"))


# --------------------------------------------------------------------------
# Eligibility
# --------------------------------------------------------------------------

class EligibilityTests(RolloutTestBase):
    def test_all_eligible_filters_non_ota_and_eol_with_reasons(self):
        rc, out = self.run_rollout([
            "--version", "v3.21.0", "--all-eligible",
            "--dashboard", self.dash_path, "--state", self.state_path,
        ])
        self.assertEqual(rc, 0)
        # The two eligible devices appear in the plan.
        self.assertIn("3c:0f:02:c7:eb:cc", out)
        self.assertIn("e0:72:a1:f5:0d:cc", out)
        # Ineligible devices are skipped WITH a printed reason.
        self.assertIn("11:22:33:44:55:66", out)
        self.assertIn("e0:72:a1:f5:0d:cc", out)
        self.assertRegex(out.lower(), r"ota")     # reason mentions ota-capability
        self.assertRegex(out.lower(), r"eol|stranded|hardware")  # eol reason
        # Up-to-date device is not part of the eligible target set.
        self.assertNotIn("99:99:99:99:99:99", out.split("PLAN", 1)[-1] if "PLAN" in out else out)

    def test_explicit_devices_skip_non_ota_with_reason(self):
        rc, out = self.run_rollout([
            "--version", "v3.21.0",
            "--devices", "3c:0f:02:c7:eb:cc,11:22:33:44:55:66",
            "--dashboard", self.dash_path, "--state", self.state_path,
        ])
        self.assertEqual(rc, 0)
        self.assertIn("11:22:33:44:55:66", out)
        self.assertRegex(out.lower(), r"skip")


# --------------------------------------------------------------------------
# --execute gate (safety invariant #1)
# --------------------------------------------------------------------------

class ExecuteGateTests(RolloutTestBase):
    def test_dry_run_makes_zero_network_writes_and_no_state_file_change(self):
        before = read_json(self.state_path)
        rc, out = self.run_rollout([
            "--version", "v3.21.0", "--all-eligible",
            "--dashboard", self.dash_path, "--state", self.state_path,
        ])
        self.assertEqual(rc, 0)
        # ZERO mutating network calls in dry-run.
        self.assertEqual(self.api.posts, [])
        # State file unchanged.
        self.assertEqual(read_json(self.state_path), before)
        self.assertIn("dry-run", out.lower())

    def test_execute_creates_records_and_writes_state(self):
        rc, out = self.run_rollout([
            "--version", "v3.21.0", "--all-eligible", "--execute",
            "--dashboard", self.dash_path, "--state", self.state_path,
        ])
        self.assertEqual(rc, 0)
        # POSTs occurred against /firmwares.
        self.assertTrue(self.api.posts)
        for url, _ in self.api.posts:
            self.assertIn("/firmwares", url)
        state = read_json(self.state_path)
        self.assertIsNotNone(state["active"])
        self.assertEqual(state["active"]["version"], "v3.21.0")
        self.assertEqual(state["active"]["version_code"], 3210000)


# --------------------------------------------------------------------------
# Canary mode (safety: one record now)
# --------------------------------------------------------------------------

class CanaryTests(RolloutTestBase):
    def test_canary_creates_exactly_one_record_others_pending(self):
        rc, out = self.run_rollout([
            "--version", "v3.21.0", "--all-eligible",
            "--canary", "3c:0f:02:c7:eb:cc", "--execute",
            "--dashboard", self.dash_path, "--state", self.state_path,
        ])
        self.assertEqual(rc, 0)
        # Exactly one device-scoped record created (the canary).
        self.assertEqual(len(self.api.posts), 1)
        _, body = self.api.posts[0]
        self.assertEqual(body["deviceId"], "uuid-canary")
        self.assertEqual(body["fileUrl"], "https://files/fw-3.21.0.bin")
        self.assertEqual(body["versionCode"], 3210000)

        state = read_json(self.state_path)
        active = state["active"]
        self.assertEqual(active["mode"], "canary")
        self.assertEqual(active["canary_mac"], "3c:0f:02:c7:eb:cc")
        by_mac = {d["mac"]: d for d in active["devices"]}
        self.assertEqual(by_mac["3c:0f:02:c7:eb:cc"]["state"], "canary")
        self.assertEqual(by_mac["e0:72:a1:f5:0d:cc"]["state"], "pending")


# --------------------------------------------------------------------------
# Source record must pre-exist (safety invariant #5)
# --------------------------------------------------------------------------

class SourceRecordTests(RolloutTestBase):
    def test_refuse_when_no_source_record_exists(self):
        self.api.firmwares = []  # no record at target versionCode
        rc, out = self.run_rollout([
            "--version", "v3.21.0", "--all-eligible", "--execute",
            "--dashboard", self.dash_path, "--state", self.state_path,
        ])
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.api.posts, [])  # never fabricated a record
        self.assertRegex(out.lower(), r"no.*(source|firmware|record)")
        # State not started.
        self.assertIsNone(read_json(self.state_path)["active"])


# --------------------------------------------------------------------------
# Skip devices already at/above target (safety invariant #6)
# --------------------------------------------------------------------------

class AlreadyAtTargetTests(RolloutTestBase):
    def test_device_at_or_above_target_marked_updated_no_record(self):
        # Canary's CURRENT /firmwares/latest is already at target.
        self.api.latest_by_mac["e0:72:a1:f5:0d:cc"] = self.target_code
        rc, out = self.run_rollout([
            "--version", "v3.21.0", "--all-eligible", "--execute",
            "--dashboard", self.dash_path, "--state", self.state_path,
        ])
        self.assertEqual(rc, 0)
        state = read_json(self.state_path)
        by_mac = {d["mac"]: d for d in state["active"]["devices"]}
        self.assertIn(by_mac["e0:72:a1:f5:0d:cc"]["state"], ("updated", "skipped"))
        # No record created for the already-current device.
        for _, body in self.api.posts:
            self.assertNotEqual(body.get("deviceId"), "uuid-two")


# --------------------------------------------------------------------------
# Refuse second concurrent rollout (safety invariant #4)
# --------------------------------------------------------------------------

class ConcurrencyTests(RolloutTestBase):
    def test_refuse_second_rollout_when_active_exists(self):
        write_json(self.state_path, {
            "active": {"rollout_id": "v3.20.0-x", "version": "v3.20.0",
                       "version_code": 3200000, "mode": "all", "devices": []},
            "history": [],
        })
        rc, out = self.run_rollout([
            "--version", "v3.21.0", "--all-eligible", "--execute",
            "--dashboard", self.dash_path, "--state", self.state_path,
        ])
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.api.posts, [])
        self.assertRegex(out.lower(), r"active|abort|in progress")

    def test_abort_archives_active_to_history_as_aborted(self):
        write_json(self.state_path, {
            "active": {"rollout_id": "v3.20.0-x", "version": "v3.20.0",
                       "version_code": 3200000, "mode": "all",
                       "devices": [{"mac": "x", "label": "X", "state": "offered",
                                    "firmware_record_id": "fw-1"}]},
            "history": [],
        })
        rc, out = self.run_rollout([
            "--abort", "--execute", "--state", self.state_path,
        ])
        self.assertEqual(rc, 0)
        state = read_json(self.state_path)
        self.assertIsNone(state["active"])
        self.assertEqual(len(state["history"]), 1)
        self.assertEqual(state["history"][0]["state"], "aborted")
        # Abort reports created records for review (does not delete them).
        self.assertIn("fw-1", out)


# --------------------------------------------------------------------------
# check_rollout.py behavior
# --------------------------------------------------------------------------

class CheckRolloutTests(RolloutTestBase):
    def test_no_active_rollout_exits_zero_silently(self):
        rc, out = self.run_check(["--state", self.state_path])
        self.assertEqual(rc, 0)

    def _active_canary_state(self, canary_state="canary"):
        return {
            "active": {
                "rollout_id": "v3.21.0-20260613T1200Z",
                "version": "v3.21.0", "version_code": self.target_code,
                "mode": "canary", "canary_mac": "3c:0f:02:c7:eb:cc",
                "canary_deadline_h": 24,
                "created_at": "2026-06-13T12:00:00+00:00",
                "source_firmware_id": "src-fw",
                "devices": [
                    {"mac": "3c:0f:02:c7:eb:cc", "label": "Eligible One",
                     "state": canary_state, "reason": "",
                     "firmware_record_id": "fw-1000",
                     "updated_at": "2026-06-13T12:00:00+00:00"},
                    {"mac": "e0:72:a1:f5:0d:cc", "label": "Eligible Two",
                     "state": "pending", "reason": "",
                     "firmware_record_id": None,
                     "updated_at": "2026-06-13T12:00:00+00:00"},
                ],
            },
            "history": [],
        }

    def test_canary_success_fans_out_pending_to_offered(self):
        write_json(self.state_path, self._active_canary_state())
        # Canary reports the target version with a post-update heartbeat.
        self.api.logs_by_mac["3c:0f:02:c7:eb:cc"] = {
            "firmwareVersion": "3.21.0",
            "createdAt": "2026-06-13T13:00:00+00:00",  # AFTER record updated_at
        }
        rc, out = self.run_check([
            "--execute", "--state", self.state_path,
        ])
        self.assertEqual(rc, 0)
        state = read_json(self.state_path)
        by_mac = {d["mac"]: d for d in state["active"]["devices"]}
        self.assertEqual(by_mac["3c:0f:02:c7:eb:cc"]["state"], "updated")
        self.assertEqual(by_mac["e0:72:a1:f5:0d:cc"]["state"], "offered")
        # A device-scoped record was created for the fanned-out device.
        self.assertTrue(self.api.posts)
        self.assertEqual(self.api.posts[0][1]["deviceId"], "uuid-two")

    def test_canary_deadline_halts_no_fanout(self):
        state = self._active_canary_state()
        state["active"]["created_at"] = "2026-06-10T00:00:00+00:00"
        state["active"]["devices"][0]["updated_at"] = "2026-06-10T00:00:00+00:00"
        write_json(self.state_path, state)
        # Canary has NOT reported the target version.
        self.api.logs_by_mac["3c:0f:02:c7:eb:cc"] = {
            "firmwareVersion": "3.19.0",
            "createdAt": "2026-06-10T01:00:00+00:00",
        }
        rc, out = self.run_check([
            "--execute", "--state", self.state_path,
            "--now", "2026-06-13T00:00:00+00:00",
        ])
        self.assertEqual(rc, 0)
        state = read_json(self.state_path)
        self.assertEqual(state["active"]["state"], "halted")
        by_mac = {d["mac"]: d for d in state["active"]["devices"]}
        self.assertEqual(by_mac["3c:0f:02:c7:eb:cc"]["state"], "failed")
        # No fan-out ever: pending stays pending, no records created.
        self.assertEqual(by_mac["e0:72:a1:f5:0d:cc"]["state"], "pending")
        self.assertEqual(self.api.posts, [])

    def test_all_updated_archives_to_history_completed(self):
        state = self._active_canary_state(canary_state="updated")
        state["active"]["devices"][1]["state"] = "offered"
        state["active"]["devices"][1]["firmware_record_id"] = "fw-2000"
        write_json(self.state_path, state)
        # Both devices now report the target version.
        self.api.logs_by_mac["3c:0f:02:c7:eb:cc"] = {
            "firmwareVersion": "3.21.0", "createdAt": "2026-06-13T13:00:00+00:00"}
        self.api.logs_by_mac["e0:72:a1:f5:0d:cc"] = {
            "firmwareVersion": "3.21.0", "createdAt": "2026-06-13T13:30:00+00:00"}
        rc, out = self.run_check([
            "--execute", "--state", self.state_path,
        ])
        self.assertEqual(rc, 0)
        state = read_json(self.state_path)
        self.assertIsNone(state["active"])
        self.assertEqual(len(state["history"]), 1)
        self.assertEqual(state["history"][0]["state"], "completed")

    def test_check_dry_run_makes_no_network_writes(self):
        write_json(self.state_path, self._active_canary_state())
        self.api.logs_by_mac["3c:0f:02:c7:eb:cc"] = {
            "firmwareVersion": "3.21.0", "createdAt": "2026-06-13T13:00:00+00:00"}
        rc, out = self.run_check(["--state", self.state_path])  # no --execute
        self.assertEqual(rc, 0)
        self.assertEqual(self.api.posts, [])


if __name__ == "__main__":
    unittest.main()
