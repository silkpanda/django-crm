# CRM style tokens

A neutral default palette that belongs to no one, so a fork can replace it
without touching CSS. The live values are in `static/css/tokens.css`; this file
records what each token is for and the contrast calls behind it.

**Every brand token is overridable from `.env`.** Set `BRAND_RED`,
`BRAND_BLUE`, `BRAND_NAVY` and friends and `base.html` emits a `:root` override
after the stylesheets load, so re-branding needs no CSS edit — see README
"Branding" and `.env.example`. To replace the palette wholesale, change the
`BRAND_*` variables, not the values below.

## Colors

Ratios are measured against white, the light surface these text tokens are used
on.

| Token | Value | Used for | Notes |
|---|---|---|---|
| `--brand-red` | `#dc2626` | the wordmark accent, the Lost stage, destructive buttons | **4.83:1 on white** — clears AA for body text, and it carries white text as a fill. |
| `--brand-red-text` | `#b91c1c` | the rare red icon/text on white | Darker red, **6.47:1 on white**, for where the red must survive as small text. |
| `--brand-blue` | `#0d9488` | primary button fill, focus ring, call-kind chip | **Fills and borders only.** White on it is 3.74:1, under AA for body text, so it never sets text on a light surface. |
| `--brand-blue-hover` | `#0f766e` | the button's hover fill | The darkening that gets the button to **5.47:1** with white text. |
| `--brand-blue-text` | `#0f766e` | link colour, qualified stage text | **5.47:1 on white.** The teal above is deliberately not the link colour; this is. |
| `--brand-navy` | `#1e293b` | sidebar and auth background | **14.63:1 on white**, and carries white text. |
| `--brand-navy-2` | `#334155` | nav hover/active tint | **10.35:1 on white.** |
| `--ink / -2 / -3` | `#141617` `#212326` `#434549` | headings and strong text | |
| `--body` | `#405363` | default body text | |
| `--surface / -2 / -3` | `#ffffff` `#f9f9fb` `#f2f3f5` | cards, hovers, page background | |
| `--line` | `#aeaeaf` | table and panel borders | |

Shape and spacing tokens (`--radius`, `--space-*`) are not brand-specific and
have no env override.

## Type
- **Roboto Slab** (500/700) — page titles and the wordmark.
- **Roboto** (400/500/700) — body, tables, forms.
- **Roboto Condensed** (400/500) — nav items, table headers, field labels.
- All three load from Google Fonts, falling back to Arial.

## Shape
**Square corners** (`--radius: 0`) everywhere, flat fills, and 1px borders.
No gradients or drop shadows.

## Logo
A text wordmark: `BRAND_WORDMARK` in white, `BRAND_WORDMARK_ACCENT` in red,
until a real logo file is dropped in. Nothing is hotlinked.

## Deviations, and why
- Text-bearing colours are **separated from fill colours.** `--brand-red` and
  `--brand-blue` are fills; `--brand-red-text` and `--brand-blue-text` are the
  darkened variants that clear AA as body text on white. A token that is safe as
  a background is rarely safe as small text, and the same name for both is how
  that gets missed.
- This is a default palette, not a brand asset. Point the `BRAND_*` variables at
  a real brand guide and the shell follows without a CSS edit.
