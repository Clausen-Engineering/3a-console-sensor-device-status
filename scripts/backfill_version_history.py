#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
VERSION_HISTORY_PATH = ROOT / "data" / "version-history.json"
DASHBOARD_DATA_GIT_PATH = "data/dashboard-data.json"
VERSION_HISTORY_RETENTION = 730


def safe_string(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def normalize_version(raw_version: Any) -> str:
    version = safe_string(raw_version)
    if not version:
        return ""
    return f"v{version.removeprefix('v')}"


def load_json_if_present(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def run_git(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def get_last_dashboard_commit_by_day() -> dict[str, str]:
    result = run_git(["log", "--format=%H %cs", "--", DASHBOARD_DATA_GIT_PATH])
    if result.returncode != 0:
        print(result.stderr.strip(), file=sys.stderr)
        return {}

    commits_by_day: dict[str, str] = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        commit_hash, commit_date = parts
        if commit_date not in commits_by_day:
            commits_by_day[commit_date] = commit_hash

    return commits_by_day


def get_dashboard_payload(commit_hash: str) -> dict[str, Any] | None:
    result = run_git(["show", f"{commit_hash}:{DASHBOARD_DATA_GIT_PATH}"])
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def iter_dashboard_sections(payload: dict[str, Any]) -> list[dict[str, Any]]:
    sections = payload.get("sections")
    if isinstance(sections, list) and sections:
        return [section for section in sections if isinstance(section, dict)]

    return [
        {
            "id": safe_string(payload.get("default_section")) or "sensor-hub",
            "devices": payload.get("devices", []) or [],
        }
    ]


def count_versions_by_section(payload: dict[str, Any]) -> dict[str, dict[str, int]]:
    section_counts: dict[str, dict[str, int]] = {}
    for section in iter_dashboard_sections(payload):
        section_id = safe_string(section.get("id"))
        if not section_id:
            continue

        version_counts: dict[str, int] = {}
        devices = section.get("devices", []) or []
        if not isinstance(devices, list):
            devices = []
        for device in devices:
            if not isinstance(device, dict):
                continue
            version = normalize_version(device.get("version"))
            version_counts[version] = version_counts.get(version, 0) + 1

        section_counts[section_id] = dict(sorted(version_counts.items()))

    return section_counts


def normalize_history(data: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
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
                normalized_counts[normalize_version(version)] = numeric_count

            normalized_sections[normalized_section_id] = dict(sorted(normalized_counts.items()))

        normalized_snapshots.append(
            {
                "date": date,
                "sections": dict(sorted(normalized_sections.items())),
            }
        )

    normalized_snapshots.sort(key=lambda item: item["date"])
    return {"snapshots": normalized_snapshots[-VERSION_HISTORY_RETENTION:]}


def main() -> int:
    commits_by_day = get_last_dashboard_commit_by_day()
    if not commits_by_day:
        print(f"No git history found for {DASHBOARD_DATA_GIT_PATH}; version history will start with the next pipeline run.")
        return 0

    history = normalize_history(load_json_if_present(VERSION_HISTORY_PATH))
    snapshots_by_date = {
        snapshot["date"]: snapshot
        for snapshot in history["snapshots"]
        if isinstance(snapshot, dict) and safe_string(snapshot.get("date"))
    }

    imported = 0
    for commit_date in sorted(commits_by_day):
        payload = get_dashboard_payload(commits_by_day[commit_date])
        if payload is None:
            continue
        snapshots_by_date[commit_date] = {
            "date": commit_date,
            "sections": count_versions_by_section(payload),
        }
        imported += 1

    snapshots = [snapshots_by_date[date] for date in sorted(snapshots_by_date)]
    history = {"snapshots": snapshots[-VERSION_HISTORY_RETENTION:]}
    VERSION_HISTORY_PATH.write_text(json.dumps(history, indent=2), encoding="utf-8")
    print(f"Wrote {VERSION_HISTORY_PATH} with {len(history['snapshots'])} snapshots ({imported} imported from git history).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
