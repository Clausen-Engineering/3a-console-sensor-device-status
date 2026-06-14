"""Tests for scripts/check_fleet_alerts.py — pure rule function and gh I/O helpers.

Run:
    python -m unittest tests.test_fleet_alerts -v
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

# Make the scripts/ directory importable.
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import check_fleet_alerts as cfa

# ---------------------------------------------------------------------------
# Helpers for building minimal fixture data
# ---------------------------------------------------------------------------

def _now():
    return datetime(2026, 6, 13, 12, 0, 0, tzinfo=timezone.utc)


def _device(mac="aa:bb:cc:dd:ee:ff", last_seen=None, status="Up to date",
            declared_deployment_version="v3.21.0", deployment_environment="",
            target_version="v3.21.0", version="v3.21.0"):
    """Return a minimal dashboard device dict."""
    return {
        "mac": mac,
        "name": f"Test Device ({mac})",
        "last_seen": last_seen,
        "declared_deployment_version": declared_deployment_version,
        "deployment_environment": deployment_environment,
        "target_version": target_version,
        "version": version,
        # status is not stored in JSON — it's computed; we inject it via
        # the _compute_status helper in the script.
    }


def _rollout_device(mac, state, updated_at):
    return {"mac": mac, "state": state, "updated_at": updated_at, "label": "Test"}


def _rollout_active(devices):
    return {"active": {"devices": devices, "version": "v3.21.0"}, "history": []}


def _version_changes(versions):
    """versions: list of (version_str, date_str) tuples."""
    return {
        "version_changes": [
            {"version": v, "date": d} for v, d in versions
        ]
    }


# ---------------------------------------------------------------------------
# Rule 1 — Silent device (last_seen older than 24h)
# ---------------------------------------------------------------------------

class TestSilentRule(unittest.TestCase):

    def _run(self, last_seen_offset, status="Up to date"):
        now = _now()
        last_seen_dt = now + timedelta(hours=last_seen_offset)
        last_seen_str = last_seen_dt.isoformat()
        device = _device(last_seen=last_seen_str, status=status)
        # Inject computed status so the helper can read it.
        device["_status"] = status
        dashboard = {"sections": [{"devices": [device]}]}
        alerts = cfa.evaluate_alerts(dashboard, {"active": None, "history": []}, _version_changes([]), now)
        return [a for a in alerts if a.rule == "silent"]

    def test_silent_at_23h59m_no_alert(self):
        """23h59m ago = not yet 24h → no silent alert."""
        alerts = self._run(-23 - 59/60)
        self.assertEqual(alerts, [])

    def test_silent_at_24h01m_fires(self):
        """24h01m ago = over 24h → silent alert fires."""
        alerts = self._run(-24 - 1/60)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].rule, "silent")
        self.assertIn("aa:bb:cc:dd:ee:ff", alerts[0].mac)

    def test_silent_boundary_exactly_24h_no_alert(self):
        """Exactly 24h = not strictly greater than → no alert (boundary is exclusive)."""
        alerts = self._run(-24)
        self.assertEqual(alerts, [])

    def test_silent_null_last_seen_no_alert(self):
        """last_seen is null → no silent alert (we don't know when last seen)."""
        device = _device(last_seen=None)
        device["_status"] = "Up to date"
        dashboard = {"sections": [{"devices": [device]}]}
        alerts = cfa.evaluate_alerts(dashboard, {"active": None, "history": []}, _version_changes([]), _now())
        silent = [a for a in alerts if a.rule == "silent"]
        self.assertEqual(silent, [])

    def test_silent_exemption_in_development(self):
        """Devices with status 'In development' must be exempt from silent rule."""
        alerts = self._run(-48, status="In development")
        self.assertEqual(alerts, [])

    def test_silent_exemption_not_deployed(self):
        """Devices with status 'Not deployed' must be exempt from silent rule."""
        alerts = self._run(-48, status="Not deployed")
        self.assertEqual(alerts, [])

    def test_silent_exemption_unknown(self):
        """Devices with status 'Unknown' should not be silenced (not in exempt set)."""
        # Unknown is not in {In development, Not deployed}, so still alerts.
        alerts = self._run(-48, status="Unknown")
        self.assertEqual(len(alerts), 1)


# ---------------------------------------------------------------------------
# Rule 2 — Behind (Needs update AND release older than 7 days)
# ---------------------------------------------------------------------------

class TestBehindRule(unittest.TestCase):

    def _run(self, device_version, target_version, release_date_offset_days, status="Needs update"):
        now = _now()
        release_date = (now + timedelta(days=release_date_offset_days)).strftime("%Y-%m-%d")
        device = _device(
            mac="11:22:33:44:55:66",
            version=device_version,
            target_version=target_version,
            last_seen=None,
        )
        device["_status"] = status
        dashboard = {"sections": [{"devices": [device]}]}
        vc = _version_changes([(target_version, release_date)])
        alerts = cfa.evaluate_alerts(dashboard, {"active": None, "history": []}, vc, now)
        return [a for a in alerts if a.rule == "behind"]

    def test_behind_fresh_release_no_alert(self):
        """Release only 3 days old → no behind alert even if Needs update."""
        alerts = self._run("v3.19.0", "v3.21.0", release_date_offset_days=-3)
        self.assertEqual(alerts, [])

    def test_behind_exactly_7_days_no_alert(self):
        """Release exactly 7 days old → boundary is exclusive, no alert."""
        alerts = self._run("v3.19.0", "v3.21.0", release_date_offset_days=-7)
        self.assertEqual(alerts, [])

    def test_behind_stale_release_fires(self):
        """Release 8 days old AND Needs update → behind alert fires."""
        alerts = self._run("v3.19.0", "v3.21.0", release_date_offset_days=-8)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].rule, "behind")

    def test_behind_up_to_date_no_alert(self):
        """Device up to date → no behind alert even if release is old."""
        alerts = self._run("v3.21.0", "v3.21.0", release_date_offset_days=-30, status="Up to date")
        self.assertEqual(alerts, [])

    def test_behind_patch_available_no_alert(self):
        """'Patch available' is not 'Needs update' → no behind alert."""
        alerts = self._run("v3.21.0", "v3.21.1", release_date_offset_days=-30, status="Patch available")
        self.assertEqual(alerts, [])

    def test_behind_unknown_release_date_no_alert(self):
        """Target version not in version_changes → can't determine age → no alert."""
        now = _now()
        device = _device(mac="11:22:33:44:55:66", version="v3.19.0", target_version="v9.99.0")
        device["_status"] = "Needs update"
        dashboard = {"sections": [{"devices": [device]}]}
        vc = _version_changes([("v3.21.0", "2026-01-01")])  # v9.99.0 not present
        alerts = cfa.evaluate_alerts(dashboard, {"active": None, "history": []}, vc, now)
        behind = [a for a in alerts if a.rule == "behind"]
        self.assertEqual(behind, [])


# ---------------------------------------------------------------------------
# Rule 3 — Rollout stalled (offered/canary > 48h)
# ---------------------------------------------------------------------------

class TestStalledRolloutRule(unittest.TestCase):

    def _run(self, state, hours_ago):
        now = _now()
        updated_at = (now - timedelta(hours=hours_ago)).isoformat()
        dev = _rollout_device("aa:bb:cc:dd:ee:ff", state=state, updated_at=updated_at)
        rollout = _rollout_active([dev])
        dashboard = {"sections": []}
        alerts = cfa.evaluate_alerts(dashboard, rollout, _version_changes([]), now)
        return [a for a in alerts if a.rule == "rollout-stalled"]

    def test_stalled_canary_under_48h_no_alert(self):
        """Canary in 'canary' state for 47h → no stalled alert."""
        alerts = self._run("canary", hours_ago=47)
        self.assertEqual(alerts, [])

    def test_stalled_canary_exactly_48h_no_alert(self):
        """Exactly 48h → boundary is exclusive, no alert."""
        alerts = self._run("canary", hours_ago=48)
        self.assertEqual(alerts, [])

    def test_stalled_canary_over_48h_fires(self):
        """Canary state for 49h → stalled alert."""
        alerts = self._run("canary", hours_ago=49)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].rule, "rollout-stalled")

    def test_stalled_offered_over_48h_fires(self):
        """Offered state for 50h → stalled alert."""
        alerts = self._run("offered", hours_ago=50)
        self.assertEqual(len(alerts), 1)

    def test_stalled_updated_state_no_alert(self):
        """Device in 'updated' state (not offered/canary) → no stalled alert."""
        alerts = self._run("updated", hours_ago=100)
        self.assertEqual(alerts, [])

    def test_stalled_pending_state_no_alert(self):
        """Device in 'pending' state → not yet offered, no stalled alert."""
        alerts = self._run("pending", hours_ago=100)
        self.assertEqual(alerts, [])

    def test_no_active_rollout_no_alert(self):
        """No active rollout → no rollout-stalled alerts."""
        dashboard = {"sections": []}
        rollout = {"active": None, "history": []}
        alerts = cfa.evaluate_alerts(dashboard, rollout, _version_changes([]), _now())
        stalled = [a for a in alerts if a.rule == "rollout-stalled"]
        self.assertEqual(stalled, [])


# ---------------------------------------------------------------------------
# Alert identity and deep-link encoding
# ---------------------------------------------------------------------------

class TestAlertIdentity(unittest.TestCase):

    def test_alert_has_title_with_mac_and_rule_slug(self):
        """Alert title must include the label, mac, and rule slug."""
        now = _now()
        last_seen = (now - timedelta(hours=25)).isoformat()
        device = _device(mac="3c:0f:02:c7:eb:cc", last_seen=last_seen)
        device["_status"] = "Up to date"
        device["name"] = "Floating Platform Motion"
        dashboard = {"sections": [{"devices": [device]}]}
        alerts = cfa.evaluate_alerts(dashboard, {"active": None, "history": []}, _version_changes([]), now)
        silent = [a for a in alerts if a.rule == "silent"]
        self.assertEqual(len(silent), 1)
        alert = silent[0]
        self.assertIn("[fleet-alert]", alert.title)
        self.assertIn("3c:0f:02:c7:eb:cc", alert.title)
        self.assertIn("silent", alert.title)

    def test_deep_link_mac_encoding(self):
        """Deep link must encode MAC as hex-only lowercase (colons stripped)."""
        now = _now()
        last_seen = (now - timedelta(hours=25)).isoformat()
        device = _device(mac="3c:0f:02:c7:eb:cc", last_seen=last_seen)
        device["_status"] = "Up to date"
        dashboard = {"sections": [{"devices": [device]}]}
        alerts = cfa.evaluate_alerts(dashboard, {"active": None, "history": []}, _version_changes([]), now)
        silent = [a for a in alerts if a.rule == "silent"]
        self.assertEqual(len(silent), 1)
        self.assertIn("#device=3c0f02c7ebcc", silent[0].body)

    def test_deep_link_uppercase_mac_normalised(self):
        """MAC stored as uppercase (e.g. 'E0:72:A1:F5:0D:CC') → hex lowercase."""
        now = _now()
        last_seen = (now - timedelta(hours=25)).isoformat()
        device = _device(mac="E0:72:A1:F5:0D:CC", last_seen=last_seen)
        device["_status"] = "Up to date"
        dashboard = {"sections": [{"devices": [device]}]}
        alerts = cfa.evaluate_alerts(dashboard, {"active": None, "history": []}, _version_changes([]), now)
        silent = [a for a in alerts if a.rule == "silent"]
        self.assertEqual(len(silent), 1)
        self.assertIn("#device=e072a1f50dcc", silent[0].body)


# ---------------------------------------------------------------------------
# GH I/O command construction — only argv is tested, gh is never executed
# ---------------------------------------------------------------------------

class TestGhCommandConstruction(unittest.TestCase):

    def test_build_create_command_contains_label(self):
        """gh issue create command must include --label fleet-alert."""
        cmd = cfa.build_create_command(
            repo="owner/repo",
            title="[fleet-alert] silent (aa:bb:cc:dd:ee:ff): silent",
            body="Some body",
            label="fleet-alert",
        )
        self.assertIn("gh", cmd[0])
        self.assertIn("--label", cmd)
        idx = cmd.index("--label")
        self.assertEqual(cmd[idx + 1], "fleet-alert")

    def test_build_create_command_title_and_body(self):
        """gh issue create command includes --title and --body."""
        cmd = cfa.build_create_command(
            repo="owner/repo",
            title="My title",
            body="My body",
            label="fleet-alert",
        )
        self.assertIn("--title", cmd)
        self.assertIn("My title", cmd)
        self.assertIn("--body", cmd)
        self.assertIn("My body", cmd)

    def test_build_close_command_includes_number(self):
        """gh issue close command must include the issue number."""
        cmd = cfa.build_close_command(repo="owner/repo", issue_number=42, comment="No longer firing.")
        self.assertIn("42", cmd)
        self.assertIn("--comment", cmd)

    def test_build_list_command_includes_label(self):
        """gh issue list command filters by label."""
        cmd = cfa.build_list_command(repo="owner/repo", label="fleet-alert")
        self.assertIn("--label", cmd)
        self.assertIn("fleet-alert", cmd)

    @patch("subprocess.run")
    def test_list_open_issues_captures_argv_not_executed(self, mock_run):
        """list_open_issues calls subprocess.run with gh argv; mock means gh is never run."""
        mock_run.return_value = MagicMock(returncode=0, stdout='[]')
        result = cfa.list_open_issues(repo="owner/repo", label="fleet-alert")
        self.assertTrue(mock_run.called)
        # Ensure we captured argv (list) — not a shell string
        call_args = mock_run.call_args
        argv = call_args[0][0]
        self.assertIsInstance(argv, list)
        # gh must appear as first element
        self.assertEqual(argv[0], "gh")

    @patch("subprocess.run")
    def test_create_issue_captures_argv(self, mock_run):
        """create_issue calls subprocess.run but does not actually create a GH issue."""
        mock_run.return_value = MagicMock(returncode=0, stdout="https://github.com/owner/repo/issues/1\n")
        cfa.create_issue(
            repo="owner/repo",
            title="[fleet-alert] silent (aa:bb): silent",
            body="body",
            label="fleet-alert",
        )
        self.assertTrue(mock_run.called)
        argv = mock_run.call_args[0][0]
        self.assertEqual(argv[0], "gh")
        self.assertIn("create", argv)

    @patch("subprocess.run")
    def test_close_issue_captures_argv(self, mock_run):
        """close_issue calls subprocess.run but does not actually close a GH issue."""
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        cfa.close_issue(repo="owner/repo", issue_number=7, comment="Resolved.")
        self.assertTrue(mock_run.called)
        argv = mock_run.call_args[0][0]
        self.assertEqual(argv[0], "gh")
        self.assertIn("close", argv)


# ---------------------------------------------------------------------------
# Determinism — now must be injected, not datetime.now()
# ---------------------------------------------------------------------------

class TestNowInjection(unittest.TestCase):

    def test_evaluate_alerts_accepts_now_parameter(self):
        """evaluate_alerts must accept an explicit 'now' datetime (no datetime.now call)."""
        import inspect
        sig = inspect.signature(cfa.evaluate_alerts)
        self.assertIn("now", sig.parameters)

    def test_evaluate_alerts_with_past_now(self):
        """Passing a past 'now' shifts all thresholds — proves now is used internally."""
        # Device last seen at a fixed point
        fixed_last_seen = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        device = _device(last_seen=fixed_last_seen.isoformat())
        device["_status"] = "Up to date"
        dashboard = {"sections": [{"devices": [device]}]}

        # now = 23h after last_seen → no alert
        now_no_alert = fixed_last_seen + timedelta(hours=23)
        alerts_no = cfa.evaluate_alerts(dashboard, {"active": None, "history": []}, _version_changes([]), now_no_alert)
        self.assertEqual([a for a in alerts_no if a.rule == "silent"], [])

        # now = 25h after last_seen → alert
        now_alert = fixed_last_seen + timedelta(hours=25)
        alerts_yes = cfa.evaluate_alerts(dashboard, {"active": None, "history": []}, _version_changes([]), now_alert)
        self.assertEqual(len([a for a in alerts_yes if a.rule == "silent"]), 1)


# ---------------------------------------------------------------------------
# compute_status helper (mirrors frontend logic)
# ---------------------------------------------------------------------------

class TestComputeStatus(unittest.TestCase):

    def test_in_development(self):
        d = _device()
        d["deployment_environment"] = "development"
        self.assertEqual(cfa.compute_status(d), "In development")

    def test_not_deployed_no_record(self):
        d = {
            "declared_deployment_version": "not-deployed",
            "last_deployed": None,
            "initial_deployed": None,
            "mac": None,
            "version": "",
            "target_version": "v3.21.0",
            "deployment_environment": "",
        }
        self.assertEqual(cfa.compute_status(d), "Not deployed")

    def test_needs_update_minor(self):
        d = _device(version="v3.19.0", target_version="v3.21.0")
        self.assertEqual(cfa.compute_status(d), "Needs update")

    def test_needs_update_major(self):
        d = _device(version="v2.16.1", target_version="v3.21.0")
        self.assertEqual(cfa.compute_status(d), "Needs update")

    def test_patch_available(self):
        d = _device(version="v3.21.0", target_version="v3.21.1")
        self.assertEqual(cfa.compute_status(d), "Patch available")

    def test_up_to_date(self):
        d = _device(version="v3.21.0", target_version="v3.21.0")
        self.assertEqual(cfa.compute_status(d), "Up to date")

    def test_unknown_no_version(self):
        d = _device(version="", target_version="v3.21.0")
        self.assertEqual(cfa.compute_status(d), "Unknown")


if __name__ == "__main__":
    unittest.main()
