# PartnerConsole — approved visual contract

Reference: `references/04-Design-system/39-design-system.png` plus the other 38 boards. Quiet Luxury × Modern Fintech. Persian-first, RTL, B2B operational console for administrators and resellers. Professional, calm, precise and comfortable for long daily use. The reference boards are the visual target, not application assets. This document is a proposed repository-root DESIGN.md; reconcile with any current repository instructions/verified UI requirements.

## Design identity

- Signature: deep-jade wallet card with very subtle layered monochrome arc motif and a small understated PartnerConsole wordmark; contrast with the warm ivory main canvas. Only use this hero where the reference calls for it (partner dashboard/financial profile), not on every page. The two diagonally arranged rounded capsule/petal logo shapes are consistent across all screens. Use local vector artwork, simple and crisp; no gradients outside the subtle tonal wallet treatment unless shown by exact board.
- Use actual reference spacing and shapes. The product is not a landing page. Do not add 3D illustrations, huge hero headlines, colored icon backgrounds for everything, gradients on buttons, glass, dashboard charts, random visual effects, large shadow stacks, thick pill tabs, giant cards, duplicate welcome text or irrelevant data.
- Content hierarchy: context breadcrumb (where needed), title, short supporting line when useful, one primary CTA, core task, optional secondary paths. Do not repeat the title in a header and again in the body. Leave intentional blank space after completed content.

## Canonical color tokens

Exact swatches from board 39 and flat-color pixel measurements on the 4K boards. Semantic extensions below are based on repeated pixels/reference colors; accessibility takes precedence over a literal weak contrast.

| Token | Hex | Purpose |
|---|---|---|
| `--pc-brand` | `#07584F` | Primary action, active text, primary mark |
| `--pc-brand-deep` | `#063C36` | Deep jade / hero accent / stronger state |
| `--pc-bg` | `#F7F4EF` | Warm ivory application canvas |
| `--pc-surface` | `#FFFEFC` | Cards and controls |
| `--pc-shell` | `#FAF8F4` | Sidebar and header |
| `--pc-text` | `#162725` | Main text |
| `--pc-heading` | `#172925` | High-emphasis headings |
| `--pc-muted` | `#64736B` | Muted text; increase contrast if needed |
| `--pc-secondary` | `#465B52` | Secondary labels |
| `--pc-border` | `#E3E3DC` | Quiet card/divider edge |
| `--pc-control-border` | `#D9DDD5` | Inputs; strengthen where needed for WCAG |
| `--pc-nav-active` | `#E1EEE6` | Active nav background |
| `--pc-icon-tint` | `#EDF2EB` | Icon tile/background when reference uses it |
| `--pc-info-bg` | `#EDF3EE` | Neutral info surface |
| `--pc-success-bg` | `#E6F0E8` | Valid/success surface |
| `--pc-warning-bg` | `#F8EEE0` | Warning / attention surface |
| `--pc-danger-bg` | `#F7E9E6` | Destructive/blocked tint |

Success foreground suggested #216447, warning #8C4F12, danger #A83232. Verify contrast against actual backgrounds and use text+icon, never color alone. White/off-white on deep jade. Keyboard focus uses a clear jade outline with separation. Disabled states remain readable; not invisible. Brand and status colors must retain semantic meaning; muted green does not mean success. Keep one token source, with CSS custom properties and Tailwind aliases rather than raw copies across each page. Derive hover/pressed variants from tokens (not a second palette). Color values of all flat fills should match reference exactly where possible.

## Geometry, type and rhythm

Source: exact board labels show desktop 1384×910 and mobile 386×890. Board rendered at 4K; measurements below are inferred CSS-pixel targets, not invented original design-source metadata. At matching viewport visually calibrate against PNGs. The board margins, rounded device shell, canvas background and presentation shadow are **not product UI**.

- Desktop sidebar approximately 230px (fixed at inline-start = right), topbar approximately 64px, content insets ~36px desktop and ~16px mobile. Desktop sidebar wordmark at top, 1px divider, quiet role text, nav rows and profile/logout at bottom. Mobile app header compact (~56–60px); use bottom navigation (~64px plus safe-area) and menu/more for role-specific routes. Do not render fake status bar or home indicator. Collapse sidebar at appropriate width (start around 1024px, verify tablet/RTL and avoid cramped content). Keep content naturally scrollable; never lock document overflow for long forms.
- Spacing scale: 4, 8, 12, 16, 20, 24, 32, 36, 40, 48px. Primary page sections 16–24px apart, comfortable header breathing room. Consistent cards ~16–24px internal padding, small lists ~12–16px. Use logical inline/block properties.
- Corners: controls ~8px; surface cards 12px; wallet hero 14–16px; tiny status pills only if semantic. Thin 1px borders, near-zero ambient shadows for normal cards; subtle only when reference shows elevation or overlays. No shadows on every row; row dividers supply structure.
- **Mandatory unchanged typeface: existing self-hosted Vazirmatn (وزیرمتن).** Keep the original `public/fonts/vazirmatn/` assets byte-for-byte, existing @font-face sources, unicode ranges, 400/500/700 weights and existing fallback order `Vazirmatn, Tahoma, sans-serif`. Do not add/replace/remove/subset/re-encode fonts, use Noto Sans Arabic, alter font dependencies, or fetch external fonts. The boards were rendered with a different font; match their hierarchy through size, line-height, existing weights and spacing only. The explicit user font requirement supersedes the typeface shown in every reference image. Preserve RTL Persian shaping, correct Persian digits (not Arabic-Indic ٠١٢), natural line heights, no letter-spacing on Persian. Use regular/medium/bold weights actually supplied; avoid synthesizing 650/750 if unsupported. Suggested desktop heading 28–30px/1.5 weight 700, mobile 23–25px/1.5; section heading 16–18px; body and labels 13–15px/1.75; captions 12–13px (not too low contrast); financial hero 34–40px desktop / 26–32px mobile; other KPIs 26–30px; responsive values for long amounts. Prefer actual PNG proportions rather than blindly using this range. Use tabular numerals if supported, consistent amount alignment, nonbreaking unit where practical, semantic `<bdi dir="ltr">` for Latin usernames/URLs/date and signed numbers.
- Icons: single cohesive 18–20px stroke family (approx. 1.6–1.8px strokes). Use house/dashboard, server, box, users, layers, wallet, list/receipt, clock, plus, chevrons, copy etc. based on reference and purpose; never Unicode pseudo-icons or emoji. Icon tiles compact ~34–36px, restrained pale jade where present. RTL chevrons lead left to next/detail; back leads right; avoid incorrectly flipping universal glyphs. Icons decorative when redundant; icon-only controls have accessible name and tooltip.

## Shell and navigation

Admin sidebar in order: داشبورد، پنل‌ها، محصولات، همکاران، اشتراک‌ها، دفترکل، عملیات ناتمام. Role-specific profile and logout; no unsupported settings, global search, notification bell or status. Partner sidebar: داشبورد، اشتراک‌ها، محصولات، حساب مالی (ledger destination); do not expose admin links. Bottom nav: partner 4 matching sections; admin dashboard/partners/subscriptions/more (remaining destinations in accessible menu). Active route shown by a pale-jade rounded row and subtle inline-start edge in the reference. Use `aria-current` and prefix/exact logic; child pages highlight parent section. Layout is mirrored intentionally, not via transforms on an English UI. Preserve user account / role label and logout. Breadcrumbs only for nested views.

## Shared controls and states

- Button emphasis: primary solid jade, secondary ivory outline, tertiary text/ghost, danger quiet outline/tint and strongly emphasized only in final destructive confirmation. Preserve reference size ~40–44px desktop, 44px touch minimum. No layout shift while loading; keep label or reserved spinner. Primary CTA one per screen/task.
- Input/Select: quiet border, white surface, ~42–44px desktop height, 44px minimum mobile, label above. Focus outline, error border+message, disabled state, optional helper; no placeholder-only labels. Preserve native semantics and keyboard support. Read-only identifiers/email links use LTR islands. Date field uses existing Jalali picker and accessible popover; style it, do not replace with Gregorian system picker. Form grids collapse on mobile as needed; progressive disclosure for optional note and advanced connection details. Do not hide mandatory financial information.
- Cards/rows: label and value alignment follows references; route affordance is a single quiet row with icon/chevron and clear focus/hover. Cards may have internal separators rather than nested cards. Admin overview has five count tiles on desktop, two columns on mobile with the fifth spanning where reference shows it; use three compact navigation sections. Partner dashboard has wallet plus subscription count, action and two shortcut rows; mobile stacks same hierarchy. Only actual data.
- Tables: quiet tinted header, no zebra overload, horizontal separators, real `<table>` semantics, 13–14px text, 44–52px row height desktop. Show only purposeful columns; details move to subpage. Preserve existing query/paging and status. On mobile use semantic rows/cards (reference) with clear text and touch area, not sideways scrolling as primary experience. Local scroll if genuinely necessary; do not hide scrollbar. Long names/URLs wrap or ellipsize with accessible full value; long amounts must not overlap. Use text-labeled details/action rather than cryptic icon-only affordance. Header+filters consistent; no fake filters or pagination counts; only client filtering if the complete dataset is really loaded and truthful.
- Tabs use lightweight bottom border or quiet tint matching references, keyboard navigation and clear selected state, avoid duplicating title or empty filler. Keep route-safe deep links for non-sensitive data; review state remains in memory where necessary.
- Modal/drawer: accessible dialog focus trap, Escape, restore focus; responsive width, scroll inside long content, stable footer and visible consequences. On mobile use full screen or sheet only when task and reference suit it. Confirm irreversible actions; preserve business-specific warnings verbatim if needed. Color cannot replace explicit impact.
- Feedback: stable shared banner, inline validation, skeleton with same footprint as final content; distinct empty, no search results, load/error, unavailable and pending states. No fake success. Avoid toast-only critical financial errors. Provide retry only for safe GET or explicitly authorized action.
- Motion: optional 120–180ms transitions for hover/focus and drawers; no ornamental animation or layout bounce. Honor prefers-reduced-motion. Ensure visible focus, contrast target WCAG 2.2 AA, keyboard operation and touch safety.

## Design quality gate

Review exact 39 PNGs, not approximate moodboards. Match each pair's spatial hierarchy and density. Preserve type, contrast, primary action, sidebar/header, bottom navigation, exact core content and real data. Deviation for accessibility, localization or actual business rule must be documented in the implementation checklist. No additional features or false metrics. Maintain this design system as durable source of decisions; any future screen must reuse canonical primitives and tokens.


**Font verification:** Record baseline and final SHA-256 asset manifests, unchanged declarations/fallback and dependencies, and browser font-loading/computed-family evidence for both roles and responsive layouts. Explain any font-metric deviations in Persian; preserve Vazirmatn even if the reference text geometry differs. All progress and delivery reports to the owner must be Persian.
