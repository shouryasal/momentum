"""``ops.lib.exchange_endpoints`` — venue mapping, the binding refusal, and verification.

No network: the only networked function in the module is ``httpx_account_probe`` and
nothing here constructs one. Every verification test injects a fake ``Prober``.

The point of most of these tests is the *refusal* path. A module whose job is to make a
mistake impossible is only as good as the proof that it says no.
"""

from __future__ import annotations

import pytest

from ops.lib import exchange_endpoints as ee

# --------------------------------------------------------------------------- venue table


def test_every_venue_has_endpoints() -> None:
    assert set(ee.VENUES) == set(ee.Venue)
    for venue, ep in ee.VENUES.items():
        assert ep.venue is venue
        assert ep.rest_base.startswith("https://")
        assert not ep.rest_base.endswith("/")
        assert ep.ws_stream.startswith("wss://")
        assert ep.ws_api.startswith("wss://")


def test_demo_is_not_testnet_and_neither_is_live() -> None:
    """The whole reason this module exists: three distinct hosts, no aliasing."""
    hosts = {v: ee.VENUES[v].rest_host for v in ee.Venue}
    assert hosts[ee.Venue.LIVE] == "api.binance.com"
    assert hosts[ee.Venue.DEMO] == "demo-api.binance.com"
    assert hosts[ee.Venue.TESTNET] == "testnet.binance.vision"
    assert len(set(hosts.values())) == 3


def test_demo_websocket_hosts_match_the_documented_ones() -> None:
    ep = ee.endpoints_for(ee.Venue.DEMO)
    assert ep.ws_stream == "wss://demo-stream.binance.com/ws"
    assert ep.ws_api == "wss://demo-ws-api.binance.com/ws-api/v3"
    assert ep.spot_api_v3 == "https://demo-api.binance.com/api/v3"


def test_only_live_serves_sapi() -> None:
    """Verified against the wire: demo-api.binance.com/sapi/... answers HTTP 404."""
    assert ee.endpoints_for(ee.Venue.LIVE).supports_sapi is True
    assert ee.endpoints_for(ee.Venue.DEMO).supports_sapi is False
    assert ee.endpoints_for(ee.Venue.TESTNET).supports_sapi is False


def test_no_venue_serves_futures_because_earn_is_spot_only() -> None:
    assert not any(ep.supports_futures for ep in ee.VENUES.values())


@pytest.mark.parametrize("raw", ["demo", "DEMO", " Demo ", ee.Venue.DEMO])
def test_parse_venue_accepts_operator_spellings(raw: object) -> None:
    assert ee.parse_venue(raw) is ee.Venue.DEMO  # type: ignore[arg-type]


def test_parse_venue_refuses_the_unknown_rather_than_defaulting() -> None:
    with pytest.raises(ee.VenueBindingError) as exc:
        ee.parse_venue("sandbox")
    assert "unknown venue" in str(exc.value)
    assert "live, demo, testnet" in str(exc.value)


# --------------------------------------------------------------------------- ccxt urls


@pytest.mark.parametrize("venue", list(ee.Venue))
def test_override_covers_every_production_key(venue: ee.Venue) -> None:
    """A partial override is the bug. Every key ccxt knows must get a value."""
    urls = ee.ccxt_url_overrides(venue)
    assert set(urls) == set(ee.PRODUCTION_URL_KEYS)
    assert all(urls.values()), "no key may be empty"


def test_pinned_key_tiers_do_not_overlap_and_cover_the_whole_set() -> None:
    tiers = (ee.SPOT_URL_KEYS, ee.SAPI_URL_KEYS, ee.FUTURES_URL_KEYS)
    joined = [k for tier in tiers for k in tier]
    assert len(joined) == len(set(joined)), "a key may belong to exactly one tier"
    assert set(joined) == set(ee.PRODUCTION_URL_KEYS)


def test_demo_override_routes_spot_to_demo_and_blackholes_the_rest() -> None:
    urls = ee.ccxt_url_overrides(ee.Venue.DEMO)
    assert urls["public"] == "https://demo-api.binance.com/api/v3"
    assert urls["private"] == "https://demo-api.binance.com/api/v3"
    assert urls["v1"] == "https://demo-api.binance.com/api/v1"
    for key in ee.SAPI_URL_KEYS + ee.FUTURES_URL_KEYS:
        assert urls[key] == ee.BLACKHOLE_URL, key


def test_no_demo_or_testnet_url_can_reach_a_production_host() -> None:
    """The dangerous case, asserted directly: nothing falls back to production."""
    for venue in (ee.Venue.DEMO, ee.Venue.TESTNET):
        for key, url in ee.ccxt_url_overrides(venue).items():
            assert "binance.com" not in url or url.startswith("https://demo-"), (venue, key, url)
            assert not url.startswith("https://api.binance.com"), (venue, key, url)
            assert not url.startswith("https://fapi."), (venue, key, url)
            assert not url.startswith("https://papi."), (venue, key, url)


def test_blackhole_host_is_unresolvable_by_construction() -> None:
    """RFC 2606 reserves .invalid, so a leaked call fails DNS instead of hitting Binance."""
    assert ee.BLACKHOLE_URL.endswith(".invalid")
    assert ee.BLACKHOLE_WS.split("/")[2].endswith(".invalid")


def test_live_override_is_the_real_production_map() -> None:
    urls = ee.ccxt_url_overrides(ee.Venue.LIVE)
    assert urls["public"] == "https://api.binance.com/api/v3"
    assert urls["sapi"] == "https://api.binance.com/sapi/v1"
    # Spot-only still holds: futures stay disabled even in live.
    assert urls["fapiPrivate"] == ee.BLACKHOLE_URL


def test_blackholed_keys_reports_what_is_disabled() -> None:
    demo = set(ee.blackholed_keys(ee.Venue.DEMO))
    assert demo == set(ee.SAPI_URL_KEYS) | set(ee.FUTURES_URL_KEYS)
    assert set(ee.blackholed_keys(ee.Venue.LIVE)) == set(ee.FUTURES_URL_KEYS)


@pytest.mark.parametrize("venue", list(ee.Venue))
def test_ws_override_covers_every_ws_key(venue: ee.Venue) -> None:
    ws = ee.ccxt_ws_overrides(venue)
    assert set(ws) == set(ee.WS_URL_KEYS) | {"ws-api"}
    assert set(ws["ws-api"]) == set(ee.WS_API_URL_KEYS)


def test_demo_websockets_never_point_at_production() -> None:
    ws = ee.ccxt_ws_overrides(ee.Venue.DEMO)
    assert ws["spot"] == "wss://demo-stream.binance.com/ws"
    assert ws["margin"] == "wss://demo-stream.binance.com/ws"
    assert ws["ws-api"]["spot"] == "wss://demo-ws-api.binance.com/ws-api/v3"
    for key in ("future", "delivery", "option", "optionMarket", "optionPrivate", "papi", "stock"):
        assert ws[key] == ee.BLACKHOLE_WS, key


def test_ccxt_config_shape_is_what_ccxt_and_freqtrade_expect() -> None:
    cfg = ee.ccxt_config(ee.Venue.DEMO)
    assert set(cfg) == {"urls"}
    assert set(cfg["urls"]) == {"api"}
    api = cfg["urls"]["api"]
    assert set(api) == set(ee.PRODUCTION_URL_KEYS) | {"ws"}


# ------------------------------------------------------- freqtrade patch


def test_demo_patch_unlocks_freqtrades_disabled_binance_switch() -> None:
    """freqtrade 2026.8 ships binance._ft_has["supports_demo_trading"] = False."""
    patch = ee.freqtrade_exchange_patch(ee.Venue.DEMO)
    assert patch["demo_trading"] is True
    assert patch["_ft_has_params"] == {"supports_demo_trading": True}
    assert "ccxt_config" not in patch, "demo uses ccxt's wholesale swap, not a merge"


def test_live_patch_turns_demo_off_explicitly() -> None:
    assert ee.freqtrade_exchange_patch(ee.Venue.LIVE) == {"demo_trading": False}


def test_testnet_patch_falls_back_to_a_complete_ccxt_override() -> None:
    patch = ee.freqtrade_exchange_patch(ee.Venue.TESTNET)
    assert patch["demo_trading"] is False
    api = patch["ccxt_config"]["urls"]["api"]
    assert api["public"] == "https://testnet.binance.vision/api/v3"
    assert api["sapi"] == ee.BLACKHOLE_URL


# ------------------------------------------------------- leak sweep


def test_leak_sweep_passes_a_correctly_bound_demo_config() -> None:
    ee.assert_no_production_host(ee.ccxt_config(ee.Venue.DEMO), venue=ee.Venue.DEMO)


def test_leak_sweep_catches_a_half_patched_config() -> None:
    bad = ee.ccxt_config(ee.Venue.DEMO)
    bad["urls"]["api"]["sapi"] = "https://api.binance.com/sapi/v1"
    with pytest.raises(ee.VenueBindingError) as exc:
        ee.assert_no_production_host(bad, venue=ee.Venue.DEMO, where="freqtrade-a.json")
    assert "freqtrade-a.json" in str(exc.value)
    assert "sapi" in str(exc.value)


def test_leak_sweep_walks_lists_and_nested_maps() -> None:
    payload = {"services": [{"env": {"url": "wss://stream.binance.com:9443/ws"}}]}
    with pytest.raises(ee.VenueBindingError):
        ee.assert_no_production_host(payload, venue=ee.Venue.DEMO)


def test_leak_sweep_is_a_no_op_for_live() -> None:
    ee.assert_no_production_host(
        {"url": "https://api.binance.com/api/v3"}, venue=ee.Venue.LIVE
    )


# --------------------------------------------------------------------------- credential


def test_credential_never_leaks_the_secret() -> None:
    cred = ee.Credential("BINANCE_KEY_A", ee.Venue.DEMO, key="abcd1234wxyz", secret="s3cr3t")
    for text in (repr(cred), str(cred), str(cred.describe())):
        assert "s3cr3t" not in text
        assert "abcd1234wxyz" not in text
    assert cred.last4 == "wxyz"
    assert cred.present is True


def test_credential_needs_both_halves_to_be_present() -> None:
    assert not ee.Credential("K", ee.Venue.DEMO, key="k", secret="").present
    assert not ee.Credential("K", ee.Venue.DEMO, key="", secret="s").present
    assert ee.Credential("K", ee.Venue.DEMO).last4 == ""


# --------------------------------------------------------------------------- binding


def _cred(venue: ee.Venue, label: str = "BINANCE_KEY_A") -> ee.Credential:
    return ee.Credential(label, venue, key="AAAABBBBCCCC", secret="ssss")


def test_mode_venue_table_covers_every_mode_and_names_a_known_venue() -> None:
    for mode, venue in ee.MODE_VENUE.items():
        assert mode == mode.upper()
        assert venue is None or venue in ee.VENUES
    assert ee.MODE_VENUE["TEST"] is None
    assert ee.MODE_VENUE["DEMO_EXECUTE"] is ee.Venue.DEMO
    assert ee.MODE_VENUE["LIVE_EXECUTE"] is ee.Venue.LIVE


@pytest.mark.parametrize(
    ("mode", "venue"),
    [("DEMO_PROPOSE", ee.Venue.DEMO), ("DEMO_EXECUTE", ee.Venue.DEMO),
     ("LIVE_PROPOSE", ee.Venue.LIVE), ("LIVE_EXECUTE", ee.Venue.LIVE),
     ("TESTNET_REHEARSAL", ee.Venue.TESTNET)],
)
def test_matching_mode_and_venue_bind(mode: str, venue: ee.Venue) -> None:
    binding = ee.resolve_binding(mode, _cred(venue))
    assert binding.venue is venue
    assert binding.endpoints is not None
    assert binding.endpoints.rest_base == ee.VENUES[venue].rest_base
    assert binding.needs_credential is True
    assert binding.describe()["venue"] == venue.value


def test_mode_is_normalised_so_case_is_not_a_trap() -> None:
    assert ee.resolve_binding("demo_execute", _cred(ee.Venue.DEMO)).mode == "DEMO_EXECUTE"


def test_test_mode_binds_to_no_venue_at_all() -> None:
    binding = ee.resolve_binding("TEST")
    assert binding.venue is None
    assert binding.endpoints is None
    assert binding.credential is None
    assert binding.needs_credential is False


def test_test_mode_tolerates_an_empty_credential_object() -> None:
    empty = ee.Credential("BINANCE_KEY_A", ee.Venue.DEMO)
    assert ee.resolve_binding("TEST", empty).venue is None


# ------------------------------------------------------- the refusals


def test_live_key_in_demo_mode_is_refused() -> None:
    """The mistake this module exists to prevent."""
    with pytest.raises(ee.VenueBindingError) as exc:
        ee.resolve_binding("DEMO_EXECUTE", _cred(ee.Venue.LIVE))
    msg = str(exc.value)
    assert "REFUSED" in msg
    assert "demo-api.binance.com" in msg
    assert "api.binance.com" in msg
    assert "BINANCE_KEY_A" in msg
    assert "...CCCC" in msg or "CCCC" in msg
    assert "ssss" not in msg


def test_demo_key_in_live_mode_is_refused() -> None:
    with pytest.raises(ee.VenueBindingError) as exc:
        ee.resolve_binding("LIVE_EXECUTE", _cred(ee.Venue.DEMO))
    msg = str(exc.value)
    assert "REFUSED" in msg
    assert "LIVE_PROPOSE or LIVE_EXECUTE" not in msg  # it should suggest the DEMO modes
    assert "DEMO_EXECUTE" in msg and "DEMO_PROPOSE" in msg


def test_testnet_key_in_demo_mode_is_refused() -> None:
    with pytest.raises(ee.VenueBindingError):
        ee.resolve_binding("DEMO_EXECUTE", _cred(ee.Venue.TESTNET))


def test_a_key_in_dry_run_mode_is_refused() -> None:
    with pytest.raises(ee.VenueBindingError) as exc:
        ee.resolve_binding("TEST", _cred(ee.Venue.DEMO))
    assert "dry-run" in str(exc.value)
    assert "Remove the key" in str(exc.value)


def test_a_venue_mode_without_a_key_is_refused_with_where_to_get_one() -> None:
    with pytest.raises(ee.VenueBindingError) as exc:
        ee.resolve_binding("DEMO_EXECUTE", None)
    msg = str(exc.value)
    assert "demo-api.binance.com" in msg
    assert "Demo Trading" in msg
    assert "withdrawals" in msg


def test_a_venue_mode_with_a_half_set_key_is_refused() -> None:
    half = ee.Credential("BINANCE_KEY_B", ee.Venue.DEMO, key="AAAA", secret="")
    with pytest.raises(ee.VenueBindingError) as exc:
        ee.resolve_binding("DEMO_PROPOSE", half)
    assert "BINANCE_KEY_B is not set" in str(exc.value)


def test_transient_modes_can_never_produce_a_config() -> None:
    for mode in ("ARMING", "DISARMING"):
        with pytest.raises(ee.VenueBindingError) as exc:
            ee.resolve_binding(mode, _cred(ee.Venue.LIVE))
        assert "transient" in str(exc.value)


def test_an_unlisted_mode_fails_closed() -> None:
    with pytest.raises(ee.VenueBindingError) as exc:
        ee.resolve_binding("LIVE_YOLO", _cred(ee.Venue.LIVE))
    assert "unknown mode" in str(exc.value)
    assert "MODE_VENUE" in str(exc.value)


def test_venue_for_mode_is_usable_on_its_own() -> None:
    assert ee.venue_for_mode("TEST") is None
    assert ee.venue_for_mode("demo_propose") is ee.Venue.DEMO


# --------------------------------------------------------------------------- verification


def _prober(table: dict[ee.Venue, ee.ProbeVerdict]) -> ee.Prober:
    def probe(venue: ee.Venue, credential: ee.Credential) -> ee.ProbeVerdict:
        assert credential.present, "the prober is only called with a usable key"
        return table.get(venue, "rejected")

    return probe


def test_a_key_that_authenticates_only_where_it_claims_is_confirmed() -> None:
    proof = ee.verify_credential_venue(
        _cred(ee.Venue.DEMO),
        probe=_prober({ee.Venue.DEMO: "authenticated",
                       ee.Venue.LIVE: "rejected",
                       ee.Venue.TESTNET: "rejected"}),
    )
    assert proof.ok
    assert proof.status == "confirmed"
    assert proof.authenticated_at == (ee.Venue.DEMO,)
    assert "demo-api.binance.com" in proof.detail
    assert proof.to_json()["claimed"] == "demo"


def test_a_key_that_also_authenticates_live_is_a_mismatch_not_a_pass() -> None:
    """A demo-labelled key accepted by production is the nightmare; catch it here."""
    proof = ee.verify_credential_venue(
        _cred(ee.Venue.DEMO),
        probe=_prober({ee.Venue.DEMO: "authenticated",
                       ee.Venue.LIVE: "authenticated",
                       ee.Venue.TESTNET: "rejected"}),
    )
    assert not proof.ok
    assert proof.status == "mismatch"
    assert "DANGER" in proof.detail
    assert "api.binance.com" in proof.detail
    assert set(proof.authenticated_at) == {ee.Venue.DEMO, ee.Venue.LIVE}


def test_a_key_its_claimed_venue_rejects_is_a_mismatch() -> None:
    proof = ee.verify_credential_venue(
        _cred(ee.Venue.DEMO),
        probe=_prober({ee.Venue.DEMO: "rejected",
                       ee.Venue.LIVE: "rejected",
                       ee.Venue.TESTNET: "rejected"}),
    )
    assert proof.status == "mismatch"
    assert "rejects it" in proof.detail


def test_no_key_returns_cannot_verify_and_never_confirmed() -> None:
    proof = ee.verify_credential_venue(
        ee.Credential("BINANCE_KEY_A", ee.Venue.DEMO), probe=_prober({})
    )
    assert proof.status == "cannot_verify"
    assert not proof.ok
    assert "not set" in proof.detail


def test_no_credential_object_at_all_returns_cannot_verify() -> None:
    proof = ee.verify_credential_venue(None, probe=_prober({}))
    assert proof.status == "cannot_verify"
    assert not proof.ok
    assert proof.probed == {}


def test_an_unreachable_claimed_venue_cannot_verify() -> None:
    proof = ee.verify_credential_venue(
        _cred(ee.Venue.DEMO),
        probe=_prober({ee.Venue.DEMO: "unreachable",
                       ee.Venue.LIVE: "rejected",
                       ee.Venue.TESTNET: "rejected"}),
    )
    assert proof.status == "cannot_verify"
    assert "could not be reached" in proof.detail


def test_an_unreachable_other_venue_cannot_prove_the_negative() -> None:
    """Authenticating at demo is only half the proof; being refused live is the other."""
    proof = ee.verify_credential_venue(
        _cred(ee.Venue.DEMO),
        probe=_prober({ee.Venue.DEMO: "authenticated",
                       ee.Venue.LIVE: "unreachable",
                       ee.Venue.TESTNET: "rejected"}),
    )
    assert proof.status == "cannot_verify"
    assert not proof.ok
    assert "unproven" in proof.detail


def test_verification_probes_every_venue_by_default() -> None:
    seen: list[ee.Venue] = []

    def probe(venue: ee.Venue, credential: ee.Credential) -> ee.ProbeVerdict:
        seen.append(venue)
        return "authenticated" if venue is ee.Venue.DEMO else "rejected"

    ee.verify_credential_venue(_cred(ee.Venue.DEMO), probe=probe)
    assert set(seen) == set(ee.Venue)


def test_verification_can_be_narrowed_for_a_cheaper_check() -> None:
    proof = ee.verify_credential_venue(
        _cred(ee.Venue.DEMO),
        probe=_prober({ee.Venue.DEMO: "authenticated", ee.Venue.LIVE: "rejected"}),
        venues=(ee.Venue.DEMO, ee.Venue.LIVE),
    )
    assert proof.status == "confirmed"
    assert set(proof.probed) == {"demo", "live"}


def test_proof_json_is_serialisable_and_carries_no_key_material() -> None:
    proof = ee.verify_credential_venue(
        _cred(ee.Venue.DEMO),
        probe=_prober({ee.Venue.DEMO: "authenticated",
                       ee.Venue.LIVE: "rejected",
                       ee.Venue.TESTNET: "rejected"}),
    )
    blob = str(proof.to_json())
    assert "ssss" not in blob
    assert "AAAABBBBCCCC" not in blob
    assert proof.to_json()["authenticated_at"] == ["demo"]


# --------------------------------------------------------------------------- ccxt drift

# The module pins ccxt's URL shapes as constants rather than importing ccxt, so that a
# library upgrade which adds a URL key fails HERE instead of quietly defaulting that key to
# production. These tests are what make that promise true. ccxt is a freqtrade dependency
# and is absent from the in-container stdlib-only world, hence importorskip.


def test_pinned_keys_match_installed_ccxt() -> None:
    ccxtpro = pytest.importorskip("ccxt.pro")
    api = ccxtpro.binance().describe()["urls"]["api"]
    assert set(api) - {"ws"} == set(ee.PRODUCTION_URL_KEYS)
    ws = api["ws"]
    assert set(ws) - {"ws-api"} == set(ee.WS_URL_KEYS)
    assert set(ws["ws-api"]) == set(ee.WS_API_URL_KEYS)


def test_pinned_production_values_match_installed_ccxt() -> None:
    ccxtpro = pytest.importorskip("ccxt.pro")
    api = ccxtpro.binance().describe()["urls"]["api"]
    live = ee.ccxt_url_overrides(ee.Venue.LIVE)
    for key in ee.SPOT_URL_KEYS + ee.SAPI_URL_KEYS:
        assert live[key] == api[key], key


def test_venue_hosts_match_ccxts_own_demo_and_test_maps() -> None:
    """Our demo/testnet hosts are not folklore — they are ccxt's, and the docs'."""
    ccxtpro = pytest.importorskip("ccxt.pro")
    urls = ccxtpro.binance().describe()["urls"]
    for venue, block in ((ee.Venue.DEMO, urls["demo"]), (ee.Venue.TESTNET, urls["test"])):
        ep = ee.endpoints_for(venue)
        assert block["public"] == ep.spot_api_v3
        assert block["private"] == ep.spot_api_v3
        assert block["ws"]["spot"] == ep.ws_stream
        assert block["ws"]["ws-api"]["spot"] == ep.ws_api


def test_ccxt_sandbox_mode_still_means_testnet_not_demo() -> None:
    """If a ccxt release ever repoints sandbox at demo, this fails and the doc is stale."""
    ccxtmod = pytest.importorskip("ccxt")
    s = ccxtmod.binance()
    s.set_sandbox_mode(True)
    assert s.urls["api"]["public"] == ee.endpoints_for(ee.Venue.TESTNET).spot_api_v3
    assert "demo" not in s.urls["api"]["public"]


def test_a_full_ccxt_url_override_still_deep_merges_production_sapi() -> None:
    """The finding the whole module is built around, asserted against the real library."""
    ccxtmod = pytest.importorskip("ccxt")
    demo_map = ccxtmod.binance().describe()["urls"]["demo"]
    leaky = ccxtmod.binance({"urls": {"api": demo_map}})
    assert leaky.urls["api"]["sapi"] == "https://api.binance.com/sapi/v1", (
        "ccxt merges rather than replaces; if this ever changes, ccxt_url_overrides "
        "may be simplified"
    )
    # Ours does not leak, because it names every key.
    safe = ccxtmod.binance({"urls": {"api": ee.ccxt_url_overrides(ee.Venue.DEMO)}})
    for key in ee.PRODUCTION_URL_KEYS:
        assert "api.binance.com" not in safe.urls["api"][key] or key in ee.SPOT_URL_KEYS
        assert not safe.urls["api"][key].startswith("https://api.binance.com")


def test_enable_demo_trading_is_the_wholesale_swap_we_rely_on() -> None:
    ccxtmod = pytest.importorskip("ccxt")
    api = ccxtmod.binance()
    api.enable_demo_trading(True)
    assert api.urls["api"]["public"] == ee.endpoints_for(ee.Venue.DEMO).spot_api_v3
    assert "sapi" not in api.urls["api"], "the swap must drop sapi entirely"
    assert "papi" not in api.urls["api"]
    assert api.options.get("enableDemoTrading") is True


# --------------------------------------------------------------------------- signing


def test_signed_query_is_deterministic_and_carries_the_signature() -> None:
    qs = ee.signed_query("secret", {"omitZeroBalances": "true"}, now_ms=1700000000000)
    assert qs == ee.signed_query("secret", {"omitZeroBalances": "true"}, now_ms=1700000000000)
    assert "timestamp=1700000000000" in qs
    assert "recvWindow=5000" in qs
    assert "&signature=" in qs
    assert "secret" not in qs.split("&signature=")[0]


def test_a_different_secret_produces_a_different_signature() -> None:
    a = ee.signed_query("one", {}, now_ms=1)
    b = ee.signed_query("two", {}, now_ms=1)
    assert a != b
    assert a.split("&signature=")[0] == b.split("&signature=")[0]
