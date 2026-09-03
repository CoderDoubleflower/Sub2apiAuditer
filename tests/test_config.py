from sub2api_auditer.config import ConfigStore


async def test_config_update_preserves_api_key_when_blank(tmp_path):
    store = ConfigStore(tmp_path / "config.json")
    first = await store.update(
        {
            "base_url": "https://api.example.com/v1",
            "api_key": "sk-secret-12345678",
            "model": "audit-model",
            "prompt": "审核策略",
            "timeout_seconds": 10,
            "max_tokens": 128,
            "expected_version": 0,
        }
    )
    assert first.version == 1
    assert first.api_key == "sk-secret-12345678"

    second = await store.update(
        {
            "base_url": first.base_url,
            "api_key": "",
            "model": first.model,
            "prompt": first.prompt,
            "timeout_seconds": first.timeout_seconds,
            "max_tokens": first.max_tokens,
            "expected_version": 1,
        }
    )
    assert second.api_key == "sk-secret-12345678"
    assert second.public_dict()["api_key_masked"].startswith("sk-s")


async def test_config_update_can_clear_api_key(tmp_path):
    store = ConfigStore(tmp_path / "config.json")
    first = await store.update(
        {
            "base_url": "https://api.example.com",
            "api_key": "sk-secret",
            "model": "audit-model",
            "prompt": "审核策略",
        }
    )
    second = await store.update(
        {
            "expected_version": first.version,
            "clear_api_key": True,
        }
    )
    assert second.api_key == ""
