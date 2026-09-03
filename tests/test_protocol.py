import pytest

from sub2api_auditer.normalize import parse_model_result
from sub2api_auditer.protocol import (
    ProtocolError,
    build_chat_completions_url,
    extract_audit_text,
    extract_upstream_content,
)


def test_build_chat_completions_url_variants():
    assert build_chat_completions_url("https://api.example.com") == "https://api.example.com/v1/chat/completions"
    assert build_chat_completions_url("https://api.example.com/v1") == "https://api.example.com/v1/chat/completions"
    assert build_chat_completions_url("https://api.example.com/openai/v2") == "https://api.example.com/openai/v2/chat/completions"
    assert build_chat_completions_url("https://api.example.com/v1/chat/completions") == "https://api.example.com/v1/chat/completions"


def test_extract_audit_text_prefers_user_messages_and_content_blocks():
    payload = {
        "messages": [
            {"role": "system", "content": "ignore"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "第一段"},
                    {"type": "input_text", "input_text": "第二段"},
                ],
            },
        ]
    }
    assert extract_audit_text(payload) == "第一段\n\n第二段"


def test_parse_native_qwen3guard_format():
    result = parse_model_result("Safety: Unsafe\nCategories: Jailbreak, PII")
    assert result.safety == "Unsafe"
    assert result.categories == ("Jailbreak", "PII")
    assert result.sub2api_content() == "Safety: Unsafe\nCategories: Jailbreak, PII"


def test_parse_fenced_json_and_chinese_categories():
    result = parse_model_result(
        """```json
        {"safety":"unsafe","categories":["越狱攻击","个人敏感信息"],"reason":"命中策略"}
        ```"""
    )
    assert result.safety == "Unsafe"
    assert result.categories == ("Jailbreak", "PII")
    assert result.reason == "命中策略"


def test_parse_flagged_style_response():
    result = parse_model_result('{"flagged": true, "confidence": 0.92, "category": "cyber abuse"}')
    assert result.safety == "Unsafe"
    assert result.categories == ("Non-violent Illegal Acts",)


def test_parse_nested_response():
    result = parse_model_result('{"result":{"decision":"review","labels":["copyright"]}}')
    assert result.safety == "Controversial"
    assert result.categories == ("Copyright Violation",)


def test_extract_upstream_content_from_openai_envelope():
    content = extract_upstream_content(
        {"choices": [{"message": {"content": [{"type": "text", "text": "hello"}]}}]}
    )
    assert content == "hello"


def test_unrecognized_model_output_is_rejected():
    with pytest.raises(ProtocolError):
        parse_model_result("模型没有给出任何判定")
