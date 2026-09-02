# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Primary audience is people evaluating the author's work, recruiters, hiring managers, and engineers/analysts assessing quant, data-engineering, and financial-analysis ability. It is not built for day-to-day use by a trading desk; it is a demonstration piece meant to be explored in a single sitting.

## Product Purpose

Tracks commodity **and** currency relationships together, to surface trade opportunities, not just commodity volatility in isolation. Nine commodities (energy, metals, agriculture) get the same instrument-level treatment (price, volatility, returns) as fifteen currency pairs; a correlation layer connects the two; a confluence screener flags when a commodity's own volatility, its historically-linked currency's behavior, and news sentiment all point the same direction at once. Macro/geopolitical context (country exposure, trade flows, political risk) explains the "why" behind a move. Success means a viewer can see a commodity, its linked currency, and whether the relationship between them is actually confirming a move right now, not just look at nine unrelated price charts.

## Positioning

The differentiator is cross-asset data fusion culminating in a decision signal, not any single chart: it tracks commodities and currencies as equal, independently-instrumented assets (own price/volatility for both), correlates them, and fuses that correlation with live GDELT news sentiment into a confluence screener that names when three independent signals align. No neighboring price-only tracker, macro-only dashboard, or correlation-only tool claims the same synthesis end-to-end.

## Operating Context

Runs locally as a Python Dash web app (`python3 app.py`, opened at `http://localhost:8050`). Data source for commodities is configurable in `config.py`: `yahoo` (default, free, no key), `bloomberg` (via Terminal + blpapi, optional), or `mock` (synthetic data for offline/demo use); FX pairs are always fetched live via Yahoo Finance. Nine tabs: Volatility, Returns & Trends, Currencies, Correlation, Opportunities, Alerts, Country Exposure, Trade Flows, Risk & News. Auto-refreshes every 5 minutes; FX/sentiment/country/trade/risk data is fetched lazily on first visit to the tabs that need it.

## Capabilities and Constraints

- 9 commodities in 3 groups (Energy: WTI/Brent/Nat Gas; Metals: Gold/Silver/Copper; Agriculture: Wheat/Corn/Soybeans), each with a configurable volatility alert threshold.
- 15 currency pairs vs. USD (Currencies tab), tracked as instruments in their own right: own price, 1D return, and historical volatility (30/60/90d), the same treatment commodities get, not just a correlation input.
- Historical volatility over 30/60/90-day windows, rolling correlation matrix, moving averages, trend signal, and 1D/5D/30D returns, for commodities and currencies alike.
- Correlation tab shows the commodity×commodity matrix (60d) plus an interactive commodity×currency relationship section (52-week rolling, switchable between Correlation, Beta, and R² via one dropdown), the actual cross-asset relationship the product is named for, quantified three ways, not a map-coloring option. Beta and R² are derived from the same aligned 252-trading-day return series as the correlation (not recomputed independently), so the three numbers are internally consistent for any given cell: beta = corr × std(currency)/std(commodity); r2 = corr².
- Opportunities tab runs a confluence screener: per commodity, finds its most-correlated currency (by |correlation|, not a fixed assumption), and flags a signal only when volatility is elevated, the currency is moving the way the correlation predicts, and news sentiment agrees with the price direction. Any one alone is not a signal; the table shows every commodity's status, flagged or not, for transparency. Sentiment scoring is the slowest input (nine sequential GDELT calls), the board renders on the fast inputs and fills sentiment in as it lands, never blocking or faking a score.
- Country-level commodity exposure via World Bank indicators, plotted on a map with click-through detail.
- Top exporter/importer figures rendered as a Sankey diagram (Trade Flows tab) and the Country Exposure map's export/import/shock metrics now pull live from UN Comtrade (`data/comtrade.py`) as a trailing-12-month (TTM) sum of monthly data, not a stale full calendar year, the UI shows exactly which window (e.g. "UN Comtrade, Aug 2025–Jul 2026 (TTM, live)"). Countries report monthly data on different cadences (major reporters typically 1-2 months behind; others considerably more), so each country's TTM figure sums whatever of those 12 months it has actually published rather than forcing every country onto one laggard's cutoff. The Trade Flows Sankey also shows real bilateral (country-to-country) routes for exporters that report destination-level detail, some major exporters (Saudi Arabia among them) report only a world total, and that gap is shown honestly via an "Unreported Destination"/"Unreported Source" node rather than guessed at. `data/trade_data.py`'s static 2023 estimate table is kept as a per-commodity fallback (network failure, rate limit, or a commodity's HS code lacking recent data), the label switches to say so plainly when that happens, rather than silently mislabeling static data as live or vice versa.
- Geopolitical risk per country blends a World Bank political-stability baseline with a static conflict/sanctions penalty list (UCDP/OFAC-sourced, dated 2024) and live GDELT news-sentiment adjustment.
- News feed and sentiment score per commodity via GDELT (no API key required, 15-minute cache); GDELT rate-limits aggressively under repeated testing (429s), which the code already treats as a graceful-fallback case (neutral score), not an error state.
- Undecided: whether this ever runs anywhere other than local (`localhost`), no deploy target has been chosen.

## Brand Commitments

None. No name, logo, or existing palette is binding, the visual direction is fully open for this redesign.

## Evidence on Hand

Real and live where a free source exists: Yahoo Finance prices for both commodities and FX, World Bank open indicators, GDELT news/sentiment. Trade-flow figures (Trade Flows tab) are a static, dated (2023) estimate table, not a live Comtrade fetch, see Capabilities above; do not describe this tab as "live UN Comtrade data" until `data/comtrade.py` is actually wired in. The conflict/sanctions penalty list is a static, dated (2024) editorial judgment call, not live data, treat it as informed but not authoritative. The confluence screener is a derived signal, not a data source: it's arithmetic over the other real inputs, and its own methodology is stated on-screen. `mock` mode substitutes synthetic random-walk prices for offline demoing; it must stay clearly a fallback, never presented as real market data. No testimonials, customers, or benchmarks exist or should be invented.

## Product Principles

1. Every number traces to a real public data source (Yahoo, World Bank, GDELT) or is clearly labeled as a static/derived figure (trade-flow estimates, the confluence score), nothing fabricated for show, including in redesign copy.
2. Commodities and currencies are tracked as equal, independently-instrumented assets, a currency is never just an input to a commodity's chart. The correlation and confluence layers are the actual value; design should make those connections legible, not bury them under equal-weight, unrelated-seeming tabs.
3. A derived signal (the confluence flag) must show its work and its gaps, partial data (e.g. sentiment not yet scored) reads as neutral and is disclosed on-screen, never silently treated as a false negative or hidden until "complete."
4. Zero-config runnability matters, the Yahoo and mock paths need no API keys, so any viewer can run the real thing themselves.
5. Speaking to a technical/hiring audience means the craft itself is part of the pitch: the interface should read as fluent in financial data, not just "a chart library was used."
