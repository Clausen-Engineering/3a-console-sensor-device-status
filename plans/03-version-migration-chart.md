# Plan 03 — Version Migration Stream Chart

Read `plans/00-context.md` first.

## Goal

Animated-feel (but screenshot-first) stacked area chart showing the fleet migrating across firmware versions over time — e.g. devices flowing from v3.19 → v3.20 → v3.21 as OTA rolls out. This is the portfolio centerpiece: it visualizes "we ship firmware, the fleet converges automatically".

## Part A — history data

### 1. New file: `data/version-history.json`
```json
{
  "snapshots": [
    { "date": "2026-06-12", "sections": { "sensor-hub": { "v3.20.0": 13, "v3.19.0": 2, "": 1 } } }
  ]
}
```
Empty-string key = not deployed/unknown. Keep it small: one snapshot per calendar day per section.

### 2. Pipeline change (`scripts/build_dashboard_data.py`)
After building device summaries: aggregate per-section version counts, load `data/version-history.json` (create if missing), upsert today's snapshot (replace if a snapshot for today exists — pipeline runs many times a day), append otherwise, write back sorted by date. Cap retention at ~730 snapshots. Stdlib only.

### 3. Backfill from git history (one-time, agent does it during this task)
`data/dashboard-data.json` is committed on every data update (verify: `git log --oneline -- data/dashboard-data.json`). Backfill script (one-off, can live in `scripts/backfill_version_history.py`, fine to keep in repo):
- `git log --format='%H %cs' -- data/dashboard-data.json`
- For the **last commit of each day**: `git show <hash>:data/dashboard-data.json`, parse, count devices per version per section, emit snapshot.
- Merge into `data/version-history.json`. Run it, commit the result.
If the file turns out not to be in git history, skip backfill — chart starts accumulating from today (handle gracefully).

### 4. CI
No workflow change needed if the existing commit step uses `git add -A` or adds the data dir — verify `.github/workflows/update-dashboard-data.yml` stages `data/version-history.json`; adjust the `git add` if it lists files explicitly. Also verify `.gitignore` doesn't exclude it.

## Part B — frontend chart

### Placement
Inside the Version Timeline section ("Release intelligence"), above the existing release cards — chart + timeline together tell the full story.

### Rendering
- Chart.js is already loaded (doughnut chart exists) — use a stacked filled line chart (`type: 'line'`, `fill: true`, `stacked: true` on y-axis) over snapshot dates for the active section.
- One dataset per version, ordered oldest→newest so newest stacks on top. Y = device count.
- Colors: derive a ramp from the existing CSS custom-property palette — older versions in muted browns/greys, latest version in the gold accent, so the eye reads "fleet converging to gold". Read computed CSS variables in JS like the doughnut likely does (check existing chart code for the pattern).
- Smooth curves (`tension: ~0.35`), no point markers except on hover, subtle grid, JetBrains Mono tick labels to match theme.
- Tooltip: date + per-version counts (skip zero rows).
- Limit datasets: group versions older than the 6 most recent into a single "older" band to avoid legend soup.
- Section switcher must update the chart (hook the same path that re-renders other section-scoped widgets).
- Empty/sparse state (≤1 snapshot): hide the chart, show muted "History accumulating — chart appears after a few days of snapshots".
- Entrance: enable Chart.js default rise animation on first render — looks good live, costs nothing, and the final frame is the screenshot.

### Stat tie-in
Optional small caption under the chart: "Latest release coverage: N of M devices" — reuse the existing coverage stat computation.

## Acceptance

- Pipeline idempotent: two runs on the same day produce one snapshot for that day.
- Backfill produces plausible history (spot-check a couple of dates against `git show`).
- Chart renders with real data, handles empty history, switches with sections.
- Doughnut chart untouched and still working.
- No new dependencies; stdlib-only Python.

## Out of scope

- OTA event-level rollout tracking (dashboard stays snapshot-based).
- Plans 01/02 features.
