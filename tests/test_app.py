from __future__ import annotations

import itertools

import httpx

from sub2api_auditer.app import create_app


async def _configured_app(tmp_path, handler, *, admin_token="", auditer_token=""):
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = create_app(
        config_path=str(tmp_path / "config.json"),
        client=upstream,
        admin_token=admin_token,
        auditer_token=auditer_token,
    )
    return app, upstream


async def _configure(app):
    await app.state.store.update(
        {
            "base_url": "https://gateway.example.com/v1",
            "api_key": "sk-upstream",
            "model": "audit-model",
            "prompt": "自定义审核策略",
        }
    )


async def test_chat_completions_returns_sub2api_format(tmp_path):
    observed = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["url"] = str(request.url)
        observed["authorization"] = request.headers.get("authorization")
        observed["payload"] = request.read().decode()
        return httpx.Response(
            200,
            headers={"x-request-id": "upstream-123"},
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"safety":"Unsafe","categories":["Jailbreak"]}'
                        }
                    }
                ]
            },
        )

    app, upstream = await _configured_app(tmp_path, handler)
    async with app.router.lifespan_context(app):
        await _configure(app)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/chat/completions",
                headers={"X-Request-ID": "sub2api-123"},
                json={"model": "sub2api-model", "messages": [{"role": "user", "content": "test"}]},
            )

    await upstream.aclose()
    assert response.status_code == 200
    body = response.json()
    assert body["choices"][0]["message"]["content"] == "Safety: Unsafe\nCategories: Jailbreak"
    assert observed["url"] == "https://gateway.example.com/v1/chat/completions"
    assert observed["authorization"] == "Bearer sk-upstream"
    assert '"model":"audit-model"' in observed["payload"].replace(" ", "")
    assert "自定义审核策略" in observed["payload"]
    assert response.headers["x-auditer-trace-id"].startswith("aud-")


async def test_models_endpoint_supports_sub2api_probe(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    app, upstream = await _configured_app(tmp_path, handler, auditer_token="secret")
    async with app.router.lifespan_context(app):
        await _configure(app)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            unauthorized = await client.get("/v1/models")
            authorized = await client.get(
                "/v1/models", headers={"Authorization": "Bearer secret"}
            )

    await upstream.aclose()
    assert unauthorized.status_code == 401
    assert authorized.status_code == 200
    assert {item["id"] for item in authorized.json()["data"]} == {
        "audit-model",
        "sub2api-auditer",
    }


async def test_invalid_upstream_model_output_returns_502(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "no verdict"}}]})

    app, upstream = await _configured_app(tmp_path, handler)
    async with app.router.lifespan_context(app):
        await _configure(app)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "test"}]},
            )

    await upstream.aclose()
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "audit_model_invalid_response"


async def test_processing_log_captures_four_timestamps_and_phases(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"x-request-id": "upstream-timing"},
            json={"choices": [{"message": {"content": '{"safety":"Safe","categories":[]}'}}]},
        )

    app, upstream = await _configured_app(tmp_path, handler)
    async with app.router.lifespan_context(app):
        await _configure(app)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/chat/completions",
                headers={"X-Request-ID": "sub2api-timing"},
                json={"model": "sub2api-auditer", "messages": [{"role": "user", "content": "hello"}]},
            )
            logs_response = await client.get("/api/logs")

    await upstream.aclose()
    assert response.status_code == 200
    assert logs_response.status_code == 200
    logs = logs_response.json()["items"]
    assert len(logs) == 1
    trace = logs[0]
    assert trace["source"] == "sub2api"
    assert trace["client_request_id"] == "sub2api-timing"
    assert trace["upstream_request_id"] == "upstream-timing"
    assert trace["status"] == "success"
    assert trace["safety"] == "Safe"
    assert trace["received_at"]
    assert trace["forwarded_at"]
    assert trace["llm_replied_at"]
    assert trace["sub2api_replied_at"]
    assert trace["preprocess_ms"] is not None and trace["preprocess_ms"] >= 0
    assert trace["upstream_ms"] is not None and trace["upstream_ms"] >= 0
    assert trace["response_ms"] is not None and trace["response_ms"] >= 0
    assert trace["total_ms"] is not None and trace["total_ms"] >= 0
    assert trace["input_chars"] == 5
    assert trace["upstream_response_bytes"] > 0


async def test_statistics_are_derived_from_current_log_window_and_clearable(tmp_path):
    counter = itertools.count()

    def handler(request: httpx.Request) -> httpx.Response:
        if next(counter) == 0:
            content = '{"safety":"Controversial","categories":["Copyright Violation"]}'
        else:
            content = "unparseable output"
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    app, upstream = await _configured_app(tmp_path, handler)
    async with app.router.lifespan_context(app):
        await _configure(app)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.post(
                "/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "first"}]},
            )
            second = await client.post(
                "/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "second"}]},
            )
            statistics = await client.get("/api/statistics")
            cleared = await client.delete("/api/logs")
            after_clear = await client.get("/api/statistics")

    await upstream.aclose()
    assert first.status_code == 200
    assert second.status_code == 502
    data = statistics.json()
    assert data["window_size"] == 2
    assert data["completed"] == 2
    assert data["success"] == 1
    assert data["failed"] == 1
    assert data["success_rate"] == 50.0
    assert data["decisions"]["Controversial"] == 1
    assert data["decisions"]["Unclassified"] == 1
    assert data["errors"] == [{"code": "audit_model_invalid_response", "count": 1}]
    assert len(data["series"]) == 2
    assert cleared.json() == {"ok": True, "cleared": 2}
    assert after_clear.json()["window_size"] == 0


async def test_logs_and_statistics_require_admin_token(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "Safety: Safe\nCategories: None"}}]})

    app, upstream = await _configured_app(tmp_path, handler, admin_token="admin-secret")
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            logs = await client.get("/api/logs")
            stats = await client.get("/api/statistics")
            authorized = await client.get(
                "/api/logs", headers={"Authorization": "Bearer admin-secret"}
            )

    await upstream.aclose()
    assert logs.status_code == 401
    assert stats.status_code == 401
    assert authorized.status_code == 200
