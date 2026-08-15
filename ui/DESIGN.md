# DESIGN.md — the dashboard's design constitution

Why this file exists (measured 2026-08-15, owner-ordered): seriousness is
discipline you can count. OpenAI's platform page: 21 text voices for 97
elements, top-3 voices cover 65%, ZERO chromatic words. Binance: 19 voices
for 155, color only where it IS market data. Our Network page before this
law: 27 voices for 96, top-3 cover 39%, 14 chromatic words — every third
element invented a voice, color spent on saying "normal". Trillion-dollar
feel = one decision repeated everywhere; amateur feel = many local
decisions. Every element on every page is built AGAINST this file; a new
voice or a new color is a constitution amendment, not a local choice.

## 1. Text castes — the only voices that exist

| caste | use | recipe (light theme tokens) |
|---|---|---|
| T1 page title | one per page | 26px/650 ink (shared page skeleton) |
| T2 label | ALL caps labels: column heads, group heads, tile labels, chip labels, lamps | 9.75px/500/+0.045em UPPERCASE, muted |
| T3 datum | every measured value | mono, tabular-nums, 400. Row 12.5px · tile 17px · chip 12.5px. INK unless deviating (§2) |
| T4 name | component/row names | 13.5px/500 ink |
| T5 meta | row details, captions, footnotes | 11px/400 muted (faint for footnotes). Terse system phrases — never tutorial sentences |
| T6 control | buttons, selects, pills, tabs | 12.5px/500; quiet: hairline border or none, ink on hover/active |

Two fonts only: Inter (UI), JetBrains Mono stack (data). No third font, no
italic, no new letter-spacing values, no new caps treatments.

## 2. Color budget — color is deviation, never decoration

Base page = ink + muted + faint + hairlines on white/panel. Chromatic color
may appear ONLY as:

1. **Deviation**: warn #d4a406, bad #cc2f3c — on the affected T3 value, its
   status word, and its dot. Nothing else on the row changes.
2. **Nominal status** is SILENT: the word (Operational/OK) renders muted,
   its dot is the only green (small, quiet). A page where everything is
   fine is a page with almost no color — that is what makes the one amber
   word loud when it appears.
3. **Machine authority chip**: value ink when ARMED (nominal), amber for
   ENTRY-SEALED/PAUSED, red only if the mirror says a bad state. Never
   green — armed is normal, not a celebration.
4. **History marks** (trend/uptime bars, chart line): nominal ticks quiet
   sage #57b47a; warn/bad ticks full status color; chart line neutral ink,
   threshold bands faint amber/red. Color in a chart is data or absent.
5. **Control accent** (blue): ONLY on real interactive state — toggle ON,
   focus ring, select accent. A control that controls nothing does not
   exist (§4), so blue never marks a fake.

Banned: green values for normal readings, colored pills with tinted fills
for nominal states, decorative line colors, any hue outside
{ink grays, #d4a406, #cc2f3c, quiet #57b47a dot/ticks, control blue}.

## 3. Status grammar

Vocabulary (old room's watchman, unchanged): Operational · Degraded ·
Outage · Owner gated · Off · Measuring. The word always appears with the
color (color never alone). Rendering (amended 2026-08-15 after the owner's
side-by-side verdict — the first cut over-quieted into washout): colored
GLYPH + readable word, no capsule. Dot 6px at full status color (#16a34a
green nominal — the glyph, not the text, carries the green; GitHub-status/
Datadog pattern); word 11px/450 sentence case in INK for nominal, status
color + 500 for deviation, muted for boundary/off. NO pill capsule, no
background fill — but also no washed-out gray whispers: silence means the
word doesn't shout, not that it fades. Columns keep their rhythm: cells
with nothing real to show render the honest "—", never blank voids.

## 4. Controls tell the truth

A switch exists only where flipping it does something real TODAY (web
server poll pause, News master). Rows whose control endpoint does not
exist yet get NO switch — their nature lives in the status word (Owner
gated) and one T5 footnote under the table. No disabled dummy switches, no
dead buttons, ever. Per-row refresh icons only where a real fetch answers.

## 5. Anatomies (one of each, reused)

- **Row**: 40px min-height, hairline bottom, name+detail left, then status,
  datum, history, uptime, cadence, refresh, control. Hover = flat --hover.
- **Tile band**: one bordered 14px-radius strip, tiles separated by inner
  hairlines: T2 label / T3 17px value / T5 subline.
- **Card** (floating surface, e.g. chart): hairline border, 14px radius,
  panel bg, T6 tool row, T5 caption. Borders exist ONLY on interactive
  units and floating surfaces — never around page regions.
- **Header lamp** (one per page, top-right): 8px dot colored + T2 word
  muted. The ONLY place the overall state is announced.
- **Page header**: T1 title + authority chip + lamp on one line, one T5
  subtitle line. Never two subtitle lines; notes live near their subject.

## 6. Voice

Terse system phrases: "serve.py · loopback probe", "104 public sources".
Subjects and measurements, no verbs of explanation, no "here/you/this
dashboard" tutorial talk. Anything longer than ~4 words is documentation
and belongs in a tooltip or the handbook, not on the instrument.

## 7. Process law

Every element = one narrow card (10–30 min) buildable blind against this
file; the integrating seat eye-verifies each landing in the owner's Chrome
at 100%, then re-measures the page: **voices ≤ 15, top-3 coverage ≥ 55%,
chromatic elements = deviations + data only.** A page that fails the count
is not done. Web-grade before showing: the reference bar is OpenAI
platform / AWS console / githubstatus discipline, measured, not remembered.
