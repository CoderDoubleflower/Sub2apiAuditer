"""Tests for per-trick key/value configuration."""

import json

import pytest

from petsitter.trickset import SCHEMA, Trickset
from petsitter.trick import Trick
from petsitter.loader import load_trick_from_path
from petsitter.tricks.mcp_tools import DEFAULT_MCP_PATH, McpToolsTrick


def _load_mcp(tmp_path, mcp_path: str = "") -> Trickset:
    """Build a trickset with the MCP tools trick loaded."""
    ts = Trickset(
        name="_default",
        schema=SCHEMA,
        filters={"X-Title": "*", "Model": "*"},
        trick_paths=["tricks/mcp_tools.py"],
        file_path=str(tmp_path / "_default.json"),
        logfile=str(tmp_path / "_default.log"),
    )
    if mcp_path:
        ts.trick_configs[ts.trick_ids[0]] = {"mcp_path": mcp_path}
    ts.load_tricks()
    return ts


class TestConfigFields:
    """Tricks declare their configurable fields."""

    def test_mcp_tools_declares_mcp_path_field(self):
        assert McpToolsTrick.config_fields
        field = McpToolsTrick.config_fields[0]
        assert field["key"] == "mcp_path"
        assert field["label"]
        assert field["description"]
        assert field["type"] == "path"
        assert field["default"] == str(DEFAULT_MCP_PATH)

    def test_base_trick_has_no_config_fields(self):
        assert Trick.config_fields == []

    def test_configure_sets_attributes(self):
        class Dummy(Trick):
            config_fields = [{"key": "alpha", "label": "Alpha"}]

        t = Dummy()
        t.configure({"alpha": "hello"})
        assert t.alpha == "hello"

    def test_available_introspection_includes_config_fields(self, tmp_path):
        from petsitter.gui_routes import _introspect_trick_file
        from pathlib import Path

        import petsitter
        packaged = Path(petsitter.__file__).parent / "tricks" / "mcp_tools.py"
        info = _introspect_trick_file(packaged)
        assert info["config_fields"]
        assert info["config_fields"][0]["key"] == "mcp_path"


class TestTricksetConfig:
    """Trickset stores and applies per-trick config."""

    def test_load_from_file_restores_config_and_applies(self, tmp_path):
        ts_file = tmp_path / "trickset.json"
        ts_file.write_text(json.dumps({
            "schema": SCHEMA,
            "name": "_default",
            "filters": {"X-Title": "*", "Model": "*"},
            "tricks": [
                {"id": "abc123", "file": "tricks/mcp_tools.py", "enabled": True,
                 "keyword": None, "config": {"mcp_path": "/tmp/nope.json"}},
            ],
            "parameters": {},
            "models": {},
            "logfile": str(tmp_path / "_default.log"),
            "loglevel": "INFO",
        }) + "\n")
        ts = Trickset.load_from_file(str(ts_file))
        assert ts.trick_configs["abc123"] == {"mcp_path": "/tmp/nope.json"}
        assert ts.tricks[0].mcp_path == __import__("pathlib").Path("/tmp/nope.json")

    def test_entries_include_config(self, tmp_path):
        ts = _load_mcp(tmp_path, mcp_path="/tmp/nope.json")
        entry = ts._trick_entries()[0]
        assert entry["config"] == {"mcp_path": "/tmp/nope.json"}

    def test_merge_tricks_applies_config_to_instance(self, tmp_path):
        ts = _load_mcp(tmp_path)
        tid = ts.trick_ids[0]
        changed = ts.merge_tricks([{"id": tid, "config": {"mcp_path": "/tmp/merge.json"}}])
        assert changed
        assert ts.trick_configs[tid] == {"mcp_path": "/tmp/merge.json"}
        assert ts.tricks[0].mcp_path == __import__("pathlib").Path("/tmp/merge.json")

    def test_remove_trick_drops_config(self, tmp_path):
        ts = _load_mcp(tmp_path, mcp_path="/tmp/nope.json")
        tid = ts.trick_ids[0]
        ts.remove_trick(tid)
        assert tid not in ts.trick_configs

    def test_merge_tricks_ignores_unknown_id_config(self, tmp_path):
        ts = _load_mcp(tmp_path)
        assert not ts.merge_tricks([{"id": "zzz", "config": {"mcp_path": "/x"}}])


class TestKeywordSerialization:
    """Trickset entries carry the effective keyword, not a bare override."""

    @staticmethod
    def _ts(tmp_path, path):
        ts = Trickset(
            name="_default",
            schema=SCHEMA,
            filters={"X-Title": "*", "Model": "*"},
            trick_paths=[path],
            file_path=str(tmp_path / "_default.json"),
            logfile=str(tmp_path / "_default.log"),
        )
        ts.load_tricks()
        return ts

    def test_default_keyword_is_serialized_not_null(self, tmp_path):
        ts = self._ts(tmp_path, "tricks/swapharness.py")
        assert ts._trick_entries()[0]["keyword"] == "swapharness"

    def test_no_keyword_field_when_trick_has_none(self, tmp_path):
        ts = self._ts(tmp_path, "tricks/conversational_tool.py")
        assert "keyword" not in ts._trick_entries()[0]

    def test_override_beats_default(self, tmp_path):
        ts = self._ts(tmp_path, "tricks/swapharness.py")
        ts.trick_keywords[0] = "custom"
        assert ts._trick_entries()[0]["keyword"] == "custom"

    def test_keyword_survives_save_and_reload(self, tmp_path):
        ts = self._ts(tmp_path, "tricks/swapharness.py")
        ts.save()
        loaded = Trickset.load_from_file(str(tmp_path / "_default.json"))
        assert loaded._trick_entries()[0]["keyword"] == "swapharness"


@pytest.mark.asyncio
class TestConfigApi:
    """The dashboard API exposes and persists per-trick config."""

    @staticmethod
    def _client(app):
        from httpx import ASGITransport, AsyncClient

        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    @pytest.fixture(autouse=True)
    def _isolate(self, monkeypatch, tmp_path):
        monkeypatch.setattr("petsitter.server.TRICKSETS_DIR", tmp_path)

    async def test_tricks_info_exposes_config(self, monkeypatch, tmp_path):
        from petsitter.server import create_app

        app = create_app(model_url="", model_name=None, api_key="", trick_paths=["tricks/mcp_tools.py"])
        async with self._client(app) as ac:
            info = (await ac.get("/api/tricks")).json()
        mcp = next(t for t in info if t["name"] == "McpToolsTrick")
        assert mcp["config_fields"][0]["key"] == "mcp_path"
        assert mcp["config"] == {}

    async def test_put_config_persists_and_is_returned(self, monkeypatch, tmp_path):
        from petsitter.server import create_app

        app = create_app(model_url="", model_name=None, api_key="", trick_paths=["tricks/mcp_tools.py"])
        async with self._client(app) as ac:
            info = (await ac.get("/api/tricks")).json()
            mcp = next(t for t in info if t["name"] == "McpToolsTrick")
            tid = mcp["id"]
            r = await ac.put(
                "/api/tricksets/_default",
                json={"tricks": [{"id": tid, "config": {"mcp_path": "/tmp/saved.json"}}]},
            )
            assert r.status_code == 200
            assert r.json()["success"]
            info = (await ac.get("/api/tricks")).json()
            mcp = next(t for t in info if t["name"] == "McpToolsTrick")
            assert mcp["config"] == {"mcp_path": "/tmp/saved.json"}
            # persisted to the trickset file
            saved = json.loads((tmp_path / "_default.json").read_text())
            entry = next(t for t in saved["tricks"] if t["id"] == tid)
            assert entry["config"] == {"mcp_path": "/tmp/saved.json"}

    async def test_put_config_survives_reload(self, monkeypatch, tmp_path):
        from petsitter.server import create_app

        app = create_app(model_url="", model_name=None, api_key="", trick_paths=["tricks/mcp_tools.py"], restore_saved=True)
        async with self._client(app) as ac:
            info = (await ac.get("/api/tricks")).json()
            mcp = next(t for t in info if t["name"] == "McpToolsTrick")
            await ac.put("/api/tricksets/_default", json={"tricks": [{"id": mcp["id"], "config": {"mcp_path": "/tmp/keep.json"}}]})
        # fresh app restores from disk, config survives
        app2 = create_app(model_url="", model_name=None, api_key="", trick_paths=["tricks/mcp_tools.py"], restore_saved=True)
        async with self._client(app2) as ac:
            info = (await ac.get("/api/tricks")).json()
            mcp = next(t for t in info if t["name"] == "McpToolsTrick")
            assert mcp["config"] == {"mcp_path": "/tmp/keep.json"}
