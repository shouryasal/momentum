# Demo mode — reaching Binance Spot Demo, and refusing to reach anything else

Status: design + verified evidence. Owner module `ops/lib/exchange_endpoints.py` is built
and tested; the patches in §6 are the handover to the agents who own
`ops/gen_freqtrade_config.py`, `ops/lib/compose.py`, `ops/modes.py`, `ops/preflight.py`,
`ops/config.py` and the compose files.

Everything in §1–§4 was measured on 2026-09-23 against the installed libraries in
`~/earn-dev/.venv` (ccxt `4.5.82`, freqtrade `2026.8`) and against the real public
endpoints. Nothing here is from memory.

---

## 0. The one-paragraph version

Binance Spot **Demo Mode** (`demo-api.binance.com`) is not the Spot **Testnet**
(`testnet.binance.vision`). ccxt's `set_sandbox_mode(True)` goes to testnet, so it is the
wrong switch. ccxt 4.5.82 already ships a demo URL map and an `enable_demo_trading()`
method, and freqtrade 2026.8 already has an `exchange.demo_trading` config key that calls
it — but freqtrade ships `binance._ft_has["supports_demo_trading"] = False`, so the key is
rejected for Binance until we override it via the supported `exchange._ft_has_params`
escape hatch. Separately, our `ops/docker-compose.testnet.yml` sets
`FREQTRADE__EXCHANGE__URLS__API`, which **freqtrade never reads**: that rehearsal has been
pointing at production all along. Demo's exchange filters, order types and rate limits are
byte-identical to live, so nothing about order clamping changes.

---

## 1. `set_sandbox_mode` points at testnet, not demo

`ccxt/base/exchange.py:3476` swaps `urls['api']` for `urls['test']`:

```python
def set_sandbox_mode(self, enabled: bool):
    if enabled:
        if 'test' in self.urls:
            ...
            self.urls['apiBackup'] = self.clone(self.urls['api'])
            self.urls['api'] = self.clone(self.urls['test'])
```

Result of `ccxt.binance(); s.set_sandbox_mode(True); s.urls['api']`:

```
private : https://testnet.binance.vision/api/v3
public  : https://testnet.binance.vision/api/v3
v1      : https://testnet.binance.vision/api/v1
fapi*   : https://testnet.binancefuture.com/fapi/...
dapi*   : https://testnet.binancefuture.com/dapi/...
options sandboxMode: True
```

**Verdict: sandbox mode can never reach demo.** It is the wrong switch, and the only thing
that makes it *safe* is that it also drops `sapi`, `papi`, `eapi`, `fapiData` and
`dapiData` entirely, because `urls['test']` has no such keys — the whole map is replaced,
not merged.

ccxt also refuses to combine the two (`ccxt/binance.py:3109`):

> `binance demo trading is not supported in the sandbox environment.`

And for futures, sandbox is now dead outright (`ccxt/binance.py:12308`):

> `testnet/sandbox mode is not supported for futures anymore ... consider using the demo trading instead.`

## 2. The full shape of `describe()["urls"]`, and which keys must be overridden

`ccxt.binance().describe()["urls"]` top-level keys:

```
['api', 'api_management', 'demo', 'doc', 'fees', 'logo', 'referral', 'test', 'www']
```

`urls['api']` (production) has **22** keys in four tiers:

| Tier | Keys | Production host |
|---|---|---|
| spot | `public`, `private`, `v1` | `api.binance.com` |
| sapi | `sapi`, `sapiV2`, `sapiV3`, `sapiV4` | `api.binance.com` |
| futures/options/pm | `fapiPublic`, `fapiPublicV2`, `fapiPublicV3`, `fapiPrivate`, `fapiPrivateV2`, `fapiPrivateV3`, `fapiData`, `dapiPublic`, `dapiPrivate`, `dapiPrivateV2`, `dapiData`, `eapiPublic`, `eapiPrivate`, `papi`, `papiV2` | `fapi.` / `dapi.` / `eapi.` / `papi.binance.com` |
| websockets (ccxt.pro only) | `ws.{spot,margin,future,delivery,option,optionMarket,optionPrivate,papi,stock}` and `ws["ws-api"].{spot,future,delivery}` | `stream.binance.com:9443`, `ws-api.binance.com:443`, … |

**ccxt 4.5.82 already ships `urls['demo']`** (`ccxt/binance.py:234`):

```
public  : https://demo-api.binance.com/api/v3
private : https://demo-api.binance.com/api/v3
v1      : https://demo-api.binance.com/api/v1
fapi*   : https://demo-fapi.binance.com/...
dapi*   : https://demo-dapi.binance.com/...
ws.spot        : wss://demo-stream.binance.com/ws
ws.margin      : wss://demo-stream.binance.com/ws
ws["ws-api"].spot : wss://demo-ws-api.binance.com/ws-api/v3
```

Those match the documented demo endpoints exactly (REST `demo-api`, WS API
`demo-ws-api`, streams `demo-stream`). Note `urls['demo']` has **no** `sapi`, `papi`,
`eapi`, `fapiData` or `dapiData` keys — by design, demo does not serve them.

### 2a. The dangerous case, measured

A **partial** `ccxt_config` url override leaves the rest on production:

```python
ccxt.binance({"urls": {"api": {"public":  "https://demo-api.binance.com/api/v3",
                               "private": "https://demo-api.binance.com/api/v3"}}})
# public      -> https://demo-api.binance.com/api/v3
# sapi        -> https://api.binance.com/sapi/v1      <-- PRODUCTION
# papi        -> https://papi.binance.com/papi/v1     <-- PRODUCTION
# fapiPrivate -> https://fapi.binance.com/fapi/v1     <-- PRODUCTION
# v1          -> https://api.binance.com/api/v1       <-- PRODUCTION
```

**And a "full" override is not full either.** ccxt's constructor *deep-merges* config over
`describe()`, it does not replace:

```python
demo = ccxt.binance().describe()["urls"]["demo"]
api3 = ccxt.binance({"urls": {"api": demo}})
api3.urls["api"].keys()   # 22 keys, not 12
api3.urls["api"]["sapi"]  # 'https://api.binance.com/sapi/v1'   <-- STILL PRODUCTION
```

Copying `urls['demo']` wholesale into `ccxt_config` therefore still leaves four `sapi`
keys, two `papi`, two `eapi` and two `*Data` keys on production. This is the single most
important finding in this document.

Only `enable_demo_trading(True)` (`ccxt/binance.py:3100`) does a wholesale assignment:

```python
self.urls['apiBackupDemoTrading'] = self.urls['api']
self.urls['api'] = self.urls['demo']       # assignment, not merge
self.options['enableDemoTrading'] = enable
```

Measured afterwards — 12 keys only, `sapi`/`papi` gone, websockets moved too:

```
urls['api'] keys: ['dapiPrivate','dapiPrivateV2','dapiPublic','fapiPrivate','fapiPrivateV2',
                   'fapiPrivateV3','fapiPublic','fapiPublicV2','fapiPublicV3',
                   'private','public','v1','ws']
public  : https://demo-api.binance.com/api/v3
private : https://demo-api.binance.com/api/v3
sapi present? False   |  papi present? False
ws.spot           : wss://demo-stream.binance.com/ws
ws["ws-api"].spot : wss://demo-ws-api.binance.com/ws-api/v3
options.enableDemoTrading: True
```

ccxt also self-defends: `fetch_currencies` returns `{}` under demo rather than calling
sapi (`ccxt/binance.py:3169`, *"demotrading does not support sapi endpoints"*), and
`fetch_markets` skips options and the sapi margin calls when
`isDemoEnv` (`ccxt/binance.py:3410`).

**Consequence for our module:** `ops/lib/exchange_endpoints.py` never emits a partial map.
`ccxt_url_overrides(venue)` returns a value for **every** one of the 22 keys, routing
anything the venue does not serve to `https://disabled.earn.invalid` (RFC 2606 reserved —
guaranteed not to resolve). Merged or assigned, the result is identical, and a leaked call
fails DNS instead of reaching Binance.

## 3. How freqtrade 2026.8 actually passes this through

### 3a. `FREQTRADE__EXCHANGE__URLS__API` is silently ignored — our testnet rehearsal was pointed at production

`ops/docker-compose.testnet.yml` currently sets:

```yaml
FREQTRADE__EXCHANGE__URLS__API: "https://testnet.binance.vision/api"
```

`freqtrade/configuration/environment_vars.py` turns that into
`{"exchange": {"urls": {"api": "https://testnet.binance.vision/api"}}}` — verified:

```json
{"dry_run": false,
 "exchange": {"key": "AAA",
              "urls": {"api": "https://testnet.binance.vision/api"}}}
```

But **`exchange.urls` is read by nothing**. A grep for `urls` across the whole installed
`freqtrade` package returns only `binance_public_data.py` docstrings, a SQLAlchemy docs
URL and `secrets.token_urlsafe` — no config lookup at all. And the key is not rejected
either: `CONF_SCHEMA["properties"]["exchange"]` declares **no** `properties` and **no**
`additionalProperties`, so unknown keys pass validation silently.

> **Live finding for the handover: the Week-3 G1 testnet rehearsal has been sending testnet
> credentials to `api.binance.com`.** It presumably failed authentication rather than
> trading, but the override never worked and must not be copied for demo.

### 3b. `ccxt_config` / `ccxt_async_config` do reach ccxt

`freqtrade/exchange/exchange.py:270-283`:

```python
ccxt_config = deep_merge_dicts(exchange_conf.get("ccxt_config", {}), ccxt_config)
ccxt_config = deep_merge_dicts(exchange_conf.get("ccxt_sync_config", {}), ccxt_config)
self._api = self._init_ccxt(exchange_conf, True, ccxt_config)
ccxt_async_config = deep_merge_dicts(exchange_conf.get("ccxt_config", {}), ...)
ccxt_async_config = deep_merge_dicts(exchange_conf.get("ccxt_async_config", {}), ...)
self._api_async = self._init_ccxt(exchange_conf, False, ccxt_async_config)
self._ws_async  = self._init_ccxt(exchange_conf, False, ccxt_async_config)   # line 290
```

So `exchange.ccxt_config.urls.api.*` **does** reach the ccxt constructor — and all three
clients (sync, async, websocket) get it. `environment_vars.py` even preserves the case of
the final key under a `ccxt_config` path. This is a working route, but per §2a it is a
*merge*, so it is only safe when the map is complete. Our module makes it complete.

### 3c. The first-class route: `exchange.demo_trading`

freqtrade 2026.8 has native demo support (`exchange.py:430`):

```python
if self.get_option("supports_demo_trading") and exchange_config.get("demo_trading", False):
    api.enable_demo_trading(True)
```

with a guard (`exchange.py:884`):

```python
def validate_demo_trading(self, exchange_conf: dict) -> None:
    if exchange_conf.get("demo_trading", False):
        if not self.get_option("supports_demo_trading"):
            raise ConfigurationError(f"Demo trading is not supported for {self.name}.")
```

**But `freqtrade/exchange/binance.py:52` ships:**

```python
# Demo trading
# https://www.binance.com/en/support/faq/detail/9be58f73e5e14338809e3b705b9687dd
# Intentionally Disabled as it's a separate market - not a simulated live market.
"supports_demo_trading": False,
```

(only `bybit.py:40` has `True`). So `demo_trading: true` on Binance raises
`ConfigurationError` out of the box.

The supported unlock is `exchange._ft_has_params`, a documented escape hatch
(`exchange.py:977`):

```python
def build_ft_has(self, exchange_conf: ExchangeConfig) -> None:
    self._ft_has = self.combine_ft_has(...)
    if exchange_conf.get("_ft_has_params"):
        self._ft_has = deep_merge_dicts(exchange_conf.get("_ft_has_params"), self._ft_has)
```

Verified: `deep_merge_dicts({"supports_demo_trading": True}, Binance.combine_ft_has(False))`
→ `True`. And the call order is right — `build_ft_has` runs at `exchange.py:228`, before
`_init_ccxt` at 274, so the override is in force when the demo switch is read.

**Recommended route: `demo_trading: true` + `_ft_has_params.supports_demo_trading: true`.**
It is the only one that gets the wholesale swap (and therefore actually removes `sapi`
and `papi`), it moves the websockets too, and it makes freqtrade label the exchange
`Binance (Demo)` / `binance_demo` in `/show_config` (`exchange.py:448,453`) — a free,
visible confirmation for `ops/modes.py`'s step-10 verify.

One more freqtrade rule to respect (`config_validation.py:417`):

```python
if conf.get("exchange", {}).get("demo_trading", False) and conf.get("dry_run", False):
    raise ConfigurationError("Demo trading cannot be used together with dry_run.")
```

**Demo mode requires `dry_run: false`.** Our invariant "committed bot configs always
`dry_run: true`" is preserved by keeping `demo_trading` out of `config/freqtrade-{a,b}.json`
entirely and putting it only in `var/runtime/freqtrade-<s>.mode.json`, exactly as live
already does — see §6.

## 4. Demo vs live on the wire (measured 2026-09-23)

`GET /api/v3/ping`, `/api/v3/time`, `/api/v3/exchangeInfo?symbol=…`, unauthenticated.

| | demo | live |
|---|---|---|
| `/api/v3/ping` | 200, `{}` | 200, `{}` |
| `/api/v3/time` | 200 | 200 |
| latency, median of 5 | **168.9 ms** (min 168.4 / max 170.6) | **168.8 ms** (min 166.3 / max 170.8) |
| `exchangeInfo` | 200, 261 ms | 200, 246 ms |

**Rate limits — identical:**

```
REQUEST_WEIGHT  1 MINUTE  6000
ORDERS         10 SECOND   100
ORDERS          1 DAY    200000
RAW_REQUESTS    5 MINUTE 300000
```

**BTCUSDT and ETHUSDT filters — byte-identical between demo and live.** Every field
compared equal (`PRICE_FILTER`, `LOT_SIZE`, `MARKET_LOT_SIZE`, `NOTIONAL`,
`PERCENT_PRICE_BY_SIDE`, `TRAILING_DELTA`, `ICEBERG_PARTS`, `MAX_NUM_*`):

```
PRICE_FILTER  minPrice 0.01000000  maxPrice 1000000.00000000  tickSize 0.01000000
LOT_SIZE      minQty   0.00001000  maxQty   9000.00000000     stepSize 0.00001000
NOTIONAL      minNotional 5.00000000  applyMinToMarket true
              maxNotional 9000000.00000000  applyMaxToMarket false  avgPriceMins 5
MARKET_LOT_SIZE  minQty 0.00000000  maxQty 108.83553435  stepSize 0.00000000
PERCENT_PRICE_BY_SIDE  bidUp 1.2 bidDown 0.5 askUp 2 askDown 0.8  avgPriceMins 5
```

`baseAssetPrecision` 8, `quoteAssetPrecision` 8, `exchangeFilters: []`,
`defaultSelfTradePreventionMode: EXPIRE_MAKER` — the same on both.

> **Nothing about how we clamp orders changes between demo and live.** The gate's
> `min_notional_usdt: 25` in `config/earn.yaml` remains comfortably above the exchange's
> `minNotional` of 5 on both.

**Prices are close but not identical**, as the docs say ("similar to live"):

```
BTCUSDT: demo 84641.87  live 84641.87  diff   0.00 bps
ETHUSDT: demo  2679.45  live  2679.52  diff  -0.26 bps
```

That is far better evidence than testnet, whose book is independent — but TCA must still
treat demo slippage as indicative, not as a live cost measurement.

### 4a. Order types on demo — everything our design needs

`orderTypes` for BTCUSDT **and** ETHUSDT, identical on demo and live:

```
LIMIT, LIMIT_MAKER, MARKET, STOP_LOSS, STOP_LOSS_LIMIT, TAKE_PROFIT, TAKE_PROFIT_LIMIT
```

plus `ocoAllowed: true`, `otoAllowed: true`, `cancelReplaceAllowed: true`,
`quoteOrderQtyMarketAllowed: true`, `isSpotTradingAllowed: true`,
`allowedSelfTradePreventionModes: [EXPIRE_TAKER, EXPIRE_MAKER, EXPIRE_BOTH, DECREMENT, TRANSFER]`.

| needed by our design | demo | evidence |
|---|---|---|
| `LIMIT` | yes | `orderTypes` |
| `MARKET` | yes | `orderTypes` |
| `STOP_LOSS_LIMIT` | yes | `orderTypes` |
| `TAKE_PROFIT_LIMIT` | yes | `orderTypes` |
| `OCO` | yes | `ocoAllowed: true`, `MAX_NUM_ORDER_LISTS: 20` |
| resting exchange-side stop | yes | `STOP_LOSS_LIMIT` + `MAX_NUM_ALGO_ORDERS: 5` |

freqtrade's binance spot adapter uses exactly this: `_ft_has["stoploss_order_types"] =
{"limit": "stop_loss_limit"}` with `stoploss_blocks_assets: True`. So
`stoploss_on_exchange` — a hard requirement of the live design
(`modes.live.require_stoploss_on_exchange: true`) — **works unchanged on demo**, and
`ops/preflight.py::_check_stoploss_on_exchange` will pass once its `exchangeInfo` probe is
pointed at the demo host.

### 4b. Demo has no `sapi` tier at all

```
GET https://demo-api.binance.com/sapi/v1/account/apiRestrictions -> 404 (nginx HTML)
GET https://demo-api.binance.com/sapi/v1/capital/config/getall   -> 404 (nginx HTML)
GET https://api.binance.com/sapi/v1/account/apiRestrictions      -> 400 {"code":-2014,...}
GET https://api.binance.com/sapi/v1/capital/config/getall        -> 400 {"code":-2014,...}
```

Production answers with a Binance error code; demo answers with an nginx 404 — the path
does not exist. **`ops/lib/binance_check.py::RESTRICTIONS_PATH` is a sapi path**, so the
`exchange_keys` preflight check cannot probe key permissions on demo. §6.4 says what to do
instead.

---

## 5. What `ops/lib/exchange_endpoints.py` provides

Pure, importable, stdlib + `httpx`. No freqtrade import, and deliberately **no ccxt import**
— the URL shapes are pinned as constants so a ccxt upgrade that adds a key fails
`tests/test_ops/test_exchange_endpoints.py` instead of silently opening a hole.

| symbol | what it is |
|---|---|
| `Venue` | `LIVE` / `DEMO` / `TESTNET` (a `StrEnum`; the value is what an operator writes) |
| `VENUES`, `endpoints_for`, `parse_venue` | the venue → `VenueEndpoints` table (REST base, ws stream, ws-api, `supports_sapi`, `supports_futures`, where to mint a key, balance-reset policy) |
| `ccxt_url_overrides(venue)` | **all 22** url keys; unsupported tiers → `BLACKHOLE_URL` |
| `ccxt_ws_overrides(venue)` | all 9 `ws` keys + the 3 nested `ws-api` keys |
| `ccxt_config(venue)` | the `{"urls": {"api": {...}}}` dict for `exchange.ccxt_config` |
| `freqtrade_exchange_patch(venue)` | the preferred freqtrade fragment (demo ⇒ `demo_trading` + `_ft_has_params`) |
| `blackholed_keys(venue)` | the audit trail of what is disabled |
| `assert_no_production_host(payload, venue=…)` | walks any JSON-shaped object and refuses on a production host for a non-live venue |
| `Credential` | a key labelled with its venue; secret never in `repr`/`str`/`describe()` |
| `MODE_VENUE`, `venue_for_mode`, `resolve_binding` | the binding rule; `resolve_binding` is the only constructor of `Binding`, so holding one *is* the proof |
| `VenueBindingError` | the typed refusal, with an operator-actionable message |
| `verify_credential_venue(cred, probe=…)` | → `VenueProof` with status `confirmed` / `mismatch` / `cannot_verify` |
| `signed_query`, `httpx_account_probe` | the signing helper (pure, tested) and the one networked function (never called in tests) |

`BLACKHOLE_URL = "https://disabled.earn.invalid"` and
`BLACKHOLE_WS = "wss://disabled.earn.invalid/ws"` — `.invalid` is reserved by RFC 2606 and
cannot resolve.

The binding table:

| mode | venue it may reach |
|---|---|
| `TEST` | none — a dry-run sleeve talks to nobody, and a present key is refused |
| `DEMO_PROPOSE`, `DEMO_EXECUTE` | `DEMO` |
| `LIVE_PROPOSE`, `LIVE_EXECUTE` | `LIVE` |
| `TESTNET_REHEARSAL` | `TESTNET` |
| `ARMING`, `DISARMING` | refused — transient, never a destination |
| anything else | refused — a new mode must be given a venue before it can run |

A refusal reads like this:

```
REFUSED: mode DEMO_EXECUTE may only reach demo (demo-api.binance.com), but credential
BINANCE_KEY_A (key ...CCCC) is labelled live (api.binance.com). Either label/replace the
credential with a demo key from Binance account UI -> Demo Trading -> API Key Management,
or move the sleeve to LIVE_EXECUTE or LIVE_PROPOSE. Refusing to build a config that points
live credentials at demo-api.binance.com.
```

`verify_credential_venue` requires **both halves** — it authenticated where it claimed and
it was *refused* everywhere else. If any other venue is unreachable, the negative is
unproven and the status is `cannot_verify`, not `confirmed`. With no key present it is
`cannot_verify`, never a pass.

Tests: `tests/test_ops/test_exchange_endpoints.py`, 63 cases, no network.

---

## 6. Handover — the exact patches other agents must apply

### 6.1 `config/earn.yaml` + `ops/config.py` — make DEMO a first-class mode

Add to the `exchange:` block in `config/earn.yaml`:

```yaml
exchange:
  name: binance
  fee_bps_assumed: 10
  venue: demo                      # live | demo | testnet — which Binance environment
```

and in `ops/config.py`, on `class Exchange` (line ~305), using the `F(...)` helper so the
schema test passes (every new key needs `description`, `x-tier`, `x-group`):

```python
venue: Literal["live", "demo", "testnet"] = F(
    "demo",
    desc="Which Binance environment the sleeves reach. Bound to mode by "
         "ops.lib.exchange_endpoints.resolve_binding; a mismatch is a render-time refusal.",
    group="exchange", protected=True,
)
```

Add a `modes.demo:` block mirroring `modes.live:` but with its own ceilings, e.g.
`seed_usdt`, `submode_default: propose`, `require_stoploss_on_exchange: true`,
`confirm_phrase: "GO DEMO {sleeve} {seed} USDT"`. Do **not** reuse
`modes.live.max_seed_usdt` — demo money is free and a demo run should be sized like the
live one it is rehearsing.

### 6.2 `ops/modes.py` — the new states

Add to `ALLOWED` (line ~78). Demo sits between TEST and LIVE; you cannot jump from demo
straight to live without passing through TEST, so the preflight gates are re-run:

```python
ALLOWED: dict[str, frozenset[str]] = {
    "TEST":          frozenset({"TEST", "DEMO_PROPOSE", "DEMO_EXECUTE",
                                "LIVE_PROPOSE", "LIVE_EXECUTE"}),
    "DEMO_PROPOSE":  frozenset({"DEMO_EXECUTE", "TEST"}),
    "DEMO_EXECUTE":  frozenset({"DEMO_PROPOSE", "TEST"}),
    "LIVE_PROPOSE":  frozenset({"LIVE_EXECUTE", "TEST"}),
    "LIVE_EXECUTE":  frozenset({"LIVE_PROPOSE", "TEST"}),
    "ARMING":        frozenset({"TEST"}),
    "DISARMING":     frozenset({"TEST"}),
}

CONFIRM_DEMO = "GO DEMO"
```

`TransitionRequest.is_live` must stay **false** for the demo states (demo is not live
money), but a new `is_venue_bound` property (`venue_for_mode(target) is not None`) should
drive the steps that need a real exchange: `preflight`, `reconcile`, and the `verify` of
`/show_config`. In step 10 (`verify`), assert `show_config["exchange"] == "binance_demo"`
for a demo target — freqtrade returns the `_demo` suffix itself (`exchange.py:453`), which
is independent confirmation that `enable_demo_trading` actually fired.

`ops/lib/mode_state.py` must treat the demo states as valid. Three exact edits there:

```python
# line 37
SleeveMode = Literal["TEST", "ARMING", "DEMO_PROPOSE", "DEMO_EXECUTE",
                     "LIVE_PROPOSE", "LIVE_EXECUTE", "DISARMING"]
# line 41
MODES: tuple[str, ...] = ("TEST", "ARMING", "DEMO_PROPOSE", "DEMO_EXECUTE",
                          "LIVE_PROPOSE", "LIVE_EXECUTE", "DISARMING")
# new, next to LIVE_MODES (line 42)
DEMO_MODES: frozenset[str] = frozenset({"DEMO_PROPOSE", "DEMO_EXECUTE"})
```

`LIVE_MODES` must **not** grow — `SleeveState.is_live` (line 71) and
`ModeState.any_live` gate real-money behaviour and demo is not real money. Add a parallel
`is_demo` / `needs_exchange` (`is_live or is_demo`) for the paths that need a venue.
`_parse` (line 167) already rejects anything outside `MODES`, so the fail-closed invariant
"unverified or unknown ⇒ TEST" survives untouched.

### 6.3 `ops/gen_freqtrade_config.py` — the overlay

Leave `build_bot_config` alone: the committed `config/freqtrade-{a,b}.json` keeps
`dry_run: true` and must gain **no** `demo_trading` key (freqtrade refuses
`demo_trading` + `dry_run` together — `config_validation.py:417`).

In `build_mode_overlay` (line ~281), after the existing `if live:` branch:

```python
from ops.lib.exchange_endpoints import (
    Venue, assert_no_production_host, freqtrade_exchange_patch, resolve_binding,
)

binding = resolve_binding(sl.mode, credential_for(cfg, sleeve))   # raises on disagreement
if binding.venue is not None:
    overlay["dry_run"] = False          # demo and live both need a real exchange
    overlay.setdefault("order_types", {})["stoploss_on_exchange"] = \
        stoploss_on_exchange(cfg, sleeve, live=True)
    overlay["exchange"] = freqtrade_exchange_patch(binding.venue)
    assert_no_production_host(overlay, venue=binding.venue,
                              where=f"freqtrade-{sleeve}.mode.json")
```

For `Venue.DEMO` that yields exactly:

```json
"exchange": {
  "demo_trading": true,
  "_ft_has_params": {"supports_demo_trading": true}
}
```

and **no** `urls` / `ccxt_config` key at all — the wholesale swap in ccxt is what routes
everything, including the websockets. Do not add a `ccxt_config` url override alongside
it; it can only make the map less complete.

Add `SCHEMA_REQUIRED_WHEN_PRESENT["exchange"] = ()` if `audit_optional_blocks` needs the
entry, and extend
`tests/test_ops/test_freqtrade_config_schema.py` to validate a demo overlay through
freqtrade's own validator.

### 6.4 `ops/preflight.py` + `ops/lib/binance_check.py` — venue-aware probes

Three changes, all blocking:

1. **New check `venue_binding`** (add to the check table at line ~56):
   `Credential + resolve_binding(request.target_mode, cred)` — a `VenueBindingError` is a
   blocking failure. Then `verify_credential_venue(cred, probe=httpx_account_probe)`; only
   `status == "confirmed"` passes. `cannot_verify` must **fail** the check, not skip it —
   that is the whole point of the tri-state.

2. **`binance_check.BASE_URL` must stop being a module constant pointed at production.**
   Thread the venue through instead:

   ```python
   # ops/lib/binance_check.py
   from ops.lib.exchange_endpoints import Venue, endpoints_for

   class BinanceClient:
       def __init__(self, keys, *, venue: Venue = Venue.LIVE, ...):
           self.base = endpoints_for(venue).rest_base
           self.venue = venue
   ```

   and pass `venue=` from `PreflightDeps` everywhere a probe is built.

3. **`_check_exchange_keys` cannot use `apiRestrictions` on demo** — that is a `sapi` path
   and demo returns nginx 404 (§4b). Make the check venue-aware:

   ```python
   ep = endpoints_for(venue)
   if not ep.supports_sapi:
       # Permissions cannot be read on demo. Record it as a WARNING with the reason,
       # and keep the blocking part that CAN be checked: /api/v3/account authenticates,
       # and verify_credential_venue confirms the key is demo-only.
   ```

   Do **not** let the check silently pass by treating the 404 as "no restrictions". The
   operator compensates by minting the demo key withdrawal-disabled in the UI, and §7
   records that as a manual step.

`_check_stoploss_on_exchange` needs no logic change — only its `exchange_info` probe must
use the demo base. The filters and `orderTypes` are identical (§4/§4a), so the verdict is
unchanged.

### 6.5 `ops/docker-compose.testnet.yml` — delete the dead line, add a demo file

The current override is broken (§3a). Replace the file with `ops/docker-compose.demo.yml`:

```yaml
# Demo-mode override for sleeve A. Real round trips against Binance Spot Demo Mode.
#   docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d freqtrade-a
# Requires DEMO keys in .env (BINANCE_KEY_A/BINANCE_SECRET_A from
# Binance Demo Trading -> API Key Management). Trade-enabled, withdrawal-DISABLED.
#
# NOTE: there is deliberately no FREQTRADE__EXCHANGE__URLS__* line. freqtrade reads no
# such key (verified against 2026.8) — the old testnet override was a silent no-op.
# Routing comes from exchange.demo_trading in var/runtime/freqtrade-a.mode.json.
services:
  freqtrade-a:
    environment:
      FREQTRADE__EXCHANGE__KEY: "${BINANCE_KEY_A}"
      FREQTRADE__EXCHANGE__SECRET: "${BINANCE_SECRET_A}"
```

`dry_run: false`, `demo_trading` and `_ft_has_params` all arrive through the mode overlay,
so compose never decides live-ness — the existing invariant is preserved exactly.

If a testnet override must be kept for history, fix it the only way that works:

```yaml
      FREQTRADE__EXCHANGE__CCXT_CONFIG__urls__api__public: "https://testnet.binance.vision/api/v3"
      # ...and the other 21 keys. Which is why you should use the module instead.
```

### 6.6 `ops/lib/compose.py` — the venue must be visible

`write_live_file` / the `compose.override.yml` renderer should stamp the venue into the
generated header comment and into `EARN_VENUE` on each service, so `docker inspect` and
the console both show which Binance a container is talking to without reading a JSON file.
Add `assert_no_production_host(rendered, venue=binding.venue)` before writing.

### 6.7 `console/` — the Mode page

Demo needs its own colour and its own confirm phrase, and the Invariants page should show
the bound venue and the `blackholed_keys(venue)` list read-only. A demo sleeve must never
render with the live styling; the whole point is that an operator can tell at a glance.

---

## 7. Operator steps to add the demo key

1. Binance account UI → **Demo Trading** → **API Key Management** → create key.
2. **Enable spot trading. Disable withdrawals.** Disable futures, margin, internal
   transfer and universal transfer. (This is manual and it matters more on demo than
   anywhere, because §4b means preflight cannot read the permissions back.)
3. Put the key in `.env` as `BINANCE_KEY_A` / `BINANCE_SECRET_A`, and set
   `BINANCE_VENUE_A=demo`. Never paste it into a config file, a prompt or chat.
4. Set `exchange.venue: demo` in `config/earn.yaml` and re-bless the protected config.
5. Run the preflight. The `venue_binding` check must report `confirmed` — meaning the key
   authenticates against `demo-api.binance.com` **and is refused by `api.binance.com`**.
6. Transition sleeve A to `DEMO_PROPOSE` from the console with the typed phrase. Step 10
   must show `"exchange": "binance_demo"` in `/show_config`.
7. The live key stays out of `.env` entirely until the demo run has produced weeks of
   evidence.

---

## 8. How this goes silently wrong, and what stops it

| Failure | Why it is plausible | What prevents it |
|---|---|---|
| **Partial URL override** — spot points at demo, `sapi`/`papi`/`fapi` still at production | ccxt *deep-merges* the constructor `urls`; even copying `urls['demo']` wholesale leaves 11 production keys (§2a) | `ccxt_url_overrides` emits **all 22** keys and blackholes the unsupported ones to a `.invalid` host; `test_no_demo_or_testnet_url_can_reach_a_production_host` asserts it; `assert_no_production_host` sweeps the rendered config before it is written |
| **Sandbox-mode confusion** — someone reaches for `set_sandbox_mode(True)` | It is the obvious-looking switch and it is what the old testnet path used | `Venue.TESTNET` is a *separate* venue with its own hosts; the demo path never calls sandbox mode, and ccxt itself raises `NotSupported` if both are set (`binance.py:3109`) |
| **`FREQTRADE__EXCHANGE__URLS__API` assumed to work** | It is already in our repo and looks plausible; freqtrade's schema accepts it without complaint | §3a documents it as a no-op; §6.5 deletes the line; the demo path uses `demo_trading`, whose effect is *observable* in `/show_config` as `binance_demo` |
| **`demo_trading: true` rejected at boot** | freqtrade ships `supports_demo_trading: False` for binance (`binance.py:52`) | `freqtrade_exchange_patch(Venue.DEMO)` always emits the `_ft_has_params` unlock alongside it; `test_demo_patch_unlocks_freqtrades_disabled_binance_switch` pins the pair together |
| **A live key accepted in demo mode** | Both keys exist in the owner's hands right now; one `.env` edit apart | `resolve_binding` refuses on a venue/mode disagreement with a typed error before any config is built, and `verify_credential_venue` proves the key is *refused* by the other venue rather than trusting the label |
| **A demo key silently used for live** | Same edit, opposite direction; it would fail auth loudly, but only after the mode switch | Same two mechanisms, symmetric by construction |
| **Websocket still on production** while REST is on demo | ws urls are a *nested* map under `urls['api']['ws']`; a REST-only override misses them entirely, and freqtrade builds a third ccxt client (`_ws_async`, `exchange.py:290`) from the same config | `enable_demo_trading` swaps the whole `urls['api']`, ws included (verified in §2a); `ccxt_ws_overrides` covers all 9 + 3 keys for the fallback route; `test_demo_websockets_never_point_at_production` asserts it |
| **`dry_run` + `demo_trading` crash at boot** | our committed configs always say `dry_run: true` | `demo_trading` lives only in `var/runtime/freqtrade-<s>.mode.json`, rendered only from a verified mode state — §6.3 |
| **Preflight "passes" on demo because sapi 404s** | `apiRestrictions` is a sapi path and demo returns nginx 404 (§4b); a naive `except` reads as "no restrictions" | §6.4 makes the check venue-aware and requires an explicit warning with the reason; withdrawal-disabled becomes a recorded manual step (§7) |
| **Preflight "passes" because the network was down** | a probe that cannot complete looks like a probe that found nothing | `verify_credential_venue` returns `cannot_verify` — a distinct third state that §6.4 requires to **fail** the check. `confirmed` needs the positive *and* every negative |
| **Balance reset mid-run** | demo balances reset when the owner asks, and that will happen | The reset must be treated as a run boundary: it changes NAV discontinuously and would corrupt drawdown, the daily/monthly loss stops and TCA. Take the reconcile step's balance baseline at run open, and have `ops/healthcheck.py` raise a critical alert and engage the kill switch on an unexplained NAV jump larger than the daily loss stop. **Never reset demo balances while a sleeve is in a demo mode** — drop to TEST, reset, then re-arm with a new `run_id`. Add this to `/ops-runbook` |
| **Demo prices mistaken for live execution evidence** | they match to ~0.3 bps (§4), which is seductive | Demo is an independent matching engine. TCA should mark demo fills as venue `demo` in the journal so the monthly cost calibration never mixes them with live fills |
| **A ccxt upgrade adds a new URL key** | new key defaults to production and nothing notices | `PRODUCTION_URL_KEYS` is pinned in the module and `test_override_covers_every_production_key` / `test_pinned_key_tiers_do_not_overlap_and_cover_the_whole_set` fail on drift. Worth adding a test that diffs the pin against the installed ccxt |
| **A new mode added without a venue** | `ALLOWED` and `MODE_VENUE` are two tables | `venue_for_mode` raises `VenueBindingError` on anything not in `MODE_VENUE`; `test_an_unlisted_mode_fails_closed` pins it. Add a cross-test asserting `set(modes.ALLOWED) == set(MODE_VENUE) | TRANSIENT_MODES` |
