# Plan 04 — Showcase Polish (animations + fleet pulse)

Read `plans/00-context.md` first. Run AFTER plans 01–03 (builds on their fields/UI; all touch `index.html`).

## Goal

Make the dashboard feel alive and satisfying for daily internal use — state-of-the-art static-site feel without frameworks. Everything must respect `prefers-reduced-motion` (wrap all non-essential motion in a media query check; reduced-motion users get instant final states).

## Items

### 1. Count-up stat numbers
The 4 hero stat cards animate from 0 → value on first render (~700 ms, ease-out, requestAnimationFrame helper `animateCount(el, target)`). Re-trigger when section switches. Integers only; keep suffixes ("%") static.

### 2. Chart entrance choreography
- Doughnut: sweep-in rotation animation on first render (Chart.js `animation: { animateRotate: true }` — verify current config, likely just needs enabling/tuning ~900 ms with easeOutQuart).
- Migration chart (plan 03): staggered dataset rise — Chart.js per-dataset `delay` so version bands cascade in oldest→newest. Reading order reinforces the migration story.
- Center text of doughnut uses the count-up helper.

### 3. Card/list entrance stagger
Existing `fade-in`/`delay-*` classes cover the hero. Extend to device cards: stagger by index (cap total delay ~600 ms regardless of count — `min(index * 40, 600)ms` via inline style or CSS custom property `--stagger-i`). Re-run on filter changes but keep it subtle there (shorter, no big translate) so filtering feels snappy, not sluggish.

### 4. Fleet pulse grid
New compact visualization — put it in the Fleet readiness section beside/under the doughnut, or as a third view-mode toggle in the device browser (pick whichever fits the layout best; prefer Fleet readiness so it's always visible).

- Each device = small rounded square/dot node in a tight grid, color = status (reuse status palette: gold/green up-to-date, amber patch, red needs update, grey unknown).
- Breathing glow animation (scale 1→1.06 + soft box-shadow pulse, ~3 s loop, randomized per-node `animation-delay` so the grid shimmers organically instead of blinking in sync).
- If `last_seen` exists (plan 01): nodes silent >24 h don't breathe — static and dimmed. Reads instantly as "this one's not alive". If last_seen absent, all nodes breathe.
- Hover: node lifts (scale + shadow), tooltip with name/version/status. Click: opens existing device drawer.
- Pure CSS animation + minimal JS for render/tooltip/click. No canvas needed at this fleet size (~18 nodes).

### 5. Micro-interactions
- Status pills: subtle background shift on hover.
- Filter chips: active chip gets a small spring (one-shot `transform: scale` keyframe ~200 ms).
- Drawer: ease the slide-in with slight overshoot (cubic-bezier(0.34, 1.3, 0.64, 1)), backdrop blur if not already (`backdrop-filter: blur(4px)`, with graceful fallback).
- Buttons/cards already have hover translateY — keep, ensure consistent (same distance + duration everywhere; audit and unify).
- "Live Fleet" indicator: keep pulse-dot; tie its label to the freshness banner state from plan 02 — if snapshot >2 h old, dot turns amber and stops pulsing ("Stale snapshot"). Honest liveness.

### 6. Update-plan panel reveal (from plan 01)
When buckets render, list rows cascade in (same stagger pattern as cards). "Fleet converged — nothing to do" empty state gets a small drawn-in checkmark (SVG stroke-dashoffset animation, ~600 ms). Best screenshot in the repo.

### 7. Performance guardrails
- Animate only `transform`/`opacity` (compositor-friendly); no layout-thrashing properties in loops.
- All entrance animations once per render, not on scroll.
- Total JS added should be small (<~150 lines); prefer CSS.

## Acceptance

- `prefers-reduced-motion: reduce` → no continuous or entrance motion, all content immediately visible.
- Filtering/sorting still feels instant (entrance stagger ≤ ~250 ms total on re-filter).
- No jank at 380 px mobile width.
- Fleet pulse nodes open the correct drawer; tooltips escaped via `escapeHtml()`.
- No new dependencies.
