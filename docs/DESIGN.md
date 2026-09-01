# VOD Pipeline Design System

## 0. Research Log

- Embedded refs: shortlisted `raycast` (desktop utility chrome), `linear.app` (dense ops), `superhuman` (keyboard-first) → picked Layer A `taste-skill` + Layer B `raycast` because this is a Windows Chromium desktop utility, not a SaaS marketing page. `layout-skill` stacked for the app shell.
- Lazyweb: 3 queries (`desktop recording dashboard dark`, `obs studio streaming control panel`, `raycast command palette settings drawer`), 8+6+6 results, 3 screens downloaded and used (Grass Valley playout, Circle live room, Chronicle dark preferences). Grammar taken: tally/clock as the live hero, green/red status chips, hairline rows, settings as a grouped dark drawer. Marketing landings (Restream/Meld/Streamlabs) discarded.
- Imagen drafts: skipped — no image-generation tool in this session.
- ui-ux-pro-max: `desktop recording ops dashboard dark dense` → OLED dark + density 8. Pattern/CTA (“Enterprise Gateway”) rejected as off-domain.
- Skipped lanes: none other.

**Design read:** existing local Chromium recorder dashboard for a single operator, Raycast precision-instrument language, vanilla CSS, OLED dark, live-red punctuation, cockpit density.

**Dials:** VARIANCE 4 / MOTION 3 / DENSITY 8.

**Signature:** the recording strip is a tally light — red edge + mono clock — not a card.

## 1. Atmosphere & Identity

A broadcast control surface that happens to live in a Chromium window. Cursor-dark charcoal (not OLED void), physical chrome (inset highlights, double-ring cards), one loud color: live red. Everything else recedes so status and the clock can be read at a glance. It should feel like a desktop instrument, not a website.

## 2. Color

Dark-only. This is a night-ops desktop app; a light theme is accepted debt (Section 8).

### Palette

| Role | Token | Dark | Usage |
|------|-------|------|-------|
| Surface/void | `--bg` | `#181818` | Page canvas (Cursor editor charcoal, not OLED) |
| Surface/panel | `--panel` | `#1e1e1e` | Cards, header, drawer |
| Surface/elevated | `--panel-2` | `#262626` | Inputs, chips, hover wells |
| Text/primary | `--text` | `#e6e6e6` | Headlines, names, clock |
| Text/secondary | `--muted` | `#9c9c9d` | Captions, hints |
| Text/tertiary | `--faint` | `#6a6b6c` | Disabled, paths |
| Border/default | `--line` | `rgba(255,255,255,0.08)` | Hairlines |
| Border/cool | `--line-solid` | `#333333` | Drawer/modal edges |
| Accent/interactive | `--accent` | `#55b3ff` | Focus, links, selected, primary Save |
| Accent/dim | `--accent-dim` | `hsla(202,100%,67%,0.15)` | Focus ring fill, report chip |
| Status/live | `--live` | `#FF6363` | Recording, stop, tally, live dots |
| Status/live-dim | `--live-dim` | `hsla(0,100%,69%,0.16)` | Live wells |
| Status/ok | `--ok` | `#5fc992` | Complete, connected |
| Status/ok-dim | `--ok-dim` | `hsla(151,59%,59%,0.16)` | Complete wells |
| Status/warn | `--warn` | `#ffbc33` | Armed, running, disk low |
| Status/warn-dim | `--warn-dim` | `hsla(43,100%,60%,0.16)` | Warn wells |
| Status/err | `--err` | `#FF6363` | Errors, stale, disk critical |
| Status/err-dim | `--err-dim` | `hsla(0,100%,69%,0.16)` | Error wells |
| On-accent | `--on-accent` | `#181818` | Text on blue primary |
| On-live | `--on-live` | `#181818` | Text on red record/stop |

### Rules

- Live red is punctuation only: recording state, stop, tally edge. Never decorative.
- Interactive accent is blue. Never reuse the retired violet `#c4b5fd`.
- Accent is never a background wash except `--accent-dim` on a selected/report chip.
- Never introduce a color not in this table.
- A label on a filled button uses the matching `--on-*` token, never a raw
  `#fff`. White on `--live` measures 2.91:1 and fails even the 3:1 non-text
  floor; `--on-live` is 6.10:1. `PaletteContrastTests` in
  `tests/test_ui_contract.py` computes these, so a palette re-picked by eye
  fails the suite instead of shipping.

## 3. Typography

### Scale

| Level | Size | Weight | Line Height | Tracking | Usage |
|-------|------|--------|-------------|----------|-------|
| Clock | 28px | 600 | 1.0 | 0.04em | Live session duration |
| H1 | 15px | 650 | 1.2 | 0 | App title |
| H2 | 12px | 600 | 1.3 | 0.02em | Panel titles |
| Body | 13.5px | 500 | 1.45 | 0.2px | Default |
| Body/sm | 12px | 500 | 1.4 | 0.2px | Meta, hints, disk |
| Caption | 11px | 600 | 1.3 | 0.04em | Pills, pipe chips, labels |
| Overline | 10px | 700 | 1.0 | 0.08em | State stamps (LIVE/REC) |
| Mono | 12.5px | 500 | 1.6 | 0.2px | Paths, ids, reports |

### Font Stack

- Primary: `"Segoe UI", system-ui, sans-serif` — already on the Windows host; no network fonts.
- Mono: `Consolas, "Cascadia Mono", ui-monospace, monospace`.

### Rules

- Two families only.
- Body never below 12px; default 13.5px.
- Numbers that change (clock, disk, chunk ids) use `font-variant-numeric: tabular-nums` and the mono stack.

## 4. Spacing & Layout

### Base Unit

4px. Dense cockpit: prefer `--space-2` / `--space-3` inside rows.

| Token | Value | Usage |
|-------|-------|-------|
| `--space-1` | 4px | Defined for the ramp; no current call site |
| `--space-2` | 8px | Inline groups, form gap |
| `--space-3` | 12px | Header padding, row padding |
| `--space-4` | 16px | Panel padding, drawer padding |
| `--space-5` | 20px | Modal body |
| `--space-6` | 24px | Toast / modal inset |
| `--space-8` | 32px | Unused — do not inflate |

### Grid

- Max content width: 1280px, centered.
- Capture row: `minmax(0, 1.5fr) minmax(260px, 0.75fr)` → one column at 980px.
- Breakpoints: 760px (header wrap), 980px (capture stack).
- Shell: document scroll. Header sticky. Drawer is a `scroll-body-shell` (head / body / actions); body is the only scroll owner (`min-height: 0`).

### Rules

- Hairline separators, not card-in-card.
- Long labels truncate; paths `overflow-wrap: anywhere`.
- 375px must be one readable column, no horizontal scroll of primary content.

## 5. Components

### Button

- **Structure**: `<button>` / `<button class="ghost">` / `<button class="danger">` / `.record` / `.stop`
- **Variants**: primary (blue fill), ghost (hairline), danger (text-only), live (red fill)
- **Spacing**: `--space-2` × `--space-3`
- **States**: hover opacity 0.72 (Raycast), active `scale(0.98)`, focus-visible 2px `--accent` ring, disabled opacity 0.45
- **Accessibility**: native button; visible focus; 32px min height
- **Motion**: 140ms opacity/transform; none under reduced motion
- **Layout**: cluster

### Panel

- **Structure**: `.panel` > `.panel-head` + body
- **Variants**: default, `.collapsed`, `#live-panel.idle`, `#live-panel.active`
- **Spacing**: head `--space-3`/`--space-4`; body flush rows
- **States**: idle live panel is a dashed well, no fake card; active has a 2px `--live` tally on the inline-start edge
- **Accessibility**: `h2` in every head
- **Motion**: none
- **Layout**: stack; live panel is the signature

### Channel / session row

- **Structure**: `.channel` / `.session-head` cluster
- **States**: hover well `--panel-2`; live/rec/armed dots; truncated title
- **Accessibility**: session head is the expand control; chevron communicates open
- **Layout**: cluster

### Pipe chip / status

- **Structure**: `.pipe` / `.status` / `.badge` / `.pill`
- **Variants**: done, running, error, warn, live
- **States**: color + well only (never color-only — well provides the second cue)
- **Layout**: cluster

### Drawer (settings)

- **Structure**: `.drawer` = head + `.drawer-body` + `.drawer-actions` (actions MUST be a sibling of the body)
- **Scroll owner**: `.drawer-body`
- **States**: `[hidden]` wins; Escape and backdrop-click close it. Focus is
  **not** trapped and is not moved into or restored out of the drawer -- see
  Accepted Debt
- **Accessibility**: Close + Escape; Save in the footer
- **Motion**: none (instant show/hide — `[hidden]` contract)
- **Layout**: scroll-body-shell

### Modal / toast / connection

- Modal: imposter overlay, `--panel` box, double-ring
- Toast: fixed bottom cluster
- Connection: `.connecting` / `.connected` / `.stale` — stale uses err well + copy from JS

### Input

- Label above field (settings). Placeholder is not a label on capture forms — those have `aria-label`.
- Focus: blue ring + `--accent-dim` glow
- States: default, focus, disabled

## 6. Motion & Interaction

| Type | Duration | Easing | Usage |
|------|----------|--------|-------|
| Micro | 140ms | ease-out | Button hover/active |
| Pulse | 1.6s | ease-in-out | `.dot.rec` / `.state.rec` only |
| Bar | 600ms | linear | Chunk progress width |

### Rules

- Animate only `transform`, `opacity`, and the progress bar `width` (the bar is informational).
- No decorative hover motion. No scroll-reveal. No idle animation except the rec pulse.
- `@media (prefers-reduced-motion: reduce)` kills the pulse.

## 7. Depth & Surface

**Strategy: mixed** — hairline rings + macOS inset highlights. No drop-shadow fog.

| Level | Treatment | Use |
|-------|-----------|-----|
| 0 void | `--bg`, no shadow | Page |
| 1 panel | `--panel` + `1px solid var(--line)` | Cards, header |
| 2 chrome | inset `rgba(255,255,255,0.06) 0 1px 0` + dark bottom inset | Buttons, keys |
| 3 float | double-ring `0 0 0 1px #333333` + inset `0 0 0 1px #181818` | Drawer, modal |
| 4 tally | 2px solid `--live` on inline-start | Active live panel |

Warm glow from Raycast marketing is **not** used — this is an ops surface.

## 8. Accessibility Constraints & Accepted Debt

### Constraints

- WCAG 2.2 AA: body contrast ≥ 4.5:1, UI chrome ≥ 3:1, visible focus on every control, full keyboard reachability, `prefers-reduced-motion` honored.
- `[hidden] { display: none !important }` is load-bearing — never remove it.
- No emoji icons. Status uses color **and** a well/label.

### Accepted Debt

| Item | Location | Why accepted | Owner / Exit |
|------|----------|--------------|--------------|
| Dark-only | whole UI | Night-ops desktop app; no light users | Add light tokens if a second operator asks |
| No primitive showcase page | n/a | Single screen; showcase would be a second unused HTML file | Treat the dashboard itself as the harness |
| System fonts, not Inter | `:root --font` | Offline Windows host; no Google Fonts | Keep |
| Single `style.css` > 250 LOC | `vodpipe/static/style.css` | One page, one sheet; splitting adds a request for no seam | Split only if a second surface appears |
| No React / no icon package | static/ | Stdlib-only pipeline; no npm | Keep geometric CSS dots/chevrons |
| Security team spawn failed | n/a | `team_create` lineage error; hunters timed out | Re-run `/security-review` in a fresh session |
| No focus trap on the drawer/modal | `static/app.js` | Single-operator localhost app; Escape, backdrop-click and Close all dismiss, and the page behind stays keyboard-usable. Claimed as implemented until the 2026-08-25 audit; it never was | Add trap + focus restore if the drawer grows beyond settings |
| Spacing only partly tokenised | `vodpipe/static/style.css` | The restyle moved the structural values onto `--space-*`, but ~70 padding/margin/gap declarations are still literals off the 4px scale (`6px`, `10px`, `14px`, `18px`). They are the pre-existing dense-row rhythm and re-tokenising them is churn with no visual change | Convert opportunistically when a rule is edited for another reason |
| Input/ghost-button borders below 3:1 | `--line` | `rgba(255,255,255,.08)` on `--panel` is ~1.3:1, so an unfocused field is delimited by little more than its fill. Pre-existing (the retired palette was 1.2:1), and every such control is labelled and reachable | Raise `--line` toward `--faint` if a second operator reports missing a field |
