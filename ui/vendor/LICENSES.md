# Vendored UI assets — provenance and licenses

Pulled 2026-08-14 by the UI seat on the owner's order ("pull super high
quality tools from web"). Official sources only, versions pinned, hashes
recorded at pull time. Nothing here is wired into a page until the owner
directs a build that needs it.

| Asset | Version | License | Source |
|---|---|---|---|
| ui/fonts/InterVariable.woff2 | Inter v4.1 | OFL 1.1 (fonts/LICENSE-Inter-OFL.txt) | github.com/rsms/inter release v4.1 zip, web/InterVariable.woff2 |
| ui/fonts/JetBrainsMono-{Regular,Medium,SemiBold}.woff2 | v2.304 | OFL 1.1 (fonts/LICENSE-JetBrainsMono-OFL.txt) | github.com/JetBrains/JetBrainsMono release v2.304 zip, fonts/webfonts/ |
| ui/vendor/uPlot.iife.min.js + uPlot.min.css | 1.6.32 | MIT (vendor/LICENSE-uPlot-MIT.txt) | cdn.jsdelivr.net/npm/uplot@1.6.32/dist/ (npm registry mirror) |
| ui/vendor/lightweight-charts.standalone.production.js | 5.0.8 | Apache-2.0 (license header in file) | unpkg.com/lightweight-charts@5.0.8/dist/ (npm registry mirror) |
| ui/vendor/icons/*.svg (53 icons) | lucide main @ pull date | ISC (vendor/icons/LICENSE-Lucide-ISC.txt) | raw.githubusercontent.com/lucide-icons/lucide/main/icons/ |

SHA-256 at pull time: see vendor/SHA256SUMS (regenerate with
`shasum -a 256` over ui/fonts and ui/vendor).

Why these
- InterVariable: the UI face the design system already names; variable
  file covers all weights; full tnum/lnum support.
- JetBrains Mono (3 static weights): the numeric/mono zone face for
  ids, hashes, receipts; static weights keep pages lean.
- uPlot: ~51KB canvas time-series engine built for high-frequency data;
  the lean default for sparklines/series when a real chart is ordered.
- lightweight-charts: TradingView's own OSS chart engine (candles,
  baseline, price scales) for exchange-grade charting if the owner
  wants that look.
- lucide: one consistent ISC icon set (arrows, shields, lamps, clocks);
  no emoji, no mixed sets, per DESIGN_DOCTRINE §B.

Serving: serve.py maps .woff2/.svg/.png content types and CSP carries
font-src 'self'; everything loads same-origin, zero CDN at runtime.
