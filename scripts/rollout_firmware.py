#!/usr/bin/env python3
"""Firmware rollout orchestrator (canary-capable).

A rollout finds an EXISTING firmware record at the target versionCode (any
scope), reuses its `fileUrl`, and creates device-scoped records for the
fan-out set.  It NEVER uploads binaries.

CLI:
    python scripts/rollout_firmware.py --version v3.21.0 \
        [--devices MAC1,MAC2 | --all-eligible] [--canary MAC] [--execute] [--abort]

Safety invariants (see plan Task 6):
  1. --dry-run is the DEFAULT; mutation requires --execute.
  2. workflow confirm==version gate (enforced in the workflow, not here).
  3. Refuse non-OTA-capable / EOL-stranded devices, with printed reasons.
  4. Refuse to start when an `active` rollout exists (must --abort first).
  5. A source firmware record at the target versionCode must already exist.
  6. Devices already at/above target are marked updated/skipped (no record).
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import rollout_common as rc

# Re-export network helpers at module level so tests can monkeypatch this
# module directly (and so the helper functions below use the patched versions).
fetch_json = rc.fetch_json
post_json = rc.post_json
build_auth = rc.build_auth

API_BASE = rc.API_BASE
STATE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "data", "rollout-state.json")
DASHBOARD_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "data", "dashboard-data.json")


def _sync_helpers():
    """Push any test-monkeypatched helpers down into rollout_common.

    Tests patch rollout_firmware.fetch_json/post_json/build_auth; the actual
    API calls live in rollout_common, so mirror them before doing API work.
    """
    rc.fetch_json = fetch_json
    rc.post_json = post_json
    rc.build_auth = build_auth


# --------------------------------------------------------------------------
# Eligibility
# --------------------------------------------------------------------------

def device_eligibility(device, target_code):
    """Return (eligible: bool, reason: str) for an --all-eligible candidate.

    Eligible = behind target AND ota_capable is True AND not hardware_eol AND
    hardware_max_firmware (if set) >= target.  Reason is printed for skips.
    """
    if device.get("ota_capable") is not True:
        return False, "not OTA-capable (ota_capable != true)"
    if device.get("hardware_eol") is True:
        return False, "hardware EOL — stranded, cannot reach target"
    max_fw = rc.safe_string(device.get("hardware_max_firmware"))
    if max_fw:
        max_code = rc.version_code(max_fw)
        if max_code is not None and max_code < target_code:
            return False, f"hardware_max_firmware {max_fw} < target (stranded)"
    current = rc.version_code(device.get("version"))
    if current is not None and current >= target_code:
        return False, "already at or above target"
    return True, ""


def explicit_device_check(device, target_code):
    """Eligibility for an explicitly-listed --devices target.

    Same OTA/EOL gating as --all-eligible (safety invariant #3) but does NOT
    filter on already-at-target here (that is handled later per invariant #6).
    """
    if device.get("ota_capable") is not True:
        return False, "not OTA-capable (ota_capable != true)"
    if device.get("hardware_eol") is True:
        return False, "hardware EOL — stranded, cannot reach target"
    max_fw = rc.safe_string(device.get("hardware_max_firmware"))
    if max_fw:
        max_code = rc.version_code(max_fw)
        if max_code is not None and max_code < target_code:
            return False, f"hardware_max_firmware {max_fw} < target (stranded)"
    return True, ""


def resolve_targets(dashboard, version, target_code, all_eligible, devices_csv):
    """Return (targets, skips).

    targets: list of {mac, label, device} for devices we will (try to) roll to.
    skips:   list of {mac, label, reason} printed and recorded as skipped.
    """
    section = rc.find_sensor_hub_section(dashboard)
    section_devices = section.get("devices", []) if isinstance(section, dict) else []
    by_mac = {}
    for dev in section_devices:
        if isinstance(dev, dict):
            by_mac[rc.normalize_mac(dev.get("mac"))] = dev

    targets = []
    skips = []

    if all_eligible:
        for dev in section_devices:
            if not isinstance(dev, dict):
                continue
            mac = rc.normalize_mac(dev.get("mac"))
            label = rc.safe_string(dev.get("name"))
            ok, reason = device_eligibility(dev, target_code)
            if ok:
                targets.append({"mac": mac, "label": label, "device": dev})
            elif reason == "already at or above target":
                # Not a target and not an error — silently excluded.
                continue
            else:
                skips.append({"mac": mac, "label": label, "reason": reason})
    else:
        requested = [rc.normalize_mac(m) for m in (devices_csv or "").split(",") if m.strip()]
        for mac in requested:
            dev = by_mac.get(mac)
            if dev is None:
                skips.append({"mac": mac, "label": "",
                              "reason": "not found in sensor-hub section"})
                continue
            label = rc.safe_string(dev.get("name"))
            ok, reason = explicit_device_check(dev, target_code)
            if ok:
                targets.append({"mac": mac, "label": label, "device": dev})
            else:
                skips.append({"mac": mac, "label": label, "reason": reason})

    return targets, skips


# --------------------------------------------------------------------------
# Abort
# --------------------------------------------------------------------------

def do_abort(state_path, execute):
    state = rc.load_state(state_path)
    active = state.get("active")
    if not active:
        print("No active rollout to abort.")
        return 0

    created = [d for d in active.get("devices", [])
               if rc.safe_string(d.get("firmware_record_id"))]
    print(f"Aborting rollout {active.get('rollout_id')} ({active.get('version')}).")
    if created:
        print("The following device-scoped firmware records were created and "
              "are NOT deleted — review/remove them in the console:")
        for d in created:
            print(f"  - {d.get('mac')} ({d.get('label')}): "
                  f"record {d.get('firmware_record_id')}")
    else:
        print("No firmware records had been created yet.")

    if not execute:
        print("[dry-run] would archive this rollout to history as 'aborted'. "
              "Re-run with --execute to apply.")
        return 0

    active["state"] = "aborted"
    active["aborted_at"] = rc.iso_now()
    state["history"].insert(0, active)
    state["active"] = None
    rc.write_state(state_path, state)
    print("Rollout aborted and archived to history.")
    return 0


# --------------------------------------------------------------------------
# Start
# --------------------------------------------------------------------------

def do_start(args):
    _sync_helpers()
    version = rc.normalize_version(args.version)
    target_code = rc.version_code(version)
    if target_code is None:
        print(f"ERROR: invalid --version '{args.version}'.")
        return 2

    state_path = args.state
    state = rc.load_state(state_path)

    # Safety invariant #4: refuse a second concurrent rollout.
    if state.get("active"):
        active = state["active"]
        print(f"ERROR: a rollout is already active "
              f"({active.get('rollout_id')} — {active.get('version')}). "
              f"Run with --abort first.")
        return 1

    dashboard = rc.load_dashboard(args.dashboard)

    if not args.all_eligible and not args.devices:
        print("ERROR: specify either --all-eligible or --devices MAC1,MAC2.")
        return 2

    targets, skips = resolve_targets(
        dashboard, version, target_code, args.all_eligible, args.devices)

    canary_mac = rc.normalize_mac(args.canary) if args.canary else ""
    mode = "canary" if canary_mac else "all"

    print("=" * 64)
    print(f"ROLLOUT PLAN — {version} (versionCode {target_code})")
    print(f"mode: {mode}" + (f"  canary: {canary_mac}" if canary_mac else ""))
    print("=" * 64)

    if skips:
        print("Skipped devices (safety invariant #3):")
        for s in skips:
            print(f"  SKIP {s['mac']} ({s['label']}): {s['reason']}")

    if not targets:
        print("No eligible target devices. Nothing to do.")
        return 0

    if canary_mac and canary_mac not in {t["mac"] for t in targets}:
        print(f"ERROR: canary {canary_mac} is not in the eligible target set.")
        return 2

    # Pure dry-run: print the eligibility plan and intended per-device action
    # WITHOUT any network read or write, then stop. This keeps a credential-less
    # dry-run from stalling on the API and guarantees zero mutation.
    if not args.execute:
        for t in targets:
            mac = t["mac"]
            label = t["label"]
            if canary_mac:
                action = "CANARY (record now)" if mac == canary_mac else "PENDING (awaits canary)"
            else:
                action = "OFFERED (record now)"
            print(f"  {action}: {mac} ({label})")
        print("-" * 64)
        print("[dry-run] No API reads/writes performed, state file NOT written. "
              "Re-run with --execute to apply (which verifies the source "
              "firmware record and resolves device UUIDs).")
        return 0

    headers = build_auth(API_BASE)

    # Safety invariant #5: a source firmware record at the target must exist.
    source = rc.find_source_firmware(API_BASE, target_code, headers)
    if source is None:
        print(f"ERROR: no existing firmware record found at versionCode "
              f"{target_code} (version {version}). Upload it first via "
              f"deploy-ota.sh or the console. Refusing to fabricate a fileUrl "
              f"(safety invariant #5).")
        return 1
    print(f"Source firmware record: id={source.get('id')} "
          f"fileUrl={source.get('fileUrl')}")

    # Build the per-device plan.
    plan_devices = []
    will_create = []  # (mac, label, uuid) we intend to POST for
    for t in targets:
        mac = t["mac"]
        label = t["label"]
        # Safety invariant #6: skip devices already at/above target.
        current_code = rc.latest_version_code_for_mac(API_BASE, mac, headers)
        if current_code is not None and current_code >= target_code:
            plan_devices.append({
                "mac": mac, "label": label, "state": "updated",
                "reason": "already at/above target per /firmwares/latest",
                "firmware_record_id": None, "updated_at": rc.iso_now()})
            print(f"  UPDATED {mac} ({label}): already >= target — no record")
            continue

        uuid = rc.resolve_device_uuid(API_BASE, mac, headers)
        if uuid is None:
            plan_devices.append({
                "mac": mac, "label": label, "state": "skipped",
                "reason": "could not resolve device UUID from API",
                "firmware_record_id": None, "updated_at": rc.iso_now()})
            print(f"  SKIP {mac} ({label}): could not resolve UUID")
            continue

        if canary_mac:
            if mac == canary_mac:
                state_name = "canary"
                will_create.append((mac, label, uuid))
                action = "CANARY (record now)"
            else:
                state_name = "pending"
                action = "PENDING (awaits canary)"
        else:
            state_name = "offered"
            will_create.append((mac, label, uuid))
            action = "OFFERED (record now)"

        plan_devices.append({
            "mac": mac, "label": label, "state": state_name, "reason": "",
            "firmware_record_id": None, "updated_at": rc.iso_now(),
            "_uuid": uuid})
        print(f"  {action}: {mac} ({label}) uuid={uuid}")

    # Record skips in the device list too.
    for s in skips:
        plan_devices.append({
            "mac": s["mac"], "label": s["label"], "state": "skipped",
            "reason": s["reason"], "firmware_record_id": None,
            "updated_at": rc.iso_now()})

    # ----- EXECUTE: create records for the canary (or all, if no canary) -----
    for mac, label, uuid in will_create:
        record = rc.create_device_firmware_record(
            API_BASE, source, version, target_code, uuid, headers)
        rec_id = rc.safe_string(record.get("id")) if isinstance(record, dict) else ""
        for d in plan_devices:
            if d["mac"] == mac:
                d["firmware_record_id"] = rec_id or None
                d["updated_at"] = rc.iso_now()
        print(f"  CREATED record {rec_id} for {mac} ({label})")

    # Strip internal _uuid before persisting.
    for d in plan_devices:
        d.pop("_uuid", None)

    rollout_id = f"{version}-{rc.now_utc().strftime('%Y%m%dT%H%MZ')}"
    active = {
        "rollout_id": rollout_id,
        "version": version,
        "version_code": target_code,
        "mode": mode,
        "canary_mac": canary_mac or None,
        "canary_deadline_h": args.canary_deadline_h,
        "created_at": rc.iso_now(),
        "source_firmware_id": rc.safe_string(source.get("id")) or None,
        "source_file_url": rc.safe_string(source.get("fileUrl")),
        "devices": plan_devices,
    }
    state["active"] = active
    rc.write_state(state_path, state)
    print(f"Rollout {rollout_id} started; state written to {state_path}.")
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(description="Firmware rollout orchestrator")
    parser.add_argument("--version", help="Target firmware version, e.g. v3.21.0")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--devices", help="CSV of target MAC addresses")
    group.add_argument("--all-eligible", action="store_true",
                       help="Target all eligible sensor-hub devices")
    parser.add_argument("--canary", help="MAC of the canary device")
    parser.add_argument("--canary-deadline-h", type=int, default=24,
                        help="Hours before an un-updated canary is failed")
    parser.add_argument("--execute", action="store_true",
                        help="Apply changes (default is dry-run)")
    parser.add_argument("--abort", action="store_true",
                        help="Abort the active rollout (archive as aborted)")
    parser.add_argument("--state", default=STATE_PATH,
                        help="Path to rollout-state.json")
    parser.add_argument("--dashboard", default=DASHBOARD_PATH,
                        help="Path to dashboard-data.json")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.abort:
        return do_abort(args.state, args.execute)
    if not args.version:
        print("ERROR: --version is required (unless --abort).")
        return 2
    return do_start(args)


if __name__ == "__main__":
    sys.exit(main())
