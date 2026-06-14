# Fleet Live Data & Rollout Operations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the dashboard from a manually-maintained mirror into a live, self-assembling fleet-operations tool: device-reported firmware versions and last-seen as source of truth, an OTA audit trail, automated stale-device alerts, and orchestrated (canary-capable) firmware rollouts — while staying a static GitHub Pages site backed by GitHub Actions.

**Architecture:** The site remains static. All API access stays server-side in Python scripts run by GitHub Actions (the browser only reads committed JSON — unchanged). New capabilities are added as: (a) richer fields in `data/dashboard-data.json` produced by `scripts/build_dashboard_data.py`, (b) new standalone scripts + workflows for rollouts and alerts, (c) frontend consumption of the new fields in `index.html`. The status site and the console stay separate apps but talk via the monitoring API (`https://monitoring-api.3aentreprise.com`).

**Tech Stack:** Python 3.12 stdlib only (no pip deps), stdlib `unittest` for tests, vanilla JS/HTML/CSS in single `index.html`, GitHub Actions, `gh` CLI for issue automation. Firmware task: C++ / PlatformIO (ESP32-S3) in `glaecier-sensorhub-data-collector`.

**Repos:**
- Status site: `C:\Programming_projects\3a-console-sensor-device-status` (this repo) — branch `feature/fleet-ops`
- Firmware: `C:\Programming_projects\glaecier-sensorhub-data-collector` — branch `feature/hardware-identity`

**Cross-cutting rules for every implementer:**
- Verify endpoint paths/auth against the live spec first: `curl -s https://monitoring-api.3aentreprise.com/openapi.json` (public, no auth). Agent reports about auth modes (Bearer vs Basic per endpoint) are approximate — the spec is truth.
- No credentials exist on the local machine. Scripts must be testable offline: network access goes through the module-level `fetch_json(url, headers)` helper (and a new `post_json`) so tests can monkeypatch them. Never make tests hit the network.
- Auth strategy for scripts: try `POST /auth/token` (form fields `username`, `password` from env `API_USERNAME`/`API_PASSWORD`) to get a Bearer JWT; on failure fall back to HTTP Basic with the same creds. Factor this into one helper (Task 1) reused by later scripts.
- Orchestrator commits after each task; implementer agents do NOT run `git commit` or `git push`.
- Frontend: all markup built via existing escape helpers (`escapeHtml()`); follow existing CSS-custom-property theming and existing naming/comment style. Read the relevant sections of `index.html` before editing — it is ~6,800 lines; navigate by function name, never rewrite wholesale.

**Execution order (conflict-driven):**
- Wave 1 (parallel, disjoint files/repos): Task 1+2 (one agent, pipeline), Task 6 (rollout scripts/workflows — new files only), Task 9 (firmware repo).
- Wave 2 (after Task 1+2; sequential, all touch `index.html`): Task 3 → Task 4 → Task 5+7 (one agent).
- Wave 2b (parallel with Wave 2, after Task 1): Task 8 (alerts — new script + workflow edit).
- Wave 3: Task 10 (docs).

**Agent model/thinking assignments:**
| Task(s) | Model | Thinking | Rationale |
|---|---|---|---|
| 1+2 Pipeline | Sonnet | think hard | Intricate merge logic + fallbacks, but well-specified Python |
| 6 Rollout orchestration | Opus | ultrathink | Safety-critical: pushes firmware to physical devices; canary state machine |
| 9 Firmware identity | Sonnet | think | Small additive C++ change, but unfamiliar embedded codebase |
| 3 Frontend live data | Sonnet | think hard | Careful surgical edits in 6,800-line file |
| 4 Frontend reorder | Sonnet | think | Mostly moving existing blocks |
| 5+7 Readiness + rollout UI | Sonnet | think hard | New UI + version math |
| 8 Alerts | Sonnet | think | Issue lifecycle edge cases |
| 10 Docs | Haiku | default | Mechanical rewrite from facts |

---

### Task 1: Pipeline — live device telemetry (last seen, reported version, online state)

**Files:**
- Modify: `scripts/build_dashboard_data.py`
- Create: `tests/__init__.py` (empty), `tests/test_build_dashboard_data.py`
- Test command: `python -m unittest discover -s tests -v` (run from repo root)

**Context:** Today the pipeline calls only `GET /devices/{mac}` and `GET /firmwares/latest?deviceMac=`. The device `version` comes from the hand-edited registry (`installed_firmware_version` in `data/devices.json`), so the dashboard shows what a human typed at flash time, not what devices report. The API has the truth: `GET /devices` (list) returns per-device `lastLog` (with `firmwareVersion`, `createdAt`, `batteryLevel`), `isOnline`, and/or `lastReportedAt`; `GET /devices/{mac}/logs/latest` returns the latest log for one device. `extract_last_seen()` (line ~283) already scans candidate fields and currently always returns None.

**New/changed per-device output fields in `dashboard-data.json`:**
```json
{
  "last_seen": "2026-06-13T08:21:44+00:00",   // from lastLog.createdAt or lastReportedAt; null if unknown
  "is_online": true,                            // API isOnline; null if unknown
  "reported_version": "v3.19.0",               // normalize_version(lastLog.firmwareVersion); "" if none
  "version": "v3.19.0",                        // NOW: reported_version || registry version (was registry-first)
  "version_source": "reported",                // "reported" | "registry" | ""
  "version_mismatch": false,                    // true when both exist and differ
  "battery_level": 87.0,                        // lastLog.batteryLevel; null if absent
  "declared_deployment_version": "v3.19.0"     // unchanged: registry value, kept for mismatch display
}
```

**Steps:**

- [ ] **Step 1: Failing tests first.** Create `tests/test_build_dashboard_data.py` importing the module via `sys.path` insertion of `scripts/`. Monkeypatch `build_dashboard_data.fetch_json`. Tests (write all, watch them fail):
  - `test_version_prefers_reported_over_registry`: registry says `v3.16.6`, API lastLog.firmwareVersion `3.19.0` → `version == "v3.19.0"`, `version_source == "reported"`, `version_mismatch is True`.
  - `test_version_falls_back_to_registry`: no lastLog → `version` from registry, `version_source == "registry"`, `version_mismatch is False`.
  - `test_last_seen_from_last_log_created_at` and `test_is_online_passthrough`.
  - `test_device_list_fetch_failure_falls_back_to_per_device_logs_latest`: list endpoint raises HTTPError(401) → per-device `GET /devices/{mac}/logs/latest` is used.
  - `test_status_uses_effective_version`: a device behind on registry but current per reported version is "Up to date".
- [ ] **Step 2: Implement.** Add to `build_dashboard_data.py`:
  - `build_auth()` helper: tries `POST {api_base}/auth/token` (urlencoded form `username`/`password`) → returns `{"Authorization": "Bearer <token>"}`; on any error returns existing Basic headers. Add `post_json(url, data, headers)` and `post_form(url, fields, headers)` helpers next to `fetch_json`.
  - `fetch_device_directory(api_base, headers) -> dict[mac, dict]`: one call to `GET /devices?limit=500` (handle both bare-list and `{"devices": [...]}`-wrapper response shapes), keyed by `normalize_mac(macAddress)`. On HTTPError/URLError return `{}` and remember the failure so per-device fallback kicks in.
  - In `fetch_device_summary()`: accept the directory entry; derive `last_seen` (first valid of `lastLog.createdAt`, `lastReportedAt`, existing `extract_last_seen()` candidates), `is_online`, `reported_version`, `battery_level`. If directory is empty for this mac, try `GET {api_base}/devices/{encoded_mac}/logs/latest` (tolerate 401/403/404 → None). Compute `version = reported_version or entry_version(entry) or normalize_version(firmware_data.version)` and set `version_source`/`version_mismatch`. `build_failure_device()` gains the new fields as null/empty defaults.
  - Hardware preference: in `fetch_device_summary`, `hardware = extract_hardware(entry) or safe_string(device_data.get("settings", {}).get("hardwareInfo", {}).get("board"))` (forward-compat with Task 9; settings may be None — guard).
- [ ] **Step 3: Run tests → all pass.** Also run `python scripts/build_dashboard_data.py` WITHOUT creds: it must still complete using registry fallback (failures listed, exit 0).
- [ ] **Step 4: Report** changed function list + new schema fields for the orchestrator's review.

### Task 2: Pipeline — OTA audit trail + derived pending updates

**Files:**
- Modify: `scripts/build_dashboard_data.py`
- Modify: `tests/test_build_dashboard_data.py` (add cases)

**Context:** Firmware sends event code **119 “OTA Update Completed”** (payload includes `firmwareVersion`, `newVersionCode`) on every successful OTA. Manual `pending_ota_version` in the registry exists only because the pipeline can’t see reality. Endpoint: `GET /devices/{mac}/events?date_from=...&limit=...` (check spec for exact params/auth; tolerate 401/403 → skip history gracefully).

**New per-device fields:**
```json
{
  "ota_history": [ {"date": "2026-05-16T20:12:00+00:00", "version": "v3.16.6", "version_code": 3160600} ],
  "pending_ota_version": "v3.20.0",      // derived: latest firmware (per /firmwares/latest) newer than reported version AND ota_capable; registry value kept as fallback
  "pending_ota_source": "api"            // "api" | "registry" | ""
}
```

**Steps:**

- [ ] **Step 1: Failing tests.** `test_ota_history_extracted_from_code_119_events` (mixed event codes → only 119, newest first, max 10); `test_pending_ota_derived_when_latest_firmware_newer` (reported v3.16.6, /firmwares/latest v3.20.0, ota_capable True → pending v3.20.0, source "api"); `test_pending_ota_not_derived_when_not_ota_capable`; `test_pending_ota_registry_fallback_when_events_unavailable`.
- [ ] **Step 2: Implement** `fetch_ota_history(api_base, mac, headers)` (filter events to code 119 via `eventType.code` or `code` — inspect spec/sample; map to `{date, version, version_code}`) and `derive_pending_ota(reported_version, latest_firmware, ota_capable)` using `version_tuple()`. Wire both into `fetch_device_summary`; clear pending when `reported_version` ≥ pending (device already took it).
- [ ] **Step 3: Tests pass; credential-less full run still exits 0.**
- [ ] **Step 4: Report.**

### Task 3: Frontend — consume live telemetry & audit trail

**Files:**
- Modify: `index.html` only

**Context:** All last-seen UI already exists and renders nothing (Fleet Pulse dimming, drawer `last-seen-row`, silent badges, "OTA but silent → site visit" bucket). Key functions: `getUpdateStatus()`, `matchesSearch()`, `fetchDeviceFromApi()` (dead fallback), device drawer renderer, card/table renderers, `dashboardState`.

**Steps:**

- [ ] **Step 1: Map the territory.** Grep `index.html` for `last_seen|lastSeen`, `pending_ota`, `fetchDeviceFromApi`, `matchesSearch`, `declared_deployment_version` and read those regions before editing.
- [ ] **Step 2: Wire new fields** into the device normalization layer: `lastSeen` (now real), `isOnline`, `reportedVersion`, `versionSource`, `versionMismatch`, `batteryLevel`, `otaHistory`, `pendingOtaSource`. Existing silent-device logic must light up from real `last_seen`. Where `is_online === false`, treat as silent even if last_seen is recent-ish.
- [ ] **Step 3: Version truth UI.** Cards/table/drawer keep showing `version` (now device-reported when available). When `versionMismatch`, show a small amber "registry says {declared}" hint in the drawer telemetry DL and a dot-indicator on the version badge (title tooltip). Add a "Source: device-reported / registry" line in the drawer.
- [ ] **Step 4: Update history in drawer.** New drawer section "Update history" listing `otaHistory` rows (`v3.16.6 — 16 May 2026, OTA`) using existing relative-time helpers; hidden when empty.
- [ ] **Step 5: Cleanups.** (a) Delete `fetchDeviceFromApi()` and its call path — the browser can never authenticate; the registry-only fallback for missing `dashboard-data.json` stays. (b) Add `device.mac` to `matchesSearch()`. (c) Online/offline: small green/grey dot next to last-seen badge when `isOnline` is non-null. (d) Battery in drawer DL when non-null.
- [ ] **Step 6: Verify.** `python -m http.server 8000` + check the page renders with the CURRENT committed `dashboard-data.json` (old schema — every new field must tolerate absence; this is the regression gate). Then craft a tiny synthetic `dashboard-data.json` variant in `/tmp` (do not commit) with new fields and eyeball via a second server root if feasible; otherwise verify by code-reading the null-paths. Check browser console for errors via the served page (report what you verified).

### Task 4: Frontend — action-first page order

**Files:**
- Modify: `index.html` only

**Steps:**

- [ ] **Step 1:** Move the **"Action required" Update Plan card and "Status balance" card** (the fleet-readiness row) to directly under the top navbar, above the hero. Order within the row: Action required first (left/top), Status balance second.
- [ ] **Step 2:** Slim the hero: keep section tabs, snapshot timestamp, the 4 metric cards and release-info aside, but cut its vertical padding (~50%) and drop the CTA buttons ("Inspect devices"/"Review timeline" — navigation already covers this). Do not delete the metrics or animations.
- [ ] **Step 3:** Update the sticky-nav anchor order to match the new visual order (Overview link should land on the action row).
- [ ] **Step 4:** Controllers-section hygiene: in `data/devices.json`, the `humidity-controller` track has no `latest_version` and its device has empty `installed_firmware_version` — verify the UI shows these as "Unknown"/"Not deployed" without rendering glitches in the new top placement; fix any glitch found (UI-side only; do not invent version data).
- [ ] **Step 5:** Verify with `python -m http.server 8000`: scroll order, anchors, mobile breakpoint (narrow the window — Bootstrap col classes must not wrap brokenly). Report before/after section order.

### Task 5: Frontend — release readiness panel

**Files:**
- Modify: `index.html` only

**Context:** Data needed is already client-side: per-device `hardware`, hardware capabilities are reflected in `ota_capable`/`hardware_eol`/`hardware_max_firmware` fields, plus `version`/`target_version`.

**Steps:**

- [ ] **Step 1:** Add a "Release readiness" card at the top of the `#versions` section. Default target = the track's `latest_version`; an inline version input (placeholder `v3.21.0`, validated by the existing semver regex pattern) lets the user ask "what if we ship X?".
- [ ] **Step 2:** Compute, for the chosen hypothetical target, the bucket counts using the SAME classification logic as the existing Update Plan (reuse/extract its classifier into a parameterized function rather than duplicating): would-self-update (OTA), needs-site-visit, stranded-on-EOL-hardware (`hardware_max_firmware < target`), unknown-path, already-at-or-above. Render as 5 count chips + expandable device lists per bucket (reuse the plan-bucket row component/styles).
- [ ] **Step 3:** Verify locally; entering an absurd target (`v99.0.0`) must not throw and should show everyone stranded/behind correctly.

### Task 6: Rollout orchestration — scripts + workflows (canary-capable)

**Files:**
- Create: `scripts/rollout_firmware.py`
- Create: `scripts/check_rollout.py`
- Create: `tests/test_rollout.py`
- Create: `.github/workflows/firmware-rollout.yml`
- Create: `.github/workflows/firmware-rollout-monitor.yml`
- Create: `data/rollout-state.json` (initial: `{"active": null, "history": []}`)
- Do NOT touch: `scripts/build_dashboard_data.py`, `index.html`

**Context:** Devices poll the API for firmware (`/ota/check` flow) and self-update when a record with a higher `versionCode` is scoped to them. Firmware records are `{version, versionCode, buildDate, fileUrl, deviceId}`. **The existing single-device flow already exists**: `scripts/deploy-ota.sh` in `glaecier-sensorhub-data-collector` builds the binary, uploads it (`POST /firmwares/upload`), and creates a record scoped to ONE device UUID, authenticated with a Bearer `API_TOKEN`. Records in the API are therefore typically DEVICE-SCOPED, not global. **A rollout = finding an existing record with the target versionCode (any scope), reusing its `fileUrl`, and creating device-scoped records for the remaining targets.** Intended composition: operator deploys the canary with `deploy-ota.sh` (or the rollout script creates the canary record from an existing upload), then this orchestration fans out. This script must never upload binaries. Endpoints (verify in openapi.json): `GET /firmwares` (list — filter client-side by `versionCode` since server filter params are unverified), `POST /firmwares`, `GET /devices/{mac}` (to resolve device UUID — `version.json` `device_uuid` values are NOT available to this repo's scripts at runtime except via the CI checkout; resolve UUIDs from the API by MAC), `GET /devices/{mac}/logs/latest`. versionCode formula: `major*1000000 + minor*10000 + patch*100 + build`.

**Safety invariants (non-negotiable):**
1. `--dry-run` is the DEFAULT; mutation requires explicit `--execute`.
2. The workflow_dispatch requires a `confirm` input that must literally equal the target version string.
3. Refuse devices that are not OTA-capable per `data/dashboard-data.json` (`ota_capable !== true`) or whose `hardware_max_firmware` < target — print why, skip them.
4. Refuse to start when `rollout-state.json` has an `active` rollout (must `--abort` first, which archives it to `history` with state `aborted`; abort does not delete API records — it reports which records were created so they can be reviewed in the console).
5. A source firmware record with the target versionCode must already exist in the API (uploaded earlier via `deploy-ota.sh` or the console UI); never fabricate `fileUrl`. If the canary already has a record at the target versionCode (operator used `deploy-ota.sh`), do not create a duplicate for the canary — record it as `canary` with the existing record noted.
6. If a target device's CURRENT `/firmwares/latest` versionCode is already ≥ target, mark `updated`/`skipped` instead of creating a record (covers devices already pushed manually via `deploy-ota.sh`).

**`rollout_firmware.py` CLI:**
`python scripts/rollout_firmware.py --version v3.21.0 [--devices MAC1,MAC2 | --all-eligible] [--canary MAC] [--execute] [--abort]`
- Resolves targets from `data/dashboard-data.json` (`--all-eligible` = status needs-update/patch-available AND ota_capable AND not hardware_eol, in the sensor-hub section).
- Canary mode: only the canary device gets its firmware record now; others recorded as `pending`.
- Writes `data/rollout-state.json`:
```json
{
  "active": {
    "rollout_id": "v3.21.0-20260613T1200Z",
    "version": "v3.21.0", "version_code": 3210000,
    "mode": "canary", "canary_mac": "3c:0f:02:c7:eb:cc", "canary_deadline_h": 24,
    "created_at": "...", "source_firmware_id": "...",
    "devices": [ {"mac": "...", "label": "...", "state": "canary|pending|offered|updated|failed|skipped", "reason": "", "firmware_record_id": null, "updated_at": "..."} ]
  },
  "history": []
}
```
- State meanings: `canary`/`offered` = device-scoped record created, awaiting device; `pending` = waiting for canary success; `updated` = device's reported versionCode ≥ target; `failed` = deadline passed; `skipped` = ineligible (with reason).

**`check_rollout.py`** (scheduled): no active rollout → exit 0 silently. Else for each `canary`/`offered` device, fetch `GET /devices/{mac}/logs/latest`; reported version ≥ target → `updated`. Canary advance: canary `updated` AND its log `createdAt` is AFTER the canary record's `updated_at` (post-update heartbeat = healthy) → create records for all `pending` → `offered`. Canary deadline exceeded → canary `failed`, rollout `state: "halted"` (no fan-out ever). All devices `updated` → move `active` to `history` with `state: "completed"`. Always rewrite state file (workflow commits only on diff).

**Workflows:** `firmware-rollout.yml`: `workflow_dispatch` inputs `version`, `devices` (CSV or `all-eligible`), `canary_mac` (optional), `confirm`; guard step fails unless `confirm == inputs.version`; checkout, setup-python, run script with `--execute`, commit `data/rollout-state.json` (commit message `Rollout: start {version}`). `firmware-rollout-monitor.yml`: schedule `*/30 * * * *` + dispatch; runs `check_rollout.py`; commits state diff (`Rollout: progress {version}`). Both need `permissions: contents: write` and env `API_USERNAME`/`API_PASSWORD`; reuse the auth helper pattern (duplicate the small helper in-file; scripts must stay stdlib-only and self-contained).

**Steps:**

- [ ] **Step 1: Failing tests** (`tests/test_rollout.py`, monkeypatched network + tmp state file): eligibility filtering (non-OTA skipped with reason), canary creates exactly one record, refuse-second-rollout, confirm-gate logic is in workflow (not testable here) but `--execute` gate is: without it, zero `post_json` calls; canary success fans out; canary deadline halts; completion archives to history.
- [ ] **Step 2: Implement both scripts** (shared helpers may live in a new `scripts/rollout_common.py` if it keeps each file clearer; tests import it).
- [ ] **Step 3: Tests pass.** Run both scripts credential-less: `rollout_firmware.py --version v0.0.1 --devices aa:bb...` (dry-run) must print the plan and write nothing; `check_rollout.py` with empty state exits 0.
- [ ] **Step 4: Lint the workflows** mentally against existing `update-dashboard-data.yml` conventions (checkout@v5, setup-python@v6, same commit pattern). Report the full safety-invariant checklist with how each is enforced.

### Task 7: Frontend — surface rollout state

**Files:**
- Modify: `index.html` (same agent as Task 5)

**Steps:**

- [ ] **Step 1:** Fetch `data/rollout-state.json` alongside the other data files (tolerate 404 → feature hidden).
- [ ] **Step 2:** When `active` exists: banner under the top nav ("Rollout v3.21.0 in progress — canary phase: 1 updated / 14 pending", amber; red variant when `halted`) and a compact progress strip inside the Update Plan card (per-state counts; device chips reuse plan-bucket rows, clickable → drawer). Drawer: device participating in active rollout gets a "Rollout" DL row with its state.
- [ ] **Step 3:** Map rollout `offered`/`canary` onto the existing "Pending {version}" pill so the old registry-pending pill and the new rollout state don't double-render (rollout state wins).
- [ ] **Step 4:** Verify locally with a hand-crafted temporary `rollout-state.json` (active canary, active halted, no file) — then restore the committed initial file exactly.

### Task 8: Fleet alerts — GitHub issues for stale/behind/stalled devices

**Files:**
- Create: `scripts/check_fleet_alerts.py`, `tests/test_fleet_alerts.py`
- Modify: `.github/workflows/update-dashboard-data.yml` (append job/step)

**Rules (evaluated from `data/dashboard-data.json` + `data/rollout-state.json` + `data/version-changes.json`; no API calls):**
1. **Silent**: `last_seen` non-null and older than 24h, status not in {In development, Not deployed}.
2. **Behind**: status "Needs update" AND the target release's date in `version-changes.json` is older than 7 days.
3. **Rollout stalled**: active rollout device in `offered`/`canary` for >48h (from its `updated_at`).

**Issue contract:** one issue per device+rule; title `[fleet-alert] <label> (<mac>): <rule-slug>`; label `fleet-alert` (create label if missing); body = facts + dashboard deep link `https://clausen-engineering.github.io/3a-console-sensor-device-status/#device=<mac-hex>`; if open issue exists → no-op (no comment spam); rule no longer firing → close with a one-line comment. Use `gh issue list/create/close --repo "$GITHUB_REPOSITORY"` via `subprocess`; `GH_TOKEN` from env.

**Steps:**

- [ ] **Step 1: Failing tests** for pure rule functions (`evaluate_alerts(dashboard, rollout, version_changes, now) -> list[Alert]`) — silent boundary (23h59m no, 24h01m yes), dev-status exemption, behind-with-fresh-release exemption, stalled-rollout. Issue I/O wrapped in thin functions tested only for command construction (capture argv, don't run gh).
- [ ] **Step 2: Implement.** `--dry-run` default (prints planned creates/closes), `--execute` for CI.
- [ ] **Step 3:** Append to `update-dashboard-data.yml` after the commit step: run `python scripts/check_fleet_alerts.py --execute` with `env: GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}` and add `issues: write` to workflow permissions.
- [ ] **Step 4:** Tests pass; credential-less dry run against committed data exits 0.

### Task 9: Firmware — self-reported hardware identity + deployment-script metadata (other repo)

**Repo:** `C:\Programming_projects\glaecier-sensorhub-data-collector`, branch `feature/hardware-identity`

**Files (verify exact names by reading the repo first):**
- Modify: `platformio.ini` (add `custom_hardware_revision = sensor-hub-v1.5-esp32-s3-n16r8` next to `custom_firmware_version`)
- Modify: the script that turns custom options into defines (`scripts/version_defines.py` or equivalent) → emit `HARDWARE_REVISION` string define
- Modify: `src/device_settings.cpp` (registration payload builder, `toVirtualRegistrationJson`)
- Modify: `scripts/deploy-device.sh` and `scripts/deploy-ota.sh` (write `hardware_target` into `devices/<name>/version.json`)

**Deployment-script context:** The status-site pipeline already reads `hardware_target` from `devices/*/version.json` (`build_dashboard_data.py` `build_repo_device_summaries`), but no script ever writes it — the field is dead today. `version.json` currently holds `template_version`, `initial_deployment_date`, `deployment_date`, `deployment_environment`, `deployment_location`, `mac_address`, `device_uuid`.

**Change:** Registration `settings` gains:
```json
"hardwareInfo": { "board": "sensor-hub-v1.5-esp32-s3-n16r8", "chip": "<ESP.getChipModel()>", "flashMb": 16, "psramMb": 8 }
```
`board` from the build define; `chip` at runtime via `ESP.getChipModel()`; flash/PSRAM via `ESP.getFlashChipSize()/1048576` and `ESP.getPsramSize()/1048576`. Additive only — change nothing else in the payload. The status-site pipeline already prefers its registry value, so this field is supplemental (used when registry has no `hardware`, see Task 1 Step 2).

**Steps:**

- [ ] **Step 1:** Read `platformio.ini`, the defines script, and the registration-payload code; confirm where settings JSON is built (also check whether settings are re-sent on boot for already-registered devices — if a separate "update settings" call exists, add the field there too).
- [ ] **Step 2:** Implement the firmware change; keep JSON-doc memory allocation sufficient (check `JsonDocument` capacity if static).
- [ ] **Step 3:** Deployment scripts: in `deploy-device.sh` (USB deploy — find where it writes/updates `version.json`) and `deploy-ota.sh`, write `"hardware_target"` into the device's `version.json`: default it to the `custom_hardware_revision` value parsed from `platformio.ini` (reuse the existing `get_platformio_option` helper pattern); preserve an existing non-empty `hardware_target` rather than overwriting (a device on older hardware keeps its manually-set value). JSON edits from bash must go through a small `python3 -c` one-liner or `jq` if already used — do not regex-replace JSON.
- [ ] **Step 4:** Per the firmware repo's CLAUDE.md: do NOT compile or upload. Verify by careful review; if `scripts/version_defines.py` is runnable standalone, dry-run it. Report explicitly that compilation was not run and must be done by the owner.
- [ ] **Step 5:** Report exact payload diff (before/after JSON) and the `version.json` diff produced by a dry-run of the script change.

### Task 10: Docs refresh

**Files:**
- Modify: `README.md`, `CLAUDE.md`, `AGENTS.md` (status repo)

**Steps:**

- [ ] **Step 1:** Rewrite README to current reality: `dashboard-data.json` multi-section schema (incl. new Task 1/2 fields), the 6 statuses, the live-telemetry data flow, rollout workflows (how to start a rollout via Actions → workflow_dispatch, the confirm gate, canary behavior, composition with the firmware repo's `deploy-ota.sh`), fleet alerts, `gh`/secrets requirements. Remove references to `device-status.json`; the deployment shell scripts live in `glaecier-sensorhub-data-collector/scripts/` — link there instead of claiming they don't exist. Document the simplified post-flash workflow: with device-reported versions as source of truth, updating `installed_firmware_version` in `data/devices.json` (and `deployment_version` in the firmware repo's `version.json`) after each flash is now OPTIONAL fallback data, not required upkeep.
- [ ] **Step 2:** Update CLAUDE.md facts: line count, data files list (`rollout-state.json`), scripts list, test command (`python -m unittest discover -s tests -v`), the gitignore/commit reality of `dashboard-data.json` (CI commits it — state whichever is true after checking `.gitignore`).
- [ ] **Step 3:** Add a short "Security note" section to README: the Pages site is public; document that no credentials ship to the browser, and flag repo-visibility as an open decision for the owner.

---

## Self-review notes

- Spec coverage: live data (T1/T3), audit trail (T2/T3), action-first UI (T4), release readiness (T5), rollouts+canary (T6/T7), notifications (T8), hardware identity (T9), docs (T10). Hosting/auth deliberately NOT implemented — owner decision, flagged in T10 docs.
- Type consistency: snake_case in JSON pipeline output (`last_seen`, `reported_version`, `ota_history`), camelCase only inside frontend state — matches existing convention (`pending_ota_version` → `pendingOtaVersion`).
- All tasks runnable/verifiable without API credentials; network code isolated behind `fetch_json`/`post_json`.
