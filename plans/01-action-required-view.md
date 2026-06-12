# Plan 01 — Action-Required View (OTA capability, hardware tracking, update plan, last-seen)

Read `plans/00-context.md` first.

## Goal

Turn the dashboard from a passive version tracker into a triage tool that directly answers the operator's three questions:

1. Which devices need a firmware update?
2. Of those, which will update remotely (OTA) and which need a physical visit?
3. Which devices need a hardware change?

## Design decision (already made — do not re-litigate)

**OTA capability is hardware-based.** Whether a device can OTA-update is determined by its board/hardware type, not its firmware version. The mapping lives in `data/devices.json`.

## Implementation

### 1. data/devices.json — hardware registry

- Add a top-level (or per-section) map:
  ```json
  "hardware_capabilities": {
    "ESP32-S3-WROOM-1U-N16": { "ota": true },
    "Adafruit Feather ESP32-S3 TFT": { "ota": true },
    "ESP32-WROOM-32 (legacy)": { "ota": false }
  }
  ```
  The exact hardware names/values above are PLACEHOLDERS — populate `hardware` on each device entry with its real board, and leave the capability map with sensible entries plus a comment-style `"_note"` key telling the user to verify the ota flags. The firmware repo `../sensorhub-data-collector/devices/<name>/version.json` has a `hardware_target` field per device config — use it to seed the per-device `hardware` values where the device folder name maps to a dashboard device label.
- Add optional per-device override: `"ota_override": true|false` (wins over hardware map; for known-broken OTA on otherwise-capable hardware).
- Add optional per-device `"hardware_note"`: free text like "swap to S3 planned Q3" — signals a pending hardware change.

### 2. scripts/build_dashboard_data.py — derive fields

For each device summary, add:

- `hardware` — already partially supported via entry; ensure it flows through for all devices.
- `ota_capable` — `true` / `false` / `null` (unknown): resolve `ota_override` → `hardware_capabilities[hardware].ota` → `null` if hardware missing/unmapped.
- `hardware_note` — passthrough.
- `last_seen` — **discovery step required.** Dump the full JSON of `GET /devices/{mac}` for one device (print to stderr during development) and look for a last-report/last-log/updatedAt timestamp field. The firmware POSTs to `/devices/{id}/logs` every 1–60 s, so the API likely tracks last contact. If a field exists, extract it as ISO timestamp into `last_seen`. If nothing usable exists, set `last_seen: null` and the frontend must degrade gracefully (omit the indicator entirely — no fake data).

### 3. index.html — frontend

**a. Device cards + table row:**
- OTA badge: small pill — `OTA` (teal/positive), `Physical` (warning/amber), nothing if unknown. Only show prominently when the device is behind (that's when it matters); in up-to-date state render it muted/subtle.
- Hardware shown on card (already in drawer telemetry; add short form to card/table — table gets a Hardware column).
- If `hardware_note` set: small wrench icon + tooltip; full note in drawer.
- If `last_seen` available: relative time ("4 min ago"); muted grey when older than ~24h ("silent 3 days") — a silent device can't OTA even if capable.

**b. New "Update plan" panel** — the centerpiece. Place it in/near the Overview or Fleet readiness section. Buckets (computed from existing status + new fields):
- **Self-updating via OTA** — behind target AND `ota_capable === true` AND (no last_seen data OR seen recently). These should converge on their own.
- **Needs site visit** — behind target AND (`ota_capable === false` OR silent > 24h when last_seen exists).
- **Hardware change pending** — any device with `hardware_note`.
- **Unknown** — behind target AND `ota_capable === null`.

Each bucket: count + compact device list (name, version → target, hardware). Clicking a device opens the existing drawer. Empty buckets show a quiet "none" state. When zero devices are behind, the whole panel collapses to a single "Fleet converged — nothing to do" line (good screenshot state).

**c. Filters:** add an "OTA" filter chip group to the device browser (OTA / Physical / Unknown), alongside existing status and component chips. Reuse the existing chip pattern and `applyFiltersAndRender()` flow.

**d. Drawer:** add OTA capability + hardware note rows to the telemetry card; mention OTA reachability in the risk-signals card ("Behind target and not OTA-capable — requires physical access").

### 4. Stats

Repurpose or extend the 4 hero stat cards: "Needs attention" stat should split or subtitle into "N remote / M on-site" once the data exists.

## Acceptance

- Pipeline runs without API creds failing the new logic (ota fields derived from devices.json alone; last_seen null-safe).
- Frontend renders correctly with `last_seen` absent everywhere.
- All new device-sourced strings go through `escapeHtml()`.
- Card, table, drawer, filters, update-plan panel all consistent on the same derived fields (single derivation helper in JS, not copy-pasted logic).
- Existing features untouched: search, sort, copy-view URL, timeline, doughnut.

## Out of scope

- Device health/error/telemetry display (lives on 3A Console).
- Any write/actions against the API.
- Version migration chart (plan 03), UX quick wins (plan 02).
