# Helixis — Design System & Anti-"Vibe-Coded" Rules

The enforceable design spec for Helixis. Read this before any UI change.
Every rule is written to be **falsifiable** (checkable against computed styles,
not taste). When unsure what "looks better" means, choose the **more restrained**
option — restraint is the strategy (Linear / Ramp / Stripe / Mercury all win by
removing decoration, not adding it).

Source of truth for values: `frontend/src/styles/globals.css` (`@theme` block →
Tailwind utilities; `:root` block → inline-style tokens). This doc is the
rationale + the rules; the CSS is the values. Keep them in sync.

## Theme

Dark-themed by default (deep neutral canvas, light ink). "White-default" in older
notes means **white/near-white text**, not a white background. A light theme is a
possible future pass, not current.

## Color tokens (never hardcode hex or raw Tailwind palette classes)

Everything resolves through tokens. Never write `bg-indigo-500`, `text-slate-400`,
`#4ade80`, etc. in a component — use the token.

- **Accent — `--iris` `#1957A0`**, the same blue as occupella.com (hover
  `--iris-hover` `#144A88`, press `--iris-press` `#0F3A6B`, both darker; tint
  `--iris-soft`, ring `--iris-ring`). Use it **sparingly**:
  primary buttons, active nav, focus rings, links, selected-state tints. **Never**
  as a gradient, a background wash, a glow, or on two competing elements in one view.
- **Neutrals** carry a faint blue bias toward the brand hue (canvas `--canvas`
  ramp, ink `--ink`/`--ink-muted`/`--ink-subtle`/`--ink-faint`, hairlines
  `--line`/`--line-strong`, card edges `--card-edge`). Chosen greys, not cool slate.
- **Semantic status — `--positive` / `--caution` / `--danger` / `--info`** (muted,
  not neon). Reserved **strictly** for a real state (payment, lease, urgency,
  validation). Never a decorative accent, a chart colour for non-status data, or a
  default icon colour.
- Never pure black/white for body text — use the ink tokens.

## Typography

- The site's faces, self-hosted via `@fontsource` (imported in `main.tsx`), no font CDN:
  **IBM Plex Sans** (`--font-sans`) for all UI text; **Fraunces** (`--font-display`)
  for page titles and empty-state headings only; **IBM Plex Mono** (`--font-mono`)
  for IDs, amounts and code only, never for labels. The wordmark is lowercase
  "occupella" in Fraunces (`Wordmark.tsx`).
- **Scale: 12 / 13 / 14 / 16 / 20 / 28px. Nothing below 12px.** Guarded by
  `src/designTokens.test.ts` and `tests/unit/test_frontend_design_tokens.py`.
- Weights 400–600; **never below 400**; headings 500–600, not 700+.
- `font-variant-numeric: tabular-nums` is set on `body` app-wide — this is a
  data-dense ops tool, so digits align and don't shift on update. Keep it.

## Radius (graduated — never one flat value everywhere)

`--r-xs 4` (chips/checkboxes) · `--r-sm 6` (buttons/inputs) · `--r-md 8`
(cards/panels/popovers) · `--r-lg 8` (dialogs; was 12) · `--r-pill 9999`
(badges/toggles/avatars, **never a button**).

## Buttons

`Button.tsx`: primary solid `--iris`; secondary white with a `--line-strong` border;
tertiary (`ghost`) a text link. Text buttons are 36–40px tall. Disabled is gray
(`--disabled-bg` / `--disabled-ink`), not a faded colour. No "→" and no "&" in labels.
Never 16px+ on standard cards/buttons (reads consumer-toy).

## Elevation

Static cards/panels = **1px border (`--line`/`--card-edge`), not a shadow**.
Shadows (`--shadow-sm/md/lg`) are for elements that genuinely float: dropdowns,
popovers, modals, toasts. Never a coloured glow / coloured box-shadow.

## Motion (frequency-based)

- Frequent, low-novelty (sort a column, hover, expand a row, open a dropdown):
  0–120ms or none.
- Occasional (modal / drawer / toast): 150–300ms, `ease-out` entrances. **Never
  `ease-in`** for UI.
- Rare / first-run: up to ~400ms.
- No looping/decorative background motion (no orbs, pulsing gradients, aurora).
- Popovers scale from their trigger origin; modals from centre. Never animate in
  from `scale(0)` — start ~`0.95`.
- `--ease` `--dur-fast/base/slow` are the tokens; respect `prefers-reduced-motion`.

## Forbidden (eliminate on sight)

- Any **brand gradient** (purple→blue/pink), anywhere. The composer border, hero,
  and auth glows were flattened — keep them flat.
- **Coloured left/top-border accent bars on cards** — the single most reliable
  AI-generated tell. (Neutral 1px hairlines are fine.)
- **Glassmorphism / `backdrop-blur`** on content cards/panels. (Window chrome like
  the title bar is the one allowed exception.)
- Neon status colours; raw Tailwind palette classes; unmodified shadcn/library
  defaults; **sparkle icons / "AI-powered" badges** on AI features; emoji in nav /
  icons / section headers; gradient-filled buttons.

## AI surfaces

Show the **output + provenance**, not a badge. Plain verbs ("Extracted,"
"Suggested," "Matched," "Drafted") + a small textual source note ("Extracted from
lease document"). No sparkle, no glow, no pulsing. Every AI-suggested value must be
as trivially editable as a manual one.

**Generative actions disappear once the user moves on.** A button the assistant
generated for one moment (a next step, a planner chip, a form asking for the
blanks) goes away as soon as the user takes their next action: clicking one of
them or typing a prompt. Stale offers left in the transcript still look
clickable. Enforced in `ConversationThread` (`SPENT_AFTER_USER_TURN`), which
both chat and the Inbox render through; a new generative card joins that set.
Confirm gates are exempt: they answer a write the backend is holding.

## Copy voice

Operator-to-operator, not growth-marketer. Name the specific mechanism + falsifiable
outcome, not adjectives. Ban: *supercharge, 10x, unlock, elevate, leverage, seamless,
robust, cutting-edge, world-class, best-in-class, "AI-powered" (standalone),
enterprise-grade (unsupported), intuitive (unsupported)*. Transactional/empty-state
copy is calm, no exclamation points ("No overdue balances", not "You're all caught up! 🎉").

## Legal safety

Fair-housing is a hard rail, not a style choice: the universal federal reference
(`company_knowledge/federal_fair_housing.py`) is injected into chat + CRM drafts.
Any leasing/screening/advertising surface must treat everyone identically and never
express a preference tied to a protected class. Info, not legal advice → cite →
defer to state/local + counsel.

## Known thin spots (honest)

The report's deep **numeric-table** rules (right-aligned currency columns, density
toggle, sticky first column, hover-revealed row actions) apply to the financial
rent-roll / ledger tables that **don't exist yet** — they arrive with the live
Buildium key + the property cockpit. Today's tables are the Properties assignment
grid (no numeric columns; sticky header + 1px dividers + row hover already) and CRM
lead lists. Revisit table density/alignment when the financial tables land.
