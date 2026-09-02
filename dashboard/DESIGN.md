---
name: Commodity-FX Volatility Monitor
description: A departures board for commodities, live reranking, one amber accent, mono figures.
colors:
  board-bg: "#12141a"
  panel-bg: "#1a1d26"
  panel-bg-alt: "#20232f"
  seam: "#2b2f3d"
  seam-soft: "#22252f"
  text: "#EAE7DC"
  muted: "#7d8394"
  accent: "#F2A93B"
  green: "#34D399"
  red: "#FB5B6E"
  blue: "#4EA1F7"
typography:
  display:
    fontFamily: "'Big Shoulders Display', sans-serif"
    fontWeight: 700
    letterSpacing: "0.04em"
  body:
    fontFamily: "'Public Sans', sans-serif"
    fontWeight: 400
  mono:
    fontFamily: "'Martian Mono', monospace"
    fontWeight: 500
rounded:
  none: "0px"
  sm: "3px"
  md: "4px"
components:
  board-refresh-btn:
    backgroundColor: "transparent"
    textColor: "{colors.muted}"
    rounded: "{rounded.sm}"
    padding: "5px 12px"
  flip-tile:
    backgroundColor: "{colors.panel-bg}"
    rounded: "{rounded.md}"
    padding: "10px 12px"
  flip-tile-alert:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.text}"
    rounded: "{rounded.md}"
    padding: "10px 12px"
---

# Design System: Commodity-FX Volatility Monitor

## Overview

**Creative North Star: "The Departures Board"**

This dashboard tracks nine commodities the way an airport tracks flights: a live board that reranks by what's urgent, holds a changed row lit until it's noticed, and reads its detail views like a segmented boarding pass. It exists to prove two things at once, on equal footing, that the underlying data fusion (price volatility → country exposure → trade flows → geopolitical risk) is real and connected, and that the surface itself has the craft of a shipped product, not a chart-library demo.

The world is deliberately *not* a literal Bloomberg-terminal skin (amber-on-black monospace, function-key rows), that reading was named and set aside as the category's own rut. Instead it draws on the adjacent, less-expected half of the same trading-infrastructure lineage: the physical information board. Split-flap and gate-listing boards are themselves the analog ancestor of the digital ticker, so the world stays inside the user's pinned "Bloomberg/trading-terminal lineage" brief without repeating its most obvious costume.

Register is Operate, not Persuade: scanability and native affordances (Bootstrap grid, standard form controls) outrank expression everywhere expression and clarity would conflict. The brief's hard limit, "keep it feeling serious/professional", rules out anything playful, gamified, or consumer-casual; amber is reserved for one job (chrome and alerts) and never spent as decoration.

**Key Characteristics:**
- Near-black board surface, off-white flap-card text, one reserved amber accent, everything else is semantic (green/red for gains/losses, blue for informational/exporter data), never decorative.
- Condensed, all-caps signage type for every label and header; monospace tabular figures for every number, price, and date, because a physical board's characters are fixed-width, not because "mono reads as technical."
- The board actually reranks: summary tiles sort breached-threshold commodities to the front on every refresh, then by descending volatility. This is the one non-negotiable behavior the chosen direction was named for.
- Flat, unglazed materiality, no faked bevels, embossing, or glass. The one physical cue is a hairline seam across each tile's midline (the flap fold) and a shallow inset frame around the whole board.

## Colors

Warm-neutral near-black ground, off-white flap text, one reserved amber for chrome and alerts. Everything else is a semantic color that encodes a fact (direction, information type), never a brand decoration.

### Primary
- **Board Amber** (`#F2A93B`): the one reserved accent. Active tab underline, the live-status dot's pulse ring, alert-tile wash, threshold-breach figures, the header alert banner, focus rings. Never used for anything that isn't chrome or an alert.

### Secondary (semantic, not decorative)
- **Signal Green** (`#34D399`): gains, net-exporter positions, "OK" status.
- **Signal Red** (`#FB5B6E`): losses, net-importer positions, danger/threshold-triggered rows.
- **Informational Blue** (`#4EA1F7`): neutral data that isn't a gain/loss/alert, exporter flow nodes in the Sankey, FX-correlation reference lines, loading spinners on informational panels.

### Neutral
- **Board** (`#12141a`): page background, the board surface itself.
- **Panel** (`#1a1d26`): tile, card, and dropdown surfaces, one step up from the board.
- **Panel Alt** (`#20232f`): a further step up, used sparingly for nested surfaces (e.g. dropdown search field backgrounds).
- **Seam** (`#2b2f3d`): hairline borders between tiles, table rows, and panel edges, the board's own seam lines, never a decorative stroke.
- **Seam Soft** (`#22252f`): the fainter mid-tile flap-fold line and secondary table dividers.
- **Flap Text** (`#EAE7DC`): primary text, a warm off-white, like flap-card stock, not clinical white.
- **Muted** (`#7d8394`): secondary text, labels, placeholder copy.

### Named Rules
**The One Accent Rule.** Amber is spent on exactly one job, board chrome and alerts. If a new element needs a highlight color and it isn't chrome or a threshold breach, it does not get amber; find the semantic color that actually matches the fact being shown (green/red/blue), or leave it neutral.

## Typography

**Display Font:** Big Shoulders Display (condensed, 700–900 weight), with a sans-serif fallback
**Body Font:** Public Sans, with a sans-serif fallback
**Mono/Label Font:** Martian Mono, with a monospace fallback

**Character:** Condensed industrial signage (Big Shoulders) for anything that names or labels a region of the board, headers, tab strip, tile labels, table headers, paired with a fixed-width counter face (Martian Mono) for every number, price, date, and figure, because a physical board's characters are fixed-width units, not a "technical" affectation. Public Sans carries ordinary prose (news titles, captions, dropdown option text) where a display or mono face would be too loud.

### Hierarchy
- **Display** (800, ~1.5rem, uppercase, 0.02em tracking): the board title in the header.
- **Headline** (700, uppercase, 0.04em tracking): every `h4`/`h5`/`h6`, section and card headings read as board signage throughout, including proper nouns like country names.
- **Label** (700, 0.66–0.78rem, uppercase, 0.05–0.06em tracking): tile labels, tab strip, table column headers, form labels.
- **Figure** (mono, tabular numerals): every price, percentage, HV reading, date, and table cell, `.table td`, `.tile-price`, `.tile-return`, `.tile-hv`, chart hover text.
- **Body** (Public Sans, 400, ~0.85rem): news article titles, dropdown option text, longer captions.

### Named Rules
**The Fixed-Width Figures Rule.** Any value a user reads as data (a price, a percentage, a date, a score) renders in Martian Mono with tabular figures. Any value that is a label or a heading renders in Big Shoulders Display, uppercase. Nothing renders in the body sans except ordinary prose.

## Layout

Built on a Bootstrap 5 fluid container and 12-column grid (via dash-bootstrap-components), not a custom grid system, Operate mode favors native, predictable reflow over a bespoke layout grammar. The nine summary tiles use `xs=6, sm=6, md=4`, which not only reflows cleanly (2-up on phone, 3-up on desktop) but happens to land 3-per-row on desktop, matching how a reranked board naturally clusters. Tabs read as a horizontal "gate strip" (`nav-tabs`), condensed caps with an amber underline on the active tab, dimmed on hover otherwise. Tables use `responsive=True` (horizontal scroll under the fold on narrow viewports) rather than collapsing columns, a wide data table earns a swipe before it earns column loss.

## Elevation & Depth

Mostly flat and tonal, not shadow-driven: depth comes from stepping between the three background tones (board → panel → panel-alt), not from drop shadows. The one shadow in the system sits on the flip tiles themselves (a soft, offset shadow under each tile, `0 6px 16px -8px rgba(0,0,0,0.55)`) to lift them slightly off the board, legible depth, not decoration. The whole dashboard additionally sits inside a shallow inset frame (`box-frame`), a 1px seam-colored inset outline plus a soft inward vignette, so the surface reads as mounted in a cabinet rather than floating loose on the page background.

### Shadow Vocabulary
- **Tile lift** (`box-shadow: 0 6px 16px -8px rgba(0,0,0,0.55)`): every flip tile, to separate it from the board surface.
- **Board frame** (`box-shadow: inset 0 0 0 1px var(--seam), inset 0 0 40px rgba(0,0,0,0.4)`): the outer `.board-frame` container, once, for the whole dashboard.

### Named Rules
**The No Floating Panel Rule.** A panel gets depth from the tonal step (board → panel) plus, at most, the one tile-lift shadow. It never gets a second shadow, a colored glow, or a border-plus-shadow combination, that reads as AI-generated gloss, not board material.

## Shapes

Mostly square, blocky, and unrounded, the board's own material logic, not a minimalist default. Tiles and panels carry a light 3–4px radius (just enough to soften a rectangle, not enough to feel soft). Tables, progress meters, and the Sankey/heatmap chrome are deliberately un-rounded (`border-radius: 0`), the segmented progress meters render as a row of discrete blocky LED-style segments (`repeating-linear-gradient`) rather than a smooth rounded pill, matching an instrument-panel level readout rather than a consumer progress bar.

### Named Rules
**The Blocky Data Rule.** Anything that encodes a quantity (a progress meter, a table cell, a chart axis) stays square-edged. Rounding is reserved for containers (tiles, cards, dropdowns), never for the data readout itself.

## Components

### Buttons
- **Shape:** 3px radius, 1px seam-colored border, transparent background.
- **Refresh (only button in the system):** ghost/ondark treatment, muted mono label, transparent fill, border and text both shift to amber on hover. No filled/solid button variant exists yet; if one is needed, it should fill with the amber accent and use board-bg text, reserved for a genuinely primary action.

### Flip Tiles (signature component)
- **Shape:** 4px radius, 1px seam border, a `::after` hairline across the vertical midpoint (the flap fold).
- **Background:** panel surface by default; an amber-tinted gradient wash (`flip-tile--alert`) when the commodity's HV30 breaches its threshold.
- **Behavior:** the board reranks on every refresh, breached tiles sort to the front, then by descending HV30, and each tile is React-keyed on its live price/HV so a refresh remounts (not patches) the tile, replaying its mount animation (`tileFlip`, a brief `rotateX` flap-down) every time a value actually changes, not just on first paint.
- **Contents:** label (display face, uppercase, muted) → price (mono, bold) → return with an authored CSS triangle/dot indicator (never a unicode glyph) → HV30 reading (mono, amber if breached).

### Tables
- **Style:** transparent background over the panel/board tone, seam-colored hairlines between rows (no zebra striping), display-face uppercase column headers, mono tabular figures in every data cell.
- **Alert state:** a triggered row gets an amber-tinted background wash plus a one-time `row-flash` entrance animation, never a colored left/right border stripe.

### Alerts (banner)
- **Style:** amber-only (`color="warning"` + `.board-alert`), never the library's default green, a breach is never allowed to render in the "gain" hue.
- **Contents:** a bold amber label ("GATE CHANGE, …") followed by the plain triggered-item list.

### Badges
- **Shape:** 2px radius, mono font, no pill rounding.
- **Color:** amber = alert, green = OK/positive, red = danger, matches the semantic system exactly, never an independent badge palette.

### Segmented Meters (replaces progress bars)
- **Style:** a row of small blocky segments via `repeating-linear-gradient`, colored green/amber/red by the value's own severity mapping (`risk_color()` in `data/political_risk.py`), an instrument-panel level readout, not a smooth rounded pill.

### Dropdowns / Sliders / Checklists
- **Style:** themed through Dash's own native `--Dash-*` design tokens (retuned in `:root` from their light-mode defaults to the board's dark surface, `--Dash-Fill-Inverse-Strong` → panel, `--Dash-Fill-Interactive-Strong` → amber, `--Dash-Text-*` → flap-text opacities) rather than per-component CSS overrides. This is the load-bearing mechanism: Dash's newer dropdown/slider components ship no legacy `react-select`-style classes to target directly.

### Navigation (tab strip)
- **Style:** `nav-tabs`, condensed uppercase display face, no background fill, amber underline + amber text on the active tab, seam-colored underline on hover, muted otherwise.

## Do's and Don'ts

### Do:
- **Do** keep amber reserved for board chrome and alerts only (see The One Accent Rule), check every new highlight against "is this chrome or a breach?" before reaching for it.
- **Do** render every number in Martian Mono with tabular figures, and every label/heading in Big Shoulders Display uppercase, never mix the two roles.
- **Do** author new status/trend indicators as CSS-drawn shapes (`.icon-tri`, `.icon-dot`) or genuine SVG, matching the existing single-stroke, single-weight system, never a unicode arrow or emoji.
- **Do** keep tables and progress/level readouts square-edged (The Blocky Data Rule), reserving rounding for containers.
- **Do** theme new Dash-native components (any future `dcc.*` control) through the `--Dash-*` custom-property overrides in `:root` first; only add component-specific CSS for gaps those tokens don't cover.

### Don't:
- **Don't** reach for a second shadow, a colored glow, or a border-plus-shadow combination on any panel (The No Floating Panel Rule), the tonal step between board/panel/panel-alt is the depth system.
- **Don't** default a `dbc.Alert` without an explicit `color=`, the library's default is green, which will silently paint an alert or warning in the "gain" semantic color.
- **Don't** let an arbitrary categorical color (e.g. a commodity's line color in a multi-series chart) reuse one of the four semantic hues (accent/green/red/blue), those hues carry meaning everywhere else in the system, and reusing one for an unrelated category creates a false signal.
- **Don't** literalize the "Bloomberg terminal" reference directly (amber/green monospace on black with a function-key strip), that reading was deliberately set aside as the category's rut; the board-and-gate-listing vocabulary is the chosen alternative within the same trading-infrastructure lineage.
