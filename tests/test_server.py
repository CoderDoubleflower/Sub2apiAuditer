"""Tests for petsitter server and CLI."""

import json

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.server import _resolve_trick_path, create_app, cli
from src.trick import Trick


def create_mock_response(data: dict) -> MagicMock:
    """Create a mock httpx Response."""
    mock = MagicMock()
    mock.json.return_value = data
    mock.status_code = 200
    return mock


class TestCreateApp:
    """Tests for create_app function."""

    def test_create_app_basic(self):
        """create_app creates a Starlette app."""
        app = create_app(
            model_url="http://localhost:11434",
            model_name="test-model",
            api_key="",
            trick_paths=[],
        )
        assert app is not None

    def test_create_app_with_tricks(self):
        """create_app loads tricks from paths."""
        app = create_app(
            model_url="http://localhost:11434",
            model_name="test-model",
            api_key="",
            trick_paths=["tricks/json_mode.py"],
        )
        assert app is not None


class TestCreateAppDefaults:
    """Tests for _default trickset seeding and persistence."""

    @staticmethod
    def _write_saved_default(tmp_path, tricks, logfile="~/.cache/petsitter/tricksets/_default.log"):
        (tmp_path / "_default.json").write_text(json.dumps({
            "schema": "0.8.0",
            "name": "_default",
            "filters": {"X-Title": "*", "Model": "*"},
            "tricks": tricks,
            "parameters": {},
            "models": {},
            "logfile": logfile,
            "loglevel": "INFO",
        }) + "\n")

    @staticmethod
    async def _trick_files(app, name="_default"):
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            return (await ac.get(f"/api/tricksets/{name}")).json()["tricks"]

    @pytest.mark.asyncio
    async def test_fresh_default_seeds_conversational_and_secrets(self, monkeypatch, tmp_path):
        """A brand-new _default starts with conversational_tool + secrets_protector."""
        monkeypatch.setattr("src.server.TRICKSETS_DIR", tmp_path)
        app = create_app(model_url="", model_name=None, api_key="", trick_paths=[])
        tricks = await self._trick_files(app)
        files = [t["file"] for t in tricks]
        assert files == ["tricks/conversational_tool.py", "tricks/secrets_protector.py"]
        assert all(t["enabled"] for t in tricks)

    @pytest.mark.asyncio
    async def test_restore_saved_default_preserves_edits(self, monkeypatch, tmp_path):
        """A saved _default.json is restored, not re-seeded."""
        monkeypatch.setattr("src.server.TRICKSETS_DIR", tmp_path)
        self._write_saved_default(tmp_path, [
            {"id": "abc123", "file": "tricks/json_mode.py", "enabled": False, "keyword": None},
        ])
        app = create_app(model_url="", model_name=None, api_key="", trick_paths=[], restore_saved=True)
        tricks = await self._trick_files(app)
        assert [t["file"] for t in tricks] == ["tricks/json_mode.py"]
        assert tricks[0]["enabled"] is False

    @pytest.mark.asyncio
    async def test_no_restore_without_flag(self, monkeypatch, tmp_path):
        """Without restore_saved, a saved file is ignored."""
        monkeypatch.setattr("src.server.TRICKSETS_DIR", tmp_path)
        self._write_saved_default(tmp_path, [
            {"id": "abc123", "file": "tricks/json_mode.py", "enabled": False, "keyword": None},
        ])
        app = create_app(model_url="", model_name=None, api_key="", trick_paths=[])
        tricks = await self._trick_files(app)
        assert tricks[0]["file"] == "tricks/conversational_tool.py"

    @pytest.mark.asyncio
    async def test_restore_plus_trick_paths_adds_missing_only(self, monkeypatch, tmp_path):
        """CLI -t tricks are added to a restored _default without duplicating."""
        monkeypatch.setattr("src.server.TRICKSETS_DIR", tmp_path)
        self._write_saved_default(tmp_path, [
            {"id": "abc123", "file": "tricks/conversational_tool.py", "enabled": True, "keyword": None},
        ])
        app = create_app(
            model_url="", model_name=None, api_key="",
            trick_paths=["tricks/conversational_tool.py", "tricks/json_mode.py"],
            restore_saved=True,
        )
        tricks = await self._trick_files(app)
        files = [t["file"] for t in tricks]
        assert files.count("tricks/conversational_tool.py") == 1
        assert files == ["tricks/conversational_tool.py", "tricks/json_mode.py"]

    @pytest.mark.asyncio
    async def test_missing_saved_file_seeds_defaults(self, monkeypatch, tmp_path):
        """No saved file + restore_saved falls back to the default seed."""
        monkeypatch.setattr("src.server.TRICKSETS_DIR", tmp_path)
        app = create_app(model_url="", model_name=None, api_key="", trick_paths=[], restore_saved=True)
        tricks = await self._trick_files(app)
        assert tricks[0]["file"] == "tricks/conversational_tool.py"


class TestServerEndpoints:
    """Tests for server endpoints."""

    @pytest.mark.asyncio
    async def test_health_endpoint(self):
        """Health endpoint returns OK."""
        from httpx import AsyncClient, ASGITransport

        app = create_app(
            model_url="http://localhost:11434",
            model_name="test-model",
            api_key="",
            trick_paths=[],
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            response = await ac.get("/health")
            assert response.status_code == 200
            assert response.json() == {"status": "ok"}

    @pytest.mark.asyncio
    async def test_chat_completions_endpoint(self):
        """Chat completions endpoint proxies requests."""
        from httpx import AsyncClient, ASGITransport

        app = create_app(
            model_url="http://localhost:11434",
            model_name="test-model",
            api_key="",
            trick_paths=[],
        )

        mock_response = create_mock_response({
            "choices": [{"message": {"role": "assistant", "content": "Hello!"}}]
        })

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                response = await ac.post(
                    "/v1/chat/completions",
                    json={"messages": [{"role": "user", "content": "Hi"}]},
                )
                assert response.status_code == 200
                assert response.json()["choices"][0]["message"]["content"] == "Hello!"

    @pytest.mark.asyncio
    async def test_chat_completions_error_handling(self):
        """Chat completions handles errors gracefully."""
        from httpx import AsyncClient, ASGITransport

        app = create_app(
            model_url="http://localhost:11434",
            model_name="test-model",
            api_key="",
            trick_paths=[],
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            # Invalid JSON should return error response
            response = await ac.post(
                "/v1/chat/completions",
                content="not valid json",
            )
            assert response.status_code == 400
            assert "error" in response.json()

    @pytest.mark.asyncio
    async def test_models_endpoint(self):
        """Models endpoint proxies requests."""
        from httpx import AsyncClient, ASGITransport

        app = create_app(
            model_url="http://localhost:11434",
            model_name="test-model",
            api_key="",
            trick_paths=[],
        )

        mock_response = create_mock_response({"data": [{"id": "test-model"}]})

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                response = await ac.get("/v1/models")
                assert response.status_code == 200
                assert "data" in response.json()


class TestResolveTrickPath:
    """Tests for _resolve_trick_path helper."""

    def test_resolves_short_name(self):
        """swapharness -> tricks/swapharness.py"""
        assert _resolve_trick_path("json_mode") == "tricks/json_mode.py"

    def test_preserves_full_path(self):
        """tricks/json_mode.py stays as-is"""
        assert _resolve_trick_path("tricks/json_mode.py") == "tricks/json_mode.py"

    def test_preserves_path_with_slash(self):
        """Tricks outside tricks/ dir stay as-is"""
        assert _resolve_trick_path("other/trick.py") == "other/trick.py"

    def test_non_existent_short_name_returned_as_is(self):
        """Unknown short name returned as-is when file doesn't exist"""
        assert _resolve_trick_path("nope") == "nope"


class TestLifecycleConvention:
    """Tests for the trickname:function CLI convention."""

    def test_lifecycle_install_runs_hook(self):
        """trickname:install runs the hook and prints confirmation."""
        from click.testing import CliRunner

        runner = CliRunner()

        with patch("src.server.uvicorn.run") as mock_run:
            with patch("src.server.create_app") as mock_create:
                mock_create.return_value = None
                result = runner.invoke(
                    cli,
                    [
                        "-u", "http://localhost:11434",
                        "-t", "tricks/json_mode.py:startup",
                    ],
                )
                assert "Ran startup() on tricks/json_mode.py" in result.output

    def test_regular_trick_still_loads(self):
        """Regular -t trick.py still loads into trickset."""
        from click.testing import CliRunner

        runner = CliRunner()

        with patch("src.server.uvicorn.run") as mock_run:
            with patch("src.server.create_app") as mock_create:
                mock_create.return_value = None
                result = runner.invoke(
                    cli,
                    [
                        "-u", "http://localhost:11434",
                        "-t", "tricks/json_mode.py",
                    ],
                )
                assert mock_create.called
                call_args = mock_create.call_args
                assert "tricks/json_mode.py" in call_args[1]["trick_paths"]

    def test_unknown_method_prints_error(self):
        """trickname:unknown prints error message."""
        from click.testing import CliRunner

        runner = CliRunner()

        with patch("src.server.uvicorn.run") as mock_run:
            with patch("src.server.create_app") as mock_create:
                mock_create.return_value = None
                result = runner.invoke(
                    cli,
                    [
                        "-u", "http://localhost:11434",
                        "-t", "json_mode:nonexistent",
                    ],
                )
                assert "has no method 'nonexistent'" in result.output


class TestCLI:
    """Tests for CLI."""

    def test_cli_parse_host_port(self):
        """CLI correctly parses host:port."""
        from click.testing import CliRunner

        runner = CliRunner()

        with patch("src.server.uvicorn.run") as mock_run:
            with patch("src.server.create_app") as mock_create:
                mock_create.return_value = None
                result = runner.invoke(
                    cli,
                    [
                        "-u", "http://localhost:11434",
                        "-l", "0.0.0.0:9000",
                    ],
                )
                assert mock_run.called
                call_args = mock_run.call_args
                assert call_args[1]["host"] == "0.0.0.0"
                assert call_args[1]["port"] == 9000

    def test_cli_default_port(self):
        """CLI uses default port 8080 if not specified."""
        from click.testing import CliRunner

        runner = CliRunner()

        with patch("src.server.uvicorn.run") as mock_run:
            with patch("src.server.create_app") as mock_create:
                mock_create.return_value = None
                result = runner.invoke(
                    cli,
                    [
                        "-u", "http://localhost:11434",
                        "-l", "localhost",
                    ],
                )
                assert mock_run.called
                call_args = mock_run.call_args
                assert call_args[1]["port"] == 8080

    def test_cli_with_tricks(self):
        """CLI accepts multiple -t options."""
        from click.testing import CliRunner

        runner = CliRunner()

        with patch("src.server.uvicorn.run") as mock_run:
            with patch("src.server.create_app") as mock_create:
                mock_create.return_value = None
                result = runner.invoke(
                    cli,
                    [
                        "-u", "http://localhost:11434",
                        "-t", "tricks/json_mode.py",
                        "-t", "tricks/tool_call.py",
                    ],
                )
                assert mock_create.called
                call_args = mock_create.call_args
                assert "tricks/json_mode.py" in call_args[1]["trick_paths"]
                assert "tricks/tool_call.py" in call_args[1]["trick_paths"]
