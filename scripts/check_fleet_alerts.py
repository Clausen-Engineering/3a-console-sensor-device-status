#!/usr/bin/env python3
"""Fleet alert checker.

Reads data/dashboard-data.json, data/rollout-state.json, and
data/version-changes.json (all committed JSON — no API calls) and evaluates
three alert rules:

  1. silent       — device last_seen non-null and older than 24 h,
                    status not in {In development, Not deployed}.
  2. behind       — status "Needs update" AND the target release's date in
                    version-changes.json is older than 7 days.
  3. rollout-stalled — active-rollout device in offered/canary for > 48 h
                       (from its updated_at).

Issue lifecycle (one issue per device+rule):
  - Existing open issue → no-op (no comment spam).
  - Rule no longer firing → close with a one-line comment.
  - New firing → create with facts + deep-link.

Dry-run is the default; pass --execute to mutate GitHub issues.

Usage:
    python scripts/check_fleet_alerts.py [--dry-run | --execute]

Environment:
    GITHUB_REPOSITORY  — e.g. "owner/repo"  (required for --execute)
    GH_TOKEN           — GitHub token with issues:write  (required for --execute)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DASHBOARD_PATH = DATA_DIR / "dashboard-data.json"
ROLLOUT_PATH = DATA_DIR / "rollout-state.json"
VERSION_CHANGES_PATH = DATA_DIR / "version-changes.json"

DASHBOARD_BASE_URL = "https://clausen-engineering.github.io/3a-console-sensor-device-status"
ISSUE_LABEL = "fleet-alert"

SILENT_THRESHOLD_H = 24
BEHIND_THRESHOLD_DAYS = 7
STALLED_THRESHOLD_H = 48

SILENT_EXEMPT_STATUSES = {"In development", "Not deployed"}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Alert:
    mac: str
    rule: str          # "silent" | "behind" | "rollout-stalled"
    label: str         # human-readable device name
    title: str
    body: str


# ---------------------------------------------------------------------------
# Status computation (mirrors index.html getUpdateStatus / getLifecycleStatus)
# ---------------------------------------------------------------------------

_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def _parse_version(version_str: str) -> Optional[tuple[int, int, int]]:
    """Return (major, minor, patch) or None if unparseable."""
    if not version_str:
        return None
    m = _VERSION_RE.match(version_str.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def _has_value(v: Any) -> bool:
    """True when v is a non-empty, non-null string."""
    return bool(v and str(v).strip())


def compute_status(device: dict[str, Any]) -> str:
    """Compute the display status for a device, mirroring the frontend logic.

    Order:
      1. deployment_environment == "development" → "In development"
      2. declared_deployment_version in sentinel list AND no deployment record
         → "Not deployed"
      3. no version → "Unknown"
      4. compare version vs target_version → "Needs update" / "Patch available"
         / "Up to date" / "Unknown"
    """
    dep_env = str(device.get("deployment_environment") or "").strip().lower()
    if dep_env == "development":
        return "In development"

    declared = str(device.get("declared_deployment_version") or "").strip().lower()
    has_dep_record = any(
        _has_value(device.get(k))
        for k in ("last_deployed", "initial_deployed", "mac")
    )
    if declared in {"0.0.0", "v0.0.0", "not-deployed", "vnot-deployed"}:
        return "Unknown" if has_dep_record else "Not deployed"

    version = str(device.get("version") or "").strip()
    target = str(device.get("target_version") or "").strip()

    if not version:
        return "Unknown"

    pv = _parse_version(version)
    pt = _parse_version(target)
    if not pv or not pt:
        return "Unknown"

    if pv[0] < pt[0]:
        return "Needs update"
    if pv[0] == pt[0] and pv[1] < pt[1]:
        return "Needs update"
    if pv[0] == pt[0] and pv[1] == pt[1] and pv[2] < pt[2]:
        return "Patch available"
    return "Up to date"


# ---------------------------------------------------------------------------
# Deep-link helper
# ---------------------------------------------------------------------------

def mac_to_hex(mac: str) -> str:
    """Convert a MAC address to the hex string used in the #device= deep link.

    Mirrors the frontend getDeviceHashKey():
        mac.toLowerCase().replace(/[^a-f0-9]/g, "")
    Example: "3C:0F:02:C7:EB:CC" → "3c0f02c7ebcc"
    """
    return re.sub(r"[^a-f0-9]", "", mac.lower())


def device_deep_link(mac: str) -> str:
    return f"{DASHBOARD_BASE_URL}/#device={mac_to_hex(mac)}"


# ---------------------------------------------------------------------------
# ISO datetime parser (tolerant of various timezone suffixes)
# ---------------------------------------------------------------------------

def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    ts = str(ts).strip()
    # Python 3.7+ fromisoformat doesn't handle trailing 'Z'.
    ts = ts.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    # Ensure timezone-aware.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_date_only(date_str: str) -> Optional[datetime]:
    """Parse a 'YYYY-MM-DD' string into a UTC midnight datetime."""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Pure rule evaluation
# ---------------------------------------------------------------------------

def evaluate_alerts(
    dashboard: dict[str, Any],
    rollout: dict[str, Any],
    version_changes: dict[str, Any],
    now: datetime,
) -> list[Alert]:
    """Evaluate all alert rules and return a list of Alert objects.

    Parameters
    ----------
    dashboard:       parsed dashboard-data.json
    rollout:         parsed rollout-state.json
    version_changes: parsed version-changes.json
    now:             current datetime (timezone-aware); injected for determinism.
    """
    alerts: list[Alert] = []

    # Build a version → release-date lookup from version_changes.json.
    # The top-level structure has a "version_changes" list.
    vc_list = version_changes.get("version_changes", [])
    release_date_map: dict[str, datetime] = {}
    for entry in vc_list:
        v = str(entry.get("version") or "").strip()
        d = str(entry.get("date") or "").strip()
        dt = _parse_date_only(d)
        if v and dt:
            release_date_map[v] = dt

    # ---- Rule 1 & 2: iterate devices from all sections --------------------
    for section in dashboard.get("sections", []):
        for device in section.get("devices", []):
            # Compute status (mirrors frontend; _status injection used in tests).
            status = device.get("_status") or compute_status(device)
            mac = str(device.get("mac") or "").strip()
            name = str(device.get("name") or mac).strip()

            # -- Rule 1: Silent ------------------------------------------------
            last_seen_str = device.get("last_seen")
            if last_seen_str:
                last_seen_dt = _parse_iso(last_seen_str)
                if last_seen_dt is not None and status not in SILENT_EXEMPT_STATUSES:
                    elapsed = now - last_seen_dt
                    if elapsed > timedelta(hours=SILENT_THRESHOLD_H):
                        last_seen_fmt = last_seen_dt.strftime("%Y-%m-%d %H:%M UTC")
                        elapsed_h = int(elapsed.total_seconds() // 3600)
                        link = device_deep_link(mac)
                        alerts.append(Alert(
                            mac=mac,
                            rule="silent",
                            label=name,
                            title=f"[fleet-alert] {name} ({mac}): silent",
                            body=(
                                f"**Device:** {name}  \n"
                                f"**MAC:** `{mac}`  \n"
                                f"**Rule:** silent — last seen {elapsed_h}h ago "
                                f"(at {last_seen_fmt}), threshold {SILENT_THRESHOLD_H}h  \n"
                                f"**Status:** {status}  \n"
                                f"**Dashboard:** {link}  \n"
                            ),
                        ))

            # -- Rule 2: Behind ------------------------------------------------
            if status == "Needs update":
                target_version = str(device.get("target_version") or "").strip()
                release_dt = release_date_map.get(target_version)
                if release_dt is not None:
                    age = now - release_dt
                    if age.days > BEHIND_THRESHOLD_DAYS:
                        age_days = age.days  # calendar days since release date (day granularity)
                        link = device_deep_link(mac)
                        alerts.append(Alert(
                            mac=mac,
                            rule="behind",
                            label=name,
                            title=f"[fleet-alert] {name} ({mac}): behind",
                            body=(
                                f"**Device:** {name}  \n"
                                f"**MAC:** `{mac}`  \n"
                                f"**Rule:** behind — status '{status}', target release "
                                f"`{target_version}` is {age_days} days old "
                                f"(threshold {BEHIND_THRESHOLD_DAYS} days)  \n"
                                f"**Dashboard:** {link}  \n"
                            ),
                        ))

    # ---- Rule 3: Rollout stalled -----------------------------------------
    active = rollout.get("active")
    if active:
        rollout_version = str(active.get("version") or "").strip()
        for dev in active.get("devices", []):
            state = str(dev.get("state") or "").strip()
            if state not in ("offered", "canary"):
                continue
            mac = str(dev.get("mac") or "").strip()
            label = str(dev.get("label") or mac).strip()
            updated_at_str = dev.get("updated_at")
            updated_at = _parse_iso(updated_at_str)
            if updated_at is None:
                continue
            elapsed = now - updated_at
            if elapsed > timedelta(hours=STALLED_THRESHOLD_H):
                elapsed_h = int(elapsed.total_seconds() // 3600)
                link = device_deep_link(mac)
                alerts.append(Alert(
                    mac=mac,
                    rule="rollout-stalled",
                    label=label,
                    title=f"[fleet-alert] {label} ({mac}): rollout-stalled",
                    body=(
                        f"**Device:** {label}  \n"
                        f"**MAC:** `{mac}`  \n"
                        f"**Rule:** rollout-stalled — device has been in state "
                        f"'{state}' for rollout `{rollout_version}` for {elapsed_h}h "
                        f"(threshold {STALLED_THRESHOLD_H}h)  \n"
                        f"**Dashboard:** {link}  \n"
                    ),
                ))

    return alerts


# ---------------------------------------------------------------------------
# gh CLI helpers (thin wrappers; only argv construction + subprocess.run)
# ---------------------------------------------------------------------------

def build_list_command(repo: str, label: str) -> list[str]:
    return [
        "gh", "issue", "list",
        "--repo", repo,
        "--label", label,
        "--state", "open",
        "--json", "number,title",
        "--limit", "200",
    ]


def build_create_command(repo: str, title: str, body: str, label: str) -> list[str]:
    return [
        "gh", "issue", "create",
        "--repo", repo,
        "--title", title,
        "--body", body,
        "--label", label,
    ]


def build_close_command(repo: str, issue_number: int, comment: str) -> list[str]:
    return [
        "gh", "issue", "close",
        str(issue_number),
        "--repo", repo,
        "--comment", comment,
    ]


def list_open_issues(repo: str, label: str) -> list[dict[str, Any]]:
    """Return open issues with the given label as a list of {number, title} dicts."""
    cmd = build_list_command(repo=repo, label=label)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[warn] gh issue list failed: {result.stderr.strip()}", file=sys.stderr)
        return []
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return []


def create_issue(repo: str, title: str, body: str, label: str) -> None:
    """Create a GitHub issue."""
    cmd = build_create_command(repo=repo, title=title, body=body, label=label)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[warn] gh issue create failed: {result.stderr.strip()}", file=sys.stderr)
    else:
        print(f"  created: {result.stdout.strip()}")


def close_issue(repo: str, issue_number: int, comment: str) -> None:
    """Close a GitHub issue with a comment."""
    cmd = build_close_command(repo=repo, issue_number=issue_number, comment=comment)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[warn] gh issue close #{issue_number} failed: {result.stderr.strip()}", file=sys.stderr)
    else:
        print(f"  closed: #{issue_number}")


def ensure_label_exists(repo: str) -> None:
    """Create the fleet-alert label if it doesn't exist yet."""
    check = subprocess.run(
        ["gh", "label", "list", "--repo", repo, "--json", "name"],
        capture_output=True, text=True,
    )
    if check.returncode == 0:
        try:
            names = [e["name"] for e in json.loads(check.stdout)]
            if ISSUE_LABEL in names:
                return
        except (json.JSONDecodeError, KeyError):
            pass
    # Create label.
    subprocess.run(
        ["gh", "label", "create", ISSUE_LABEL,
         "--repo", repo,
         "--color", "D93F0B",
         "--description", "Automated fleet health alert"],
        capture_output=True, text=True,
    )


# ---------------------------------------------------------------------------
# Issue key extraction
# ---------------------------------------------------------------------------

_TITLE_PREFIX = "[fleet-alert]"


def _title_key(title: str) -> str:
    """Return the identifying suffix from a fleet-alert issue title.

    Example: "[fleet-alert] Foo Bar (aa:bb:cc): silent" → "Foo Bar (aa:bb:cc): silent"
    """
    if title.startswith(_TITLE_PREFIX):
        return title[len(_TITLE_PREFIX):].strip()
    return title.strip()


def _alert_key(alert: Alert) -> str:
    """Canonical key for matching an alert to an open issue."""
    return _title_key(alert.title)


# ---------------------------------------------------------------------------
# Main reconciliation loop
# ---------------------------------------------------------------------------

def reconcile(alerts: list[Alert], repo: str, dry_run: bool) -> None:
    """Compare firing alerts to open issues; create/close as needed."""
    open_issues = list_open_issues(repo=repo, label=ISSUE_LABEL) if not dry_run else []

    # Map from title-key → issue number for open issues.
    open_by_key: dict[str, int] = {}
    for issue in open_issues:
        key = _title_key(str(issue.get("title") or ""))
        if key:
            open_by_key[key] = int(issue["number"])

    firing_keys = {_alert_key(a) for a in alerts}

    # --- Create new issues -----------------------------------------------
    for alert in alerts:
        key = _alert_key(alert)
        if key in open_by_key:
            print(f"[skip]   already open — {alert.title}")
            continue
        if dry_run:
            print(f"[create] {alert.title}")
        else:
            print(f"[create] {alert.title}")
            create_issue(repo=repo, title=alert.title, body=alert.body, label=ISSUE_LABEL)

    # --- Close resolved issues -------------------------------------------
    for key, number in open_by_key.items():
        if key not in firing_keys:
            if dry_run:
                print(f"[close]  #{number} — {key} (no longer firing)")
            else:
                print(f"[close]  #{number} — {key} (no longer firing)")
                close_issue(
                    repo=repo,
                    issue_number=number,
                    comment="Alert condition no longer detected; closing automatically.",
                )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def load_json_file(path: Path) -> dict[str, Any]:
    """Load a JSON file; return empty dict on missing / parse error."""
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return {}
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check fleet alert rules and manage GitHub issues."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="dry_run", action="store_true", default=True,
                      help="Print planned actions without executing (default).")
    mode.add_argument("--execute", dest="dry_run", action="store_false",
                      help="Execute creates/closes against GitHub.")
    args = parser.parse_args(argv)

    dry_run: bool = args.dry_run

    # Load data files.
    dashboard = load_json_file(DASHBOARD_PATH)
    rollout = load_json_file(ROLLOUT_PATH)
    if not rollout:
        rollout = {"active": None, "history": []}
    version_changes = load_json_file(VERSION_CHANGES_PATH)

    now = datetime.now(tz=timezone.utc)

    alerts = evaluate_alerts(dashboard, rollout, version_changes, now)

    mode_str = "DRY RUN" if dry_run else "EXECUTE"
    print(f"[fleet-alerts] {mode_str} — {len(alerts)} alert(s) firing")
    for alert in alerts:
        print(f"  * [{alert.rule}] {alert.mac}")

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        if not dry_run:
            print("[error] GITHUB_REPOSITORY env var not set", file=sys.stderr)
            return 1
        print("[info] GITHUB_REPOSITORY not set; skipping issue reconciliation (dry-run only)")
        return 0

    if not dry_run:
        ensure_label_exists(repo)

    reconcile(alerts=alerts, repo=repo, dry_run=dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
