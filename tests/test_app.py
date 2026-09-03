import httpx

from sub2api_auditer.app import create_app


async def _configured_app(tmp_path, handler, *, auditer_token=""):
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = create_app(
        config_path=str(tmp_path / "config.json"),
        client=upstream,
        admin_token="",
        auditer_token=auditer_token,
    )
    return app, upstream


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
        await app.state.store.update(
            {
                "base_url": "https://gateway.example.com/v1",
                "api_key": "sk-upstream",
                "model": "audit-model",
                "prompt": "自定义审核策略",
            }
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/chat/completions",
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


async def test_models_endpoint_supports_sub2api_probe(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    app, upstream = await _configured_app(tmp_path, handler, auditer_token="secret")
    async with app.router.lifespan_context(app):
        await app.state.store.update(
            {
                "base_url": "https://gateway.example.com",
                "model": "audit-model",
                "prompt": "审核策略",
            }
        )
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
        await app.state.store.update(
            {
                "base_url": "https://gateway.example.com",
                "model": "audit-model",
                "prompt": "审核策略",
            }
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "test"}]},
            )

    await upstream.aclose()
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "audit_model_invalid_response"
