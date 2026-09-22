# Source whitelist and independence groups

The machine-readable whitelist lives in `config/earn.yaml: news.whitelist` (tier 2).
This file defines which outlets count as ONE source for the two-source rule.

| Independence group | Outlets |
|---|---|
| dcg-adjacent | CoinDesk |
| ct | Cointelegraph |
| theblock | TheBlock |
| blockworks | Blockworks |
| decrypt | Decrypt |
| btc-media | BitcoinMagazine |
| official-eth | EthereumFoundation (primary) |
| official-btc | BitcoinCore (primary) |
| official-us | FederalReserve (primary) |

Rules: two outlets in the SAME group corroborate nothing. A `primary` source
confirms its own domain's facts (an Ethereum Foundation post confirms an Ethereum
upgrade) but not market claims. Regulator actions are corroborated by the
regulator's own notice, not by coverage volume.
