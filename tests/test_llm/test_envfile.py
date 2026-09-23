"""The `.env` editor: comments survive, writes are atomic, values never come back."""

from __future__ import annotations

import pytest

from ops.lib import envfile

SAMPLE = """\
# Earn secrets — copy to .env, chmod 600.

# Anthropic Console API key
ANTHROPIC_API_KEY=sk-ant-oldkey12345

# Telegram
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID="12345"
HOST_UID=1000
"""


@pytest.fixture
def env_path(tmp_path):
    p = tmp_path / ".env"
    p.write_text(SAMPLE)
    return p


def test_reading_reports_presence_and_last4_only(env_path):
    info = envfile.info("ANTHROPIC_API_KEY", path=env_path)
    assert info.present and info.last4 == "2345"
    assert "sk-ant" not in str(info.as_dict())
    assert not envfile.info("TELEGRAM_BOT_TOKEN", path=env_path).present


def test_quotes_and_carriage_returns_are_stripped_on_read(tmp_path):
    p = tmp_path / ".env"
    p.write_text('A="quoted"\r\nB=plain\r\n')
    assert envfile.parse(p.read_text()) == {"A": "quoted", "B": "plain"}


def test_setting_a_value_preserves_comments_and_order(env_path):
    envfile.set_value("ANTHROPIC_API_KEY", "sk-ant-newkey98765", path=env_path)
    text = env_path.read_text()
    assert "# Earn secrets" in text and "# Telegram" in text
    assert envfile.names(path=env_path) == [
        "ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "HOST_UID"]
    assert "sk-ant-oldkey12345" not in text


def test_a_new_key_is_appended(env_path):
    envfile.set_value("EARN_CLAUDE_AUTH_MODE", "auto", path=env_path)
    assert envfile.value_of("EARN_CLAUDE_AUTH_MODE", path=env_path) == "auto"
    assert envfile.names(path=env_path)[-1] == "EARN_CLAUDE_AUTH_MODE"


def test_round_trip_never_echoes_the_value(env_path):
    result = envfile.set_value("BINANCE_KEY_A", "abcdefghij1234567890", path=env_path)
    assert result.present and result.last4 == "7890"
    assert "abcdefghij" not in str(result.as_dict())
    listed = envfile.infos(["BINANCE_KEY_A", "MISSING"], path=env_path)
    assert [i.name for i in listed] == ["BINANCE_KEY_A", "MISSING"]
    assert listed[0].last4 == "7890" and listed[1].last4 is None
    assert "abcdefghij" not in str([i.as_dict() for i in listed])


def test_delete_removes_every_assignment(env_path):
    env_path.write_text(env_path.read_text() + "\nHOST_UID=2000\n")
    envfile.delete("HOST_UID", path=env_path)
    assert "HOST_UID" not in env_path.read_text()
    assert not envfile.info("HOST_UID", path=env_path).present


def test_a_duplicate_assignment_collapses_to_one(env_path):
    env_path.write_text("A=1\nA=2\n")
    envfile.set_value("A", "3", path=env_path)
    assert env_path.read_text().count("A=") == 1
    assert envfile.value_of("A", path=env_path) == "3"


def test_values_needing_quotes_get_them(tmp_path):
    p = tmp_path / ".env"
    envfile.set_value("PHRASE", "two words", path=p)
    assert 'PHRASE="two words"' in p.read_text()
    assert envfile.value_of("PHRASE", path=p) == "two words"


def test_writes_refuse_under_an_automated_run(env_path):
    with pytest.raises(envfile.EnvFileError, match="EARN_AUTOMATED_RUN"):
        envfile.set_value("ANTHROPIC_API_KEY", "x", path=env_path,
                          env={"EARN_AUTOMATED_RUN": "1"})
    with pytest.raises(envfile.EnvFileError, match="EARN_AUTOMATED_RUN"):
        envfile.delete("ANTHROPIC_API_KEY", path=env_path,
                       env={"EARN_AUTOMATED_RUN": "1"})
    assert envfile.info("ANTHROPIC_API_KEY", path=env_path).last4 == "2345"


def test_an_empty_value_is_a_delete_not_a_write(env_path):
    with pytest.raises(envfile.EnvFileError, match="empty"):
        envfile.set_value("ANTHROPIC_API_KEY", "   ", path=env_path)


def test_an_invalid_name_is_refused(env_path):
    with pytest.raises(envfile.EnvFileError, match="invalid secret name"):
        envfile.set_value("bad name", "x", path=env_path)


def test_a_missing_file_reads_as_nothing_present(tmp_path):
    missing = tmp_path / "absent" / ".env"
    assert envfile.names(path=missing) == []
    assert not envfile.info("ANYTHING", path=missing).present
    envfile.set_value("FIRST", "value", path=missing)
    assert missing.exists() and envfile.value_of("FIRST", path=missing) == "value"
