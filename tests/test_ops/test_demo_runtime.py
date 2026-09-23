"""DEMO as a first-class rendered mode, and the venue binding that makes it safe.

What is actually being pinned here:

* a demo sleeve renders ``demo_trading`` **together with** the ``_ft_has_params`` unlock,
  never one without the other — freqtrade ships ``binance.supports_demo_trading = False``
  and refuses the key on its own;
* a demo sleeve renders ``dry_run: false`` — freqtrade refuses ``demo_trading`` and
  ``dry_run`` together, so "demo" and "paper" are different things and the config says so;
* no rendered artefact carries an exchange URL in any form. That is the bug this whole
  package exists because of: ``docker-compose.testnet.yml`` set
  ``FREQTRADE__EXCHANGE__URLS__API``, freqtrade reads ``exchange.urls`` from nowhere, and
  the "testnet rehearsal" therefore sent testnet credentials to the production host;
* a container is only ever handed its own venue's credential NAMES, and a demo service
  block does not so much as mention ``BINANCE_KEY_A``;
* every one of those is a **render-time** refusal, so the wrong config cannot be produced,
  never mind started.

Evidence for every "freqtrade does X" claim: ``docs/design/demo-mode.md``, measured against
the installed ccxt 4.5.82 / freqtrade 2026.8.
"""

import json

import pytest

from ops.config import load_config
from ops.gen_freqtrade_config import (
    RenderError,
    build_bot_config,
    build_compose_override,
    build_mode_overlay,
    render_runtime,
    venue_of,
)
from ops.lib import compose as composelib
from ops.lib import mode_state as ms
from ops.lib.exchange_endpoints import (
    Venue,
    credential_env_names,
    endpoints_for,
)

DEMO_HOST = "demo-api.binance.com"
LIVE_HOST = "api.binance.com"


@pytest.fixture(scope="module")
def cfg():
    return load_config()


def _uncommented(text: str) -> str:
    """The parts of a compose layer that can actually do something. The generated header
    explains both venues' credential names, and a comment cannot hand a container a key."""
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def _state(a="TEST", b="TEST", *, verified=True, seed=500.0):
    def sleeve(mode):
        return ms.SleeveState(
            state=mode,
            submode=None if mode == "TEST" else "propose",
            run_id=f"r-{mode.lower()}",
            seed_usdt=None if mode == "TEST" else seed,
        )

    return ms.ModeState(
        sleeves={"a": sleeve(a), "b": sleeve(b)},
        verified=verified,
        reason=ms.REASON_OK if verified else ms.REASON_BAD_SIGNATURE,
        set_at="2026-09-23T00:00:00Z",
        set_by="human:test",
    )


# --------------------------------------------------------------------- the overlay


class TestDemoOverlay:
    def test_demo_carries_both_halves_of_the_switch(self, cfg):
        """``demo_trading`` alone does not start; the unlock alone does not route."""
        overlay = build_mode_overlay(cfg, "a", _state(a="DEMO_PROPOSE"))
        assert overlay["exchange"] == {
            "demo_trading": True,
            "_ft_has_params": {"supports_demo_trading": True},
        }

    def test_demo_is_not_dry_run(self, cfg):
        """freqtrade's config_validation refuses demo_trading + dry_run outright."""
        overlay = build_mode_overlay(cfg, "a", _state(a="DEMO_EXECUTE"))
        assert overlay["dry_run"] is False
        assert "dry_run_wallet" not in overlay
        assert overlay["bot_name"] == "earn-a-demo"

    def test_demo_places_its_stops_on_the_exchange(self, cfg):
        """Demo's orderTypes and filters are byte-identical to live (demo-mode.md §4a),
        so the live rule applies unchanged rather than being relaxed for a rehearsal."""
        overlay = build_mode_overlay(cfg, "a", _state(a="DEMO_PROPOSE"))
        assert overlay["order_types"]["stoploss_on_exchange"] is True

    def test_demo_overlay_has_no_url_override_of_any_kind(self, cfg):
        """A ccxt_config url map DEEP-MERGES, so a 'full' override still leaves sapi,
        papi and fapi on production. demo_trading makes ccxt ASSIGN the demo map."""
        overlay = build_mode_overlay(cfg, "a", _state(a="DEMO_PROPOSE"))
        assert "urls" not in overlay["exchange"]
        assert "ccxt_config" not in overlay["exchange"]
        assert LIVE_HOST not in json.dumps(overlay)
        assert DEMO_HOST not in json.dumps(overlay)

    def test_test_mode_is_untouched(self, cfg):
        overlay = build_mode_overlay(cfg, "a", _state(a="TEST"))
        assert overlay["dry_run"] is True
        assert "exchange" not in overlay
        assert overlay["bot_name"] == "earn-a-test"

    def test_live_gets_no_demo_key_and_no_exchange_block(self, cfg):
        """Production is ccxt's default; an overlay key that says 'demo_trading: false'
        is one careless edit away from saying the opposite."""
        overlay = build_mode_overlay(cfg, "b", _state(b="LIVE_PROPOSE"))
        assert overlay["dry_run"] is False
        assert "exchange" not in overlay
        assert overlay["bot_name"] == "earn-b-live"

    def test_committed_bot_config_gains_nothing(self, cfg):
        """The committed file must stay dry-run and demo-free: freqtrade would refuse to
        start if a demo key ever landed in it next to dry_run: true."""
        conf = build_bot_config(cfg, "a")
        assert conf["dry_run"] is True
        assert "demo_trading" not in conf["exchange"]
        assert "_ft_has_params" not in conf["exchange"]

    def test_a_demo_overlay_never_renders_from_an_unverified_mode_file(self, cfg):
        """Demo is not real money, so it is deliberately not in LIVE_MODES — but it does
        place real orders with a real key, so it gets the live rule anyway."""
        # ms.load() already replaces every sleeve with TEST on a bad signature, so this
        # shape is unreachable in production — which is exactly why it is asserted here.
        with pytest.raises(RenderError, match="unverified"):
            build_mode_overlay(cfg, "a", _state(a="DEMO_EXECUTE", verified=False))
        assert build_mode_overlay(cfg, "a", ms.default_state())["dry_run"] is True


class TestVenueOfMode:
    @pytest.mark.parametrize(
        "mode,venue",
        [
            ("TEST", None),
            ("DEMO_PROPOSE", Venue.DEMO),
            ("DEMO_EXECUTE", Venue.DEMO),
            ("LIVE_PROPOSE", Venue.LIVE),
            ("LIVE_EXECUTE", Venue.LIVE),
            ("ARMING", None),
            ("DISARMING", None),
        ],
    )
    def test_binding_table(self, mode, venue):
        assert venue_of(ms.SleeveState(state=mode)) is venue

    def test_an_unknown_mode_is_a_refusal_not_a_default(self):
        with pytest.raises(RenderError, match="unknown mode"):
            venue_of(ms.SleeveState(state="LIVE_YOLO"))


# --------------------------------------------------------------------- credentials


class TestComposeOverrideBindsCredentials:
    def test_demo_sleeve_sees_only_demo_names(self, cfg):
        text = build_compose_override(cfg, _state(a="DEMO_PROPOSE"), env={})
        text = _uncommented(text)
        assert "${BINANCE_DEMO_KEY}" in text
        assert "${BINANCE_DEMO_SECRET}" in text
        assert "BINANCE_KEY_A" not in text
        assert "BINANCE_SECRET_A" not in text
        assert 'EARN_VENUE: "demo"' in text

    def test_live_sleeve_sees_only_live_names(self, cfg):
        text = _uncommented(build_compose_override(cfg, _state(b="LIVE_EXECUTE"), env={}))
        assert "${BINANCE_KEY_B}" in text
        assert "BINANCE_DEMO_KEY" not in text
        assert 'EARN_VENUE: "live"' in text

    def test_test_sleeve_gets_no_credential_at_all(self, cfg):
        text = build_compose_override(cfg, _state(), env={})
        assert 'FREQTRADE__EXCHANGE__KEY: ""' in text
        assert "${BINANCE" not in text       # no reference of any venue's credential
        assert 'EARN_VENUE: "none"' in text

    def test_a_demo_sleeve_and_a_live_sleeve_stay_isolated(self, cfg):
        """Docker injects only the variables listed under a service, so the check is per
        service block: sleeve A on demo and sleeve B live is legal, and neither container
        can see the other's key."""
        text = _uncommented(
            build_compose_override(cfg, _state(a="DEMO_PROPOSE", b="LIVE_PROPOSE"), env={})
        )
        a_block, b_block = text.split("  freqtrade-b:")
        assert "BINANCE_DEMO_KEY" in a_block and "BINANCE_KEY_B" not in a_block
        assert "BINANCE_KEY_B" in b_block and "BINANCE_DEMO_KEY" not in b_block

    def test_an_unverified_mode_file_yields_no_credentials(self, cfg):
        text = build_compose_override(
            cfg, _state(a="DEMO_EXECUTE", b="LIVE_EXECUTE", verified=False), env={}
        )
        assert "${BINANCE" not in text
        assert text.count('EARN_VENUE: "none"') == 2

    def test_the_same_key_under_two_venues_is_refused(self, cfg):
        """The likeliest way the owner's live key ends up pointed at demo, and the only
        mislabelling detectable with no network at all."""
        env = {
            "BINANCE_DEMO_KEY": "SAMEKEY1111",
            "BINANCE_DEMO_SECRET": "s1",
            "BINANCE_KEY_A": "SAMEKEY1111",
            "BINANCE_SECRET_A": "s2",
        }
        with pytest.raises(RenderError, match="two venues"):
            build_compose_override(cfg, _state(a="DEMO_PROPOSE"), env=env)

    def test_a_distinct_demo_key_renders(self, cfg):
        env = {
            "BINANCE_DEMO_KEY": "DEMOKEY2222",
            "BINANCE_DEMO_SECRET": "s1",
            "BINANCE_KEY_A": "LIVEKEY3333",
            "BINANCE_SECRET_A": "s2",
        }
        text = build_compose_override(cfg, _state(a="DEMO_PROPOSE"), env=env)
        assert "${BINANCE_DEMO_KEY}" in text
        assert "LIVEKEY3333" not in text and "DEMOKEY2222" not in text  # names, never values

    def test_no_secret_value_is_ever_written(self, cfg, tmp_path):
        env = {"BINANCE_DEMO_KEY": "DEMOKEY2222", "BINANCE_DEMO_SECRET": "supersecret"}
        rendered = render_runtime(
            cfg, state=_state(a="DEMO_PROPOSE"), runtime_dir=tmp_path, env=env
        )
        blob = "\n".join(rendered.values())
        assert "supersecret" not in blob and "DEMOKEY2222" not in blob


# --------------------------------------------------------------------- the URL no-op


class TestNoLayerChoosesAVenue:
    def test_the_broken_testnet_override_is_gone(self):
        """It set FREQTRADE__EXCHANGE__URLS__API, which freqtrade reads from nowhere; the
        rehearsal it enabled was talking to production the whole time."""
        assert not (composelib.base_path().parent / "docker-compose.testnet.yml").exists()

    def test_every_committed_compose_layer_is_venue_blind(self):
        checked = composelib.audit_committed_layers()
        assert "docker-compose.yml" in checked
        assert "docker-compose.demo.yml" in checked
        assert "docker-compose.live.yml.in" in checked

    def test_the_demo_layer_uses_the_demo_credential_names(self):
        text = composelib.demo_path().read_text(encoding="utf-8")
        assert "${BINANCE_DEMO_KEY}" in text
        assert "${BINANCE_KEY_A}" not in text

    def test_a_url_override_in_a_layer_is_refused(self):
        bad = 'services:\n  freqtrade-a:\n    environment:\n      FREQTRADE__EXCHANGE__URLS__API: "x"\n'
        with pytest.raises(composelib.ComposeError, match="reads from nowhere"):
            composelib.assert_layer_chooses_no_venue(bad, where="bad.yml")

    def test_a_hardcoded_host_in_a_layer_is_refused(self):
        bad = f'services:\n  a:\n    environment:\n      X: "https://{LIVE_HOST}/api"\n'
        with pytest.raises(composelib.ComposeError, match="hardcodes exchange host"):
            composelib.assert_layer_chooses_no_venue(bad, where="bad.yml")

    def test_a_comment_naming_the_host_is_allowed(self):
        """A comment cannot route traffic, and the generated override names the venue's
        host in one so an operator reading the file knows which Binance it is bound to."""
        ok = f"# bound to {DEMO_HOST}\nservices:\n  a:\n    environment:\n      EARN_VENUE: \"demo\"\n"
        composelib.assert_layer_chooses_no_venue(ok, where="ok.yml")


# --------------------------------------------------------------------- venue facts


class TestVenueFacts:
    def test_demo_and_live_are_different_hosts(self):
        assert endpoints_for(Venue.DEMO).rest_host == DEMO_HOST
        assert endpoints_for(Venue.LIVE).rest_host == LIVE_HOST

    def test_demo_serves_no_sapi_tier(self):
        """So the key-permission preflight check cannot be asked there — it must warn with
        the reason rather than read a 404 as 'no restrictions found'."""
        assert endpoints_for(Venue.DEMO).supports_sapi is False
        assert endpoints_for(Venue.LIVE).supports_sapi is True

    def test_credential_names_are_disjoint_per_venue(self):
        live = set(credential_env_names(Venue.LIVE, "a")) | set(
            credential_env_names(Venue.LIVE, "b")
        )
        demo = set(credential_env_names(Venue.DEMO, "a"))
        assert live.isdisjoint(demo)
        assert demo == {"BINANCE_DEMO_KEY", "BINANCE_DEMO_SECRET"}
