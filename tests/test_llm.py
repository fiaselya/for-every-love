"""LLMClient / Extractor 的测试：JSON 解析后处理 + 降级兜底 + 围栏剥离。

覆盖验收点：
  1. FakeLLM（chat_json 返回固定 dict）注入 Extractor，验证
     extract / segment / judge 的解析与后处理（小写化、去标点、数量上限、去重）。
  2. FakeLLM 连续抛错时，extract 返回降级结果而不抛异常。
  3. LLMClient.chat_json 对 ```json 围栏能正确剥离（httpx MockTransport）。
"""

import json
from datetime import datetime, timezone

import httpx
import pytest

from event_memory.extractor import Extractor
from event_memory.llm import LLMClient, LLMError
from event_memory.models import Candidate, EventMeta


# ---------------------------------------------------------------------------
# 辅助：FakeLLM（chat_json 返回固定 dict）
# ---------------------------------------------------------------------------

class FakeLLM:
    """注入到 Extractor 的假客户端：chat_json 固定返回构造时给定的 dict。"""

    def __init__(self, result=None):
        self.result = result if result is not None else {}
        self.calls = []  # 记录 (system, user) 调用栈，便于校验数据已流入 prompt

    def chat_json(self, system: str, user: str) -> dict:
        self.calls.append((system, user))
        return self.result


class ExplodingFakeLLM(FakeLLM):
    """每次调用都抛错，用于验证 Extractor 的降级兜底。"""

    def chat_json(self, system: str, user: str) -> dict:
        self.calls.append((system, user))
        raise ValueError("simulated parse failure")


# ---------------------------------------------------------------------------
# 验收 1：extract 解析与后处理（小写化、去首尾标点、去重、数量上限）
# ---------------------------------------------------------------------------

def test_extract_hard_soft_summary_normalization():
    result = {
        "hard": ["Python", "AI!!!", "  test  ", "python", "docker", "k8s", "zsh"],
        "soft": [
            "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight",
            "Nine", "Ten",
        ],
        "summary": "Z" * 60,
    }
    ext = Extractor(FakeLLM(result=result))

    hard, soft, summary = ext.extract("无关文本，仅供后处理校验")

    # hard：小写化 + 去首尾标点 + 去重保序 + 上限 5。
    assert hard == ["python", "ai", "test", "docker", "k8s"]

    # soft：小写化 + 去重保序 + 上限 8。
    assert soft == ["one", "two", "three", "four", "five", "six", "seven", "eight"]
    assert len(soft) == 8

    # summary：截前 50 字。
    assert summary == "Z" * 50

    # 校验数据确实 flowed 进 prompt（占位符被替换）。
    assert ext._llm.calls  # 至少被调用一次
    assert "无关文本" in ext._llm.calls[-1][1]


# ---------------------------------------------------------------------------
# 验收 1：segment 解析（仅保留非空、可字符串化的原文）
# ---------------------------------------------------------------------------

def test_segment_filters_empty_and_non_string_texts():
    result = {
        "events": [
            {"text": "做了个鹈鹕骑车的动画", "status": "completed"},
            {"text": "   ", "status": "completed"},   # 空白 -> 跳过
            {"text": "", "status": "completed"},       # 空 -> 跳过
            {"text": 123, "status": "completed"},      # 非字符串 -> 跳过
            {"text": "写了个贪吃蛇游戏", "status": "completed"},
        ]
    }
    ext = Extractor(FakeLLM(result=result))

    history = [
        {"role": "user", "content": "帮我做个动画"},
        {"role": "assistant", "content": "好的，做了个鹈鹕骑车的动画"},
    ]
    texts = ext.segment(history)

    assert texts == ["做了个鹈鹕骑车的动画", "写了个贪吃蛇游戏"]
    # 历史文本应流入 prompt。
    assert "做了个鹈鹕骑车的动画" in ext._llm.calls[-1][1]


# ---------------------------------------------------------------------------
# 验收 1：judge 解析（去重、非空过滤、候选/元信息流入 prompt）
# ---------------------------------------------------------------------------

def test_judge_dedupe_and_filter_empty():
    result = {"relevant": ["evt2", "evt1", "evt2", "", "evt3"]}
    ext = Extractor(FakeLLM(result=result))

    candidates = [
        Candidate(event_id="evt1"),
        Candidate(event_id="evt2"),
        Candidate(event_id="evt3"),
    ]
    metas = {
        "evt1": EventMeta(event_id="evt1", created_at=1700000000, summary="s1"),
        "evt2": EventMeta(event_id="evt2", created_at=1700000001, summary="s2"),
        "evt3": EventMeta(event_id="evt3", created_at=1700000002, summary="s3"),
    }

    relevant = ext.judge("鹈鹕骑行", candidates, metas)

    # 去重保序、过滤空串。
    assert relevant == ["evt2", "evt1", "evt3"]

    # 候选与元信息（含日期格式化）应流入 prompt。
    _, user = ext._llm.calls[-1]
    assert "evt2" in user and "evt3" in user
    assert "s1" in user and "s3" in user
    exp_date = datetime.fromtimestamp(
        1700000000, tz=timezone.utc
    ).strftime("%Y-%m-%d %H:%M")
    assert exp_date in user


# ---------------------------------------------------------------------------
# 验收 2：FakeLLM 连续抛错时，extract 返回降级结果而不抛异常
# ---------------------------------------------------------------------------

def test_extract_fallback_when_llm_keeps_failing():
    text = "这是一个会触发多次失败的中文测试文本"
    ext = Extractor(ExplodingFakeLLM())

    hard, soft, summary = ext.extract(text)  # 不应抛异常

    assert hard == []
    assert soft == []
    assert summary == text[:50]

    # 验证重试 1 次（共 2 次调用）后仍降级。
    assert len(ext._llm.calls) == 2


def test_segment_and_judge_fallback_to_empty_on_llm_failure():
    ext = Extractor(ExplodingFakeLLM())

    assert ext.segment([{"role": "user", "content": "hi"}]) == []

    candidates = [Candidate(event_id="evt1")]
    metas = {"evt1": EventMeta(event_id="evt1", created_at=1, summary="s")}
    assert ext.judge("q", candidates, metas) == []


# ---------------------------------------------------------------------------
# 验收 2（llm=None 分支）：jieba 不可用时退回标准库兜底
# ---------------------------------------------------------------------------

def test_extract_llm_none_uses_fallback_tokenizer():
    text = "做了一个鹈鹕骑车的动画，鹈鹕戴着墨镜，第二天又写了个贪吃蛇游戏"
    ext = Extractor(None)

    hard, soft, summary = ext.extract(text)

    assert hard == []
    # jieba 未安装 -> 标准库按标点切词，去重保序，取前 10。
    assert soft == ["做了一个鹈鹕骑车的动画", "鹈鹕戴着墨镜", "第二天又写了个贪吃蛇游戏"]
    assert summary == text[:50]


# ---------------------------------------------------------------------------
# 验收 3：LLMClient.chat_json 对 ```json 围栏能正确剥离（httpx MockTransport）
# ---------------------------------------------------------------------------

def _json_content_response(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def test_chat_json_strips_json_fence():
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_content_response('```json\n{"hello": "世界", "n": 2}\n```\n')

    client = LLMClient("https://example.com/v1", "sk-test", "gpt-test")
    client._client = httpx.Client(transport=httpx.MockTransport(handler), timeout=60.0)

    result = client.chat_json("系统提示", "用户请求")

    assert result == {"hello": "世界", "n": 2}


def test_chat_json_strips_fence_without_language():
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_content_response("```\n{\"a\": 1}\n```")

    client = LLMClient("https://example.com/v1", "sk-test", "gpt-test")
    client._client = httpx.Client(transport=httpx.MockTransport(handler), timeout=60.0)

    assert client.chat_json("s", "u") == {"a": 1}


def test_endpoint_and_auth_header():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return _json_content_response("{}")

    client = LLMClient("https://api.example.com/v1", "secret-key", "model-x")
    client._client = httpx.Client(transport=httpx.MockTransport(handler), timeout=60.0)

    client.chat_json("s", "u")

    assert seen["url"] == "https://api.example.com/v1/chat/completions"
    assert seen["auth"] == "Bearer secret-key"


def test_chat_json_retries_without_response_format_on_server_error():
    payloads = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        if len(payloads) == 1:
            return httpx.Response(400, json={"error": "bad request"})
        return _json_content_response('{"ok": true}')

    client = LLMClient("https://api.example.com/v1/", "k", "m")
    client._client = httpx.Client(transport=httpx.MockTransport(handler), timeout=60.0)

    result = client.chat_json("s", "u")

    assert result == {"ok": True}
    assert len(payloads) == 2
    assert "response_format" in payloads[0]     # 首次带 response_format
    assert "response_format" not in payloads[1]  # 重试时去掉
    # 尾斜杠被归一化，拼成 /chat/completions。
    assert payloads[1]["messages"][-1]["content"]  # 用户消息仍在


def test_chat_json_raises_llmerror_on_persistent_failure():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500)

    client = LLMClient("https://api.example.com/v1", "k", "m")
    client._client = httpx.Client(transport=httpx.MockTransport(handler), timeout=60.0)

    with pytest.raises(LLMError):
        client.chat_json("s", "u")

    assert len(calls) == 2  # 重试 1 次后仍失败
