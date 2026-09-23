"""Shared fixtures for the provider layer: real databases, no network, no SDK."""

from __future__ import annotations

import pytest

from ops import db
from ops.config import load_config
from ops.models_config import ModelsConfig, load_models_cfg
from runs.llm.stub import StubProvider
from runs.llm.types import ProviderCaps


@pytest.fixture(scope="session")
def cfg():
    return load_config()


@pytest.fixture
def dbs(cfg, tmp_path):
    """A fresh journal + knowledge pair; the router writes into both."""
    journal, knowledge = db.init_all(cfg, root=tmp_path)
    jdb = db.connect(journal)
    kdb = db.connect(knowledge)
    yield jdb, kdb
    jdb.close()
    kdb.close()


@pytest.fixture
def jdb(dbs):
    return dbs[0]


@pytest.fixture
def kdb(dbs):
    return dbs[1]


@pytest.fixture
def mc() -> ModelsConfig:
    """The committed models.yaml, without the tier-1 overlay."""
    return load_models_cfg(overlay=None)


def tweak(base: ModelsConfig, **patch) -> ModelsConfig:
    """A copy of a ModelsConfig with a deep-merged patch — tests stay declarative."""
    data = base.model_dump()

    def merge(dst: dict, src: dict) -> dict:
        for key, value in src.items():
            if isinstance(value, dict) and isinstance(dst.get(key), dict):
                merge(dst[key], value)
            else:
                dst[key] = value
        return dst

    return ModelsConfig.model_validate(merge(data, patch))


def claude(key: str = "claude:subscription", **kwargs) -> StubProvider:
    """A stub with Claude's capabilities under a real provider key."""
    return StubProvider(key=key, caps=ProviderCaps(), **kwargs)


def local(**kwargs) -> StubProvider:
    """A stub with Ollama's capabilities: structured output only."""
    return StubProvider(
        key="ollama",
        caps=ProviderCaps(structured_output=True, tools_readonly=False,
                          tools_write=False, skills=False, max_ctx=8192),
        **kwargs,
    )
