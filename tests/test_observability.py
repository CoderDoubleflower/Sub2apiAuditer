"""Tests for per-trickset logging and request-scoped observability."""

import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.observability import (
    get_logger,
    new_request_id,
    request_tag,
    reset_current_trickset,
    reset_request_id,
    set_current_trickset,
    set_request_id,
)
from src.proxy import ProxyHandler
from src.trick import Trick
from src.trickset import Trickset


def create_mock_response(data: dict) -> MagicMock:
    mock = MagicMock()
    mock.json.return_value = data
    mock.status_code = 200
    return mock


class LoggingTrick(Trick):
    def system_prompt(self, to_add: str) -> str:
        return "[logging system]"


class PromptLoggingTrick(Trick):
    prompt_keyword = "cmd"

    def handle_prompt_keyword(self, request: str, messages: list | None = None, payload: dict | None = None) -> dict | None:
        return {"role": "assistant", "content": "handled"}


class TestTricksetLogging:
    def test_default_logfile_and_loglevel(self):
        ts = Trickset("dflt_ts", "0.8.0", {"X-Title": "*", "Model": "*"}, [])
        assert ts.logfile == str(Path.home() / ".cache" / "petsitter" / "tricksets" / "dflt_ts.log")
        assert ts.loglevel == "INFO"

    def test_explicit_logfile_and_loglevel(self, tmp_path):
        ts = Trickset(
            "exp_ts", "0.8.0", {"X-Title": "*", "Model": "*"}, [],
            logfile=str(tmp_path / "custom.log"), loglevel="debug",
        )
        assert ts.logfile == str(tmp_path / "custom.log")
        assert ts.loglevel == "DEBUG"

    def test_config_round_trip(self, tmp_path):
        ts = Trickset(
            "rt_ts", "0.8.0", {"X-Title": "*", "Model": "*"}, [],
            logfile=str(tmp_path / "rt.log"), loglevel="DEBUG",
        )
        ts.file_path = str(tmp_path / "rt.json")
        ts.save()
        loaded = Trickset.load_from_file(str(tmp_path / "rt.json"))
        assert loaded.logfile == str(tmp_path / "rt.log")
        assert loaded.loglevel == "DEBUG"

    def test_get_logger_writes_to_logfile(self, tmp_path):
        ts = Trickset(
            "w_ts", "0.8.0", {"X-Title": "*", "Model": "*"}, [],
            logfile=str(tmp_path / "w.log"),
        )
        ts.get_logger().info("hello from %s", ts.name)
        content = (tmp_path / "w.log").read_text()
        assert "hello from w_ts" in content

    def test_loglevel_filters_file_output(self, tmp_path):
        ts = Trickset(
            "lv_ts", "0.8.0", {"X-Title": "*", "Model": "*"}, [],
            logfile=str(tmp_path / "lv.log"), loglevel="INFO",
        )
        log = ts.get_logger()
        log.debug("debug line")
        log.info("info line")
        content = (tmp_path / "lv.log").read_text()
        assert "info line" in content
        assert "debug line" not in content

        ts.loglevel = "DEBUG"
        ts.get_logger().debug("debug line 2")
        content = (tmp_path / "lv.log").read_text()
        assert "debug line 2" in content

    def test_retarget_logfile(self, tmp_path):
        ts = Trickset(
            "rt2_ts", "0.8.0", {"X-Title": "*", "Model": "*"}, [],
            logfile=str(tmp_path / "first.log"),
        )
        ts.get_logger().info("first file")
        ts.logfile = str(tmp_path / "second.log")
        ts.get_logger().info("second file")
        assert "first file" in (tmp_path / "first.log").read_text()
        assert "second file" in (tmp_path / "second.log").read_text()


class TestRequestScopedRouting:
    def test_request_id_tag(self):
        rid = new_request_id()
        token = set_request_id(rid)
        assert request_tag() == f"[{rid}] "
        reset_request_id(token)
        assert request_tag() == ""

    def test_get_logger_routing(self, tmp_path):
        base = get_logger()
        ts = Trickset(
            "route_ts", "0.8.0", {"X-Title": "*", "Model": "*"}, [],
            logfile=str(tmp_path / "route.log"),
        )
        rid = new_request_id()
        rid_token = set_request_id(rid)
        ts_token = set_current_trickset(ts)
        try:
            assert get_logger() is ts.get_logger()
            get_logger().info("%srouted message", request_tag())
        finally:
            reset_current_trickset(ts_token)
            reset_request_id(rid_token)
        assert request_tag() == ""
        assert get_logger() is base
        assert f"[{rid}] routed message" in (tmp_path / "route.log").read_text()


class TestProxyLogRouting:
    @pytest.mark.asyncio
    async def test_chat_completions_logs_to_trickset_file(self, tmp_path):
        ts = Trickset(
            "log_ts", "0.8.0", {"X-Title": "*", "Model": "*"}, [],
            logfile=str(tmp_path / "ts.log"),
        )
        ts.tricks = [LoggingTrick()]
        ts.trick_enabled = [True]
        handler = ProxyHandler("http://localhost:11434", "test", tricksets={"log_ts": ts})
        mock_response = create_mock_response({
            "choices": [{"message": {"role": "assistant", "content": "done"}}]
        })
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            payload = {"model": "some-model", "messages": [{"role": "user", "content": "hello"}]}
            await handler.chat_completions(payload)

        content = (tmp_path / "ts.log").read_text()
        assert re.search(r"\[[0-9a-f]{8}\]", content)
        assert "trickset 'log_ts' matched" in content
        assert "started LoggingTrick (run 0 -> 1)" in content
        assert "calling upstream model" in content

    def test_prompt_keyword_logs_to_owning_trickset(self, tmp_path):
        ts = Trickset(
            "pk_ts", "0.8.0", {"X-Title": "*", "Model": "*"}, [],
            logfile=str(tmp_path / "pk.log"),
        )
        ts.tricks = [PromptLoggingTrick()]
        ts.trick_enabled = [True]
        handler = ProxyHandler("http://localhost:11434", "test", tricksets={"pk_ts": ts})

        messages = [{"role": "user", "content": "hello (cmd: do thing)"}]
        modified, response = handler._filter_prompt_keywords(messages)
        assert response == {"role": "assistant", "content": "handled"}

        content = (tmp_path / "pk.log").read_text()
        assert "prompt keyword 'cmd' recognized -> PromptLoggingTrick" in content
        assert "handled by PromptLoggingTrick -> response injected" in content
