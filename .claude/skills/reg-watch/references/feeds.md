# Watched bodies

| Body | Why | Where it shows up |
|---|---|---|
| VARA (Dubai) | Licenses Binance's Dubai entity | press releases, news coverage |
| ADGM/FSRA (Abu Dhabi) | Licenses Binance's AD entity | notices |
| CMA | Regional securities authority | notices |
| SEC / CFTC (US) | Actions move BTC/ETH markets | filings, lawsuit coverage |
| Binance announcements | Delistings, maintenance, halts | exchange notices, coverage |
| US Federal Reserve | Macro context only — CPI/FOMC blackouts come from config/macro_calendar.yaml, never from here | primary feed |

The news whitelist itself is `config/earn.yaml: news.whitelist`; this skill reads
the archive those feeds fill, it does not fetch the web.
