# Plan 02 — UX Quick Wins

Read `plans/00-context.md` first. All changes in `index.html` only (single-file constraint). Independent of plans 01/03 — safe to run before or after, but coordinate on merge conflicts if run in parallel (all three touch index.html).

## Items (in priority order)

### 1. Relative timestamps
Everywhere a date/timestamp renders (last deployed, last updated, last commit date, version timeline dates): show relative form ("3 days ago", "2 h ago") with the absolute value in a `title` tooltip. One shared helper `formatRelativeTime(isoString)`; handle date-only strings (treat as midnight local) and invalid input (return raw string). Keep absolute dates where precision is the point (version timeline can show both: "2026-06-09 · 3 days ago").

### 2. Data freshness banner
Small muted line near the hero/topbar: "Data snapshot: {last_updated, relative}". Source: `last_updated` from dashboard-data.json (already in the payload). If snapshot older than ~2 h, tint it amber — signals the pipeline/CI may have stalled. Don't promise a "next update" time (pipeline is also triggered ad-hoc by deployments).

### 3. Drawer deep-links
- Opening the device drawer sets `location.hash` to `#device=<mac-without-colons>`; closing clears it.
- On load, after data fetch, if hash matches a device → open its drawer (scroll device browser into view first).
- Coexist with the existing "Copy current view" URL feature — inspect how it encodes filters (search the JS for the copy-view handler) and extend rather than clobber. Copying the view while a drawer is open should include the device hash.

### 4. Persist view preferences
localStorage key e.g. `sensor-dashboard-prefs`: view mode (card/list), sort, active status filter, active component filters, section. Restore on load **but** URL params/hash always win over stored prefs (deep links must behave as sent). Debounce writes; wrap localStorage access in try/catch (private-mode safety).

### 5. Skeleton loading
While the initial fetches resolve: shimmer placeholder blocks for the 4 stat cards, doughnut area, and ~6 device-card skeletons. Pure CSS (existing custom-property palette; subtle animated gradient). Replace on render. Also add a visible error state if any fetch fails (currently verify what happens — if it silently breaks, add a centered "Failed to load fleet data" message with a retry button).

### 6. Mobile filter bar
At narrow widths the filter chips wrap awkwardly. Make the chip rows horizontally scrollable (`overflow-x: auto`, `flex-wrap: nowrap`, hidden scrollbar, edge fade-out mask) below the `lg` breakpoint. Verify drawer, table, and topology sections at 380 px width.

### 7. Command palette (Ctrl+K) — do last, skip if time-boxed
Minimal overlay: input + result list. Sources: devices (open drawer), statuses (apply filter), sections (switch), views ("List view", "Card view"). Fuzzy-ish match is fine as simple case-insensitive substring. Keyboard: ↑/↓/Enter/Esc. Reuse drawer backdrop styling. No library.

## Acceptance

- No regressions: filters, sort, search, copy-view, drawer, timeline, chart all work.
- Hash/URL deep links override localStorage prefs.
- Site still renders fine with JS storage unavailable.
- Test at 380 px and 1440 px widths.
- No new external dependencies.
