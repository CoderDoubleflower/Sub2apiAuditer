from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .protocol import NormalizedAuditResult, ProtocolError

LABELS = {
    "violent": "Violent",
    "illegal": "Non-violent Illegal Acts",
    "sexual": "Sexual Content or Sexual Acts",
    "pii": "PII",
    "self_harm": "Suicide & Self-Harm",
    "unethical": "Unethical Acts",
    "political": "Politically Sensitive Topics",
    "copyright": "Copyright Violation",
    "jailbreak": "Jailbreak",
}


def _words(value: str) -> str:
    value = value.strip().lower().replace("&", " and ")
    value = re.sub(r"[_/\\–—-]+", " ", value)
    value = re.sub(r"[^\w\u4e00-\u9fff]+", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


ALIASES: dict[str, str] = {}
for canonical, values in {
    "violent": ["violent", "violence", "violence graphic", "violent threat", "暴力", "暴力威胁"],
    "illegal": ["non violent illegal acts", "illegal acts", "illicit", "crime", "criminal", "cyber abuse", "malware", "fraud", "违法", "非暴力违法行为", "网络攻击", "网络滥用"],
    "sexual": ["sexual content or sexual acts", "sexual", "sexual minors", "adult content", "色情", "性内容", "性行为"],
    "pii": ["pii", "personal identifying information", "personal identifiable information", "privacy", "doxxing", "dox", "个人敏感信息", "隐私", "人肉搜索"],
    "self_harm": ["suicide and self harm", "suicide self harm", "self harm", "自杀", "自残", "自杀与自残"],
    "unethical": ["unethical acts", "unethical", "abuse", "不道德行为", "不道德"],
    "political": ["politically sensitive topics", "political", "politics", "政治敏感话题", "政治敏感"],
    "copyright": ["copyright violation", "copyright", "piracy", "版权侵权", "版权"],
    "jailbreak": ["jailbreak", "prompt injection", "prompt attack", "system prompt extraction", "越狱", "越狱攻击", "提示词注入"],
}.items():
    for value in values:
        ALIASES[_words(value)] = canonical

SAFETY = {
    "safe": "Safe", "allow": "Safe", "allowed": "Safe", "pass": "Safe", "passed": "Safe", "ok": "Safe",
    "合规": "Safe", "安全": "Safe", "放行": "Safe",
    "controversial": "Controversial", "flag": "Controversial", "flagged": "Controversial", "review": "Controversial",
    "warn": "Controversial", "warning": "Controversial", "uncertain": "Controversial", "争议": "Controversial", "可疑": "Controversial", "复核": "Controversial",
    "unsafe": "Unsafe", "block": "Unsafe", "blocked": "Unsafe", "deny": "Unsafe", "denied": "Unsafe",
    "critical": "Unsafe", "violation": "Unsafe", "违规": "Unsafe", "不安全": "Unsafe", "阻止": "Unsafe", "拦截": "Unsafe",
}


def _objects(text: str) -> Iterable[Mapping[str, Any]]:
    stripped = text.strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, re.I | re.S)
    if fence:
        stripped = fence.group(1).strip()
    decoder = json.JSONDecoder()
    seen: set[int] = set()
    for start in [0, *(m.start() for m in re.finditer(r"\{", stripped))]:
        if start in seen:
            continue
        seen.add(start)
        try:
            value, _ = decoder.raw_decode(stripped[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, Mapping):
            yield value


def _nested(value: Mapping[str, Any]) -> Mapping[str, Any]:
    known = {"safety", "status", "decision", "action", "label", "risk_level", "flagged", "blocked", "unsafe", "categories", "category", "labels", "violations"}
    if any(key in value for key in known):
        return value
    for key in ("result", "data", "output", "moderation"):
        child = value.get(key)
        if isinstance(child, Mapping):
            return _nested(child)
    return value


def _bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        if value.strip().lower() in {"true", "1", "yes", "y", "是"}:
            return True
        if value.strip().lower() in {"false", "0", "no", "n", "否"}:
            return False
    return None


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safety(value: Any) -> str | None:
    if value is None:
        return None
    normalized = _words(str(value))
    if normalized in SAFETY:
        return SAFETY[normalized]
    for token, result in SAFETY.items():
        if len(token) >= 4 and re.search(rf"\b{re.escape(token)}\b", normalized):
            return result
    return None


def _category(raw: str) -> str | None:
    raw = raw.strip()
    if not raw or raw.lower() in {"none", "n/a", "null", "[]", "无", "没有"}:
        return None
    normalized = _words(raw)
    canonical = ALIASES.get(normalized)
    if canonical:
        return LABELS[canonical]
    for alias, target in sorted(ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
        if len(alias) >= 4 and alias in normalized:
            return LABELS[target]
    cleaned = " ".join(re.sub(r"[\r\n,;|]+", " ", raw).split())[:80]
    return cleaned or None


def _categories(value: Any) -> tuple[str, ...]:
    raw: list[str] = []
    if value is None:
        return ()
    if isinstance(value, str):
        raw.extend(part.strip() for part in re.split(r"[,;|\n]", value))
    elif isinstance(value, Mapping):
        for key, enabled in value.items():
            include = _bool(enabled)
            if include is None:
                score = _float(enabled)
                include = score is not None and score > 0
            if include:
                raw.append(str(key))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            if isinstance(item, Mapping):
                name = item.get("name") or item.get("category") or item.get("label")
                if name:
                    raw.append(str(name))
            else:
                raw.append(str(item))
    else:
        raw.append(str(value))
    result: list[str] = []
    seen: set[str] = set()
    for item in raw:
        normalized = _category(item)
        if normalized and normalized.casefold() not in seen:
            seen.add(normalized.casefold())
            result.append(normalized)
        if len(result) >= 12:
            break
    return tuple(result)


def _json_result(value: Mapping[str, Any]) -> NormalizedAuditResult | None:
    value = _nested(value)
    safety = next((_safety(value.get(k)) for k in ("safety", "status", "decision", "action", "label", "risk_level", "risk") if _safety(value.get(k))), None)
    flagged = None
    for key in ("flagged", "blocked", "unsafe", "violation", "is_unsafe"):
        if key in value:
            flagged = _bool(value.get(key))
            if flagged is not None:
                break
    confidence = next((_float(value.get(k)) for k in ("confidence", "score", "risk_score", "probability") if _float(value.get(k)) is not None), None)
    if safety is None and flagged is not None:
        safety = "Safe" if not flagged else ("Controversial" if confidence is not None and confidence < 0.5 else "Unsafe")
    categories = ()
    for key in ("categories", "category", "labels", "violations", "risk_categories"):
        if key in value:
            categories = _categories(value.get(key))
            break
    if safety is None and categories:
        safety = "Unsafe"
    if safety is None:
        return None
    reason = str(value.get("reason") or value.get("message") or value.get("explanation") or "").strip()[:1000]
    return NormalizedAuditResult(safety, categories, reason)


def parse_model_result(text: str) -> NormalizedAuditResult:
    if not text or not text.strip():
        raise ProtocolError("审核模型返回为空")
    native = re.search(r"(?im)^\s*safety\s*:\s*(safe|controversial|unsafe)\s*$", text)
    if native:
        category_line = re.search(r"(?im)^\s*categories\s*:\s*(.*?)\s*$", text)
        return NormalizedAuditResult(native.group(1).capitalize(), _categories(category_line.group(1) if category_line else None))
    for value in _objects(text):
        result = _json_result(value)
        if result:
            return result
    lowered = _words(text)
    if re.search(r"\b(unsafe|blocked|critical|violation)\b", lowered) or any(x in text for x in ("违规", "不安全", "拦截", "阻止")):
        safety = "Unsafe"
    elif re.search(r"\b(controversial|flagged|review|warning|uncertain)\b", lowered) or any(x in text for x in ("争议", "可疑", "复核")):
        safety = "Controversial"
    elif re.search(r"\b(safe|allowed|pass|passed)\b", lowered) or any(x in text for x in ("合规", "放行")):
        safety = "Safe"
    else:
        raise ProtocolError("无法识别审核模型的判定结果")
    categories: list[str] = []
    for alias, canonical in sorted(ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
        if len(alias) >= 4 and alias in lowered and LABELS[canonical] not in categories:
            categories.append(LABELS[canonical])
    return NormalizedAuditResult(safety, tuple(categories))
