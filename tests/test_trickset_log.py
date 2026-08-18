"""Tests for the per-trickset log endpoint."""

import json

import pytest
from httpx import ASGITransport, AsyncClient

from petsitter.server import create_app


def _write_trickset(path, name, logfile, loglevel="INFO"):
    path.write_text(json.dumps({
        "schema": "0.8.0",
        "name": name,
        "filters": {"X-Title": "*", "Model": "*"},
        "tricks": [],
        "logfile": logfile,
        "loglevel": loglevel,
    }) + "\n")


@pytest.mark.asyncio
async def test_trickset_log_returns_tail(tmp_path):
    logfile = tmp_path / "ts.log"
    logfile.write_text("2026-08-01 10:00:00,000 INFO line one\n"
                       "2026-08-01 10:00:01,000 ERROR line two\n")
    ts_file = tmp_path / "trickset.json"
    _write_trickset(ts_file, "mytest", str(logfile))

    app = create_app(model_url="", model_name=None, api_key="", trick_paths=[], trickset_paths=[str(ts_file)])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get("/api/tricksets/mytest/log")
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "mytest"
        assert data["logfile"] == str(logfile)
        assert data["missing"] is False
        assert data["lines"][:2] == ["2026-08-01 10:00:00,000 INFO line one",
                                     "2026-08-01 10:00:01,000 ERROR line two"]


@pytest.mark.asyncio
async def test_trickset_log_respects_lines_limit(tmp_path):
    logfile = tmp_path / "ts.log"
    logfile.write_text("".join(f"2026-08-01 10:00:00,000 INFO line {i}\n" for i in range(10)))
    ts_file = tmp_path / "trickset.json"
    _write_trickset(ts_file, "mytest", str(logfile))

    app = create_app(model_url="", model_name=None, api_key="", trick_paths=[], trickset_paths=[str(ts_file)])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get("/api/tricksets/mytest/log?lines=3")
        assert response.status_code == 200
        data = response.json()
        assert len(data["lines"]) == 3
        assert any(line.endswith("line 9") for line in data["lines"])
        assert not any(line.endswith("line 0") for line in data["lines"])


@pytest.mark.asyncio
async def test_trickset_log_missing_file(tmp_path):
    logfile = tmp_path / "removed.log"
    ts_file = tmp_path / "trickset.json"
    _write_trickset(ts_file, "empty", str(logfile))

    app = create_app(model_url="", model_name=None, api_key="", trick_paths=[], trickset_paths=[str(ts_file)])
    logfile.unlink(missing_ok=True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get("/api/tricksets/empty/log")
        assert response.status_code == 200
        data = response.json()
        assert data["missing"] is True
        assert data["lines"] == []


@pytest.mark.asyncio
async def test_trickset_log_unknown_trickset(tmp_path):
    ts_file = tmp_path / "trickset.json"
    _write_trickset(ts_file, "mytest", str(tmp_path / "ts.log"))

    app = create_app(model_url="", model_name=None, api_key="", trick_paths=[], trickset_paths=[str(ts_file)])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get("/api/tricksets/nope/log")
        assert response.status_code == 404
