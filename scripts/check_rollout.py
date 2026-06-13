#!/usr/bin/env python3
"""Scheduled monitor that advances an active firmware rollout.

Behavior (see plan Task 6):
  - No active rollout -> exit 0 silently.
  - For each `canary`/`offered` device, fetch GET /devices/{mac}/logs/latest;
    reported version >= target -> state `updated`.
  - Canary advance: canary `updated` AND its log createdAt is AFTER the canary
    record's updated_at (post-update heartbeat = healthy) -> create records for
    all `pending` devices, moving them to `offered`.
  - Canary deadline exceeded (created_at + canary_deadline_h) without success
    -> canary `failed`, rollout state `halted`, NEVER fans out.
  - All devices `updated` -> archive `active` to `history` (state `completed`).
  - Always rewrite the state file (the workflow commits only on diff).
  - --dry-run is the DEFAULT; --execute required to mutate the API.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import rollout_common as rc

# Module-level network helpers (tests monkeypatch these).
fetch_json = rc.fetch_json
post_json = rc.post_json
build_auth = rc.build_auth

API_BASE = rc.API_BASE
STATE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "data", "rollout-state.json")


def _sync_helpers():
    rc.fetch_json = fetch_json
    rc.post_json = post_json
    rc.build_auth = build_auth


def _reported_code(api_base, mac, headers):
    """Return (versionCode_int_or_None, log_createdAt_datetime_or_None)."""
    log = rc.latest_log_for_mac(api_base, mac, headers)
    if not isinstance(log, dict):
        return None, None
    code = rc.version_code(log.get("firmwareVersion"))
    created = rc.parse_timestamp(log.get("createdAt"))
    return code, created


def do_check(args):
    _sync_helpers()
    state_path = args.state
    state = rc.load_state(state_path)
    active = state.get("active")

    if not active:
        # No active rollout -> exit silently.
        return 0

    now = rc.parse_timestamp(args.now) if args.now else rc.now_utc()
    target_code = active.get("version_code")
    if target_code is None:
        target_code = rc.version_code(active.get("version"))
    version = rc.normalize_version(active.get("version"))
    canary_mac = rc.normalize_mac(active.get("canary_mac")) if active.get("canary_mac") else ""
    deadline_h = active.get("canary_deadline_h") or 24

    headers = build_auth(API_BASE)
    devices = active.get("devices", [])
    by_mac = {d["mac"]: d for d in devices}

    print(f"Checking rollout {active.get('rollout_id')} ({version}, "
          f"versionCode {target_code}).")

    halted = active.get("state") == "halted"

    # 1) Refresh canary/offered devices from their latest logs.
    for dev in devices:
        if dev.get("state") not in ("canary", "offered"):
            continue
        code, created = _reported_code(API_BASE, dev["mac"], headers)
        if code is not None and target_code is not None and code >= target_code:
            if dev["state"] != "updated":
                dev["state"] = "updated"
                dev["updated_at"] = rc.iso_now()
                print(f"  {dev['mac']} ({dev.get('label')}) -> updated "
                      f"(reported >= target)")

    # 2) Canary lifecycle (only in canary mode and not already halted).
    if canary_mac and not halted:
        canary = by_mac.get(canary_mac)
        has_pending = any(d.get("state") == "pending" for d in devices)
        if canary is not None and has_pending:
            canary_record_at = rc.parse_timestamp(canary.get("updated_at"))
            _, canary_log_at = _reported_code(API_BASE, canary_mac, headers)
            healthy = (
                canary.get("state") == "updated"
                and canary_log_at is not None
                and canary_record_at is not None
                and canary_log_at > canary_record_at
            )
            created_at = rc.parse_timestamp(active.get("created_at"))
            deadline_passed = (
                created_at is not None
                and (now - created_at).total_seconds() > deadline_h * 3600
            )

            if healthy:
                # Fan out: create records for all pending -> offered.
                source = {
                    "fileUrl": active.get("source_file_url"),
                    "buildDate": "",
                    "id": active.get("source_firmware_id"),
                }
                if not rc.safe_string(source["fileUrl"]):
                    # Recover the source record from the API if not cached.
                    found = rc.find_source_firmware(API_BASE, target_code, headers)
                    if found:
                        source = found
                for dev in devices:
                    if dev.get("state") != "pending":
                        continue
                    uuid = rc.resolve_device_uuid(API_BASE, dev["mac"], headers)
                    if uuid is None:
                        dev["state"] = "skipped"
                        dev["reason"] = "could not resolve device UUID at fan-out"
                        dev["updated_at"] = rc.iso_now()
                        print(f"  {dev['mac']} skipped at fan-out (no UUID)")
                        continue
                    if args.execute:
                        record = rc.create_device_firmware_record(
                            API_BASE, source, version, target_code, uuid, headers)
                        rec_id = (rc.safe_string(record.get("id"))
                                  if isinstance(record, dict) else "")
                        dev["firmware_record_id"] = rec_id or None
                    dev["state"] = "offered"
                    dev["updated_at"] = rc.iso_now()
                    print(f"  FAN-OUT {dev['mac']} ({dev.get('label')}) -> offered")
            elif deadline_passed and canary.get("state") != "updated":
                # Canary deadline exceeded -> halt, NEVER fan out.
                canary["state"] = "failed"
                canary["reason"] = "canary deadline exceeded without update"
                canary["updated_at"] = rc.iso_now()
                active["state"] = "halted"
                halted = True
                print(f"  CANARY FAILED {canary_mac}: deadline exceeded -> "
                      f"rollout halted (no fan-out).")

    # 3) Completion: all non-skipped/non-failed devices updated -> archive.
    actionable = [d for d in devices if d.get("state") not in ("skipped", "failed")]
    all_done = bool(actionable) and all(d.get("state") == "updated" for d in actionable)
    if all_done and not halted:
        active["state"] = "completed"
        active["completed_at"] = rc.iso_now()
        if args.execute:
            state["history"].insert(0, active)
            state["active"] = None
        print("  All devices updated -> rollout completed (archived to history).")

    # 4) Always rewrite state (workflow commits only on diff). Dry-run still
    #    writes the recomputed state, but performs NO API mutations.
    if args.execute or args.write_dry_run:
        rc.write_state(state_path, state)
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description="Advance an active firmware rollout")
    parser.add_argument("--execute", action="store_true",
                        help="Apply API changes + persist state (default dry-run)")
    parser.add_argument("--state", default=STATE_PATH,
                        help="Path to rollout-state.json")
    parser.add_argument("--now", help="Override current time (ISO-8601) for testing")
    parser.add_argument("--write-dry-run", action="store_true",
                        help="Persist recomputed state even without --execute")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return do_check(args)


if __name__ == "__main__":
    sys.exit(main())
