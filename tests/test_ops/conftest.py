from datetime import UTC, datetime

import pytest

from ops import db
from ops.config import load_config

NOW = datetime(2026, 9, 22, 8, 0, tzinfo=UTC)  # 12:00 Gulf


@pytest.fixture
def cfg():
    return load_config()


@pytest.fixture
def dbs(cfg, tmp_path):
    journal, knowledge = db.init_all(cfg, root=tmp_path)
    jdb = db.connect(journal)
    kdb = db.connect(knowledge)
    yield tmp_path, jdb, kdb
    jdb.close()
    kdb.close()
