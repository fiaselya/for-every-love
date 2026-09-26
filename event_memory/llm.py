"""LLMClient：OpenAI 兼容接口的 JSON 调用封装。

- POST {base_url}/chat/completions，header "Authorization: Bearer {api_key}"，
  body {"model", "messages", "temperature": 0}。
- 请求 JSON 输出（response_format），服务端报错或解析失败各重试 1 次，
  再失败抛 LLMError。内容可能带 ```json 围栏，先剥离再解析。

仅使用标准库 + httpx。
"""

from __future__ import annotations

import json
from pathlib import Path

try:
    import httpx
except ImportError:  # httpx 未安装：llm 相关能力降级为不可用，记忆本体照常工作
    httpx = None  # type: ignore[assignment]

LLM_TIMEOUT_SECONDS = 60.0

# 默认配置文件：项目根目录下的 llm_config.json
# {"base_url": "...", "api_key": "...", "model": "..."}
DEFAULT_LLM_CONFIG_PATH = Path(__file__).resolve().parent.parent / "llm_config.json"


class LLMError(Exception):
    """LLM 调用失败（网络/服务端/解析均一视）时抛出。"""


def load_llm_client(config_path: Path | None = None) -> LLMClient | None:
    """从 JSON 配置文件加载 LLMClient；文件缺失或字段不全返回 None。

    配置格式：
    {"base_url": "http://127.0.0.1:8787/v1", "api_key": "local", "model": "deepseek-v4.1-flash"}
    """
    path = Path(config_path) if config_path else DEFAULT_LLM_CONFIG_PATH
    if httpx is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    base_url = data.get("base_url")
    api_key = data.get("api_key")
    model = data.get("model")
    if not (base_url and api_key and model):
        return None
    return LLMClient(base_url=base_url, api_key=api_key, model=model)


class LLMClient:
    """OpenAI 兼容接口客户端，专用于取得结构化 JSON。"""

    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        if httpx is None:
            raise LLMError("httpx 未安装，无法创建 LLMClient（pip install httpx）")
        self._base_url = base_url
        self._api_key = api_key
        self._model = model
        self._client = httpx.Client(timeout=LLM_TIMEOUT_SECONDS)

    # ---- 内部：URL / 认证 ----

    def _endpoint(self) -> str:
        """统一拼成 {base_url}/chat/completions。"""
        base = self._base_url
        if base.endswith("/"):
            base = base[:-1]
        return f"{base}/chat/completions"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    # ---- 主入口 ----

    def chat_json(self, system: str, user: str) -> dict:
        """按 system/user 发起一次"要求 JSON"的对话，返回解析后的 dict。

        重试策略：
        1. 首次用 response_format；解析失败或服务端报错时，去掉该字段重试 1 次，
           仅靠 prompt 约束 JSON。
        2. 仍失败则抛 LLMError（宁可不命中，不要错命中）。
        """
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

        # 第一次尝试带 response_format；失败后置 None 重试。
        response_format: dict | None = {"type": "json_object"}
        last_exc: Exception | None = None
        for _ in range(2):
            payload = {
                "model": self._model,
                "messages": messages,
                "temperature": 0,
                # 显式声明非流式：WorkBuddy 中继缺省该字段时按 SSE 回流，
                # 标准服务端则忽略此字段。
                "stream": False,
            }
            if response_format is not None:
                payload["response_format"] = response_format

            try:
                content = self._call(payload)
                return self._parse(content)
            except LLMError as exc:
                last_exc = exc
                response_format = None  # 重试时去掉，仅靠 prompt 约束
                continue

        assert last_exc is not None
        raise last_exc

    # ---- 内部：单次 HTTP 调用 ----

    def _call(self, payload: dict) -> str:
        """发起一次 POST，返回 choices[0].message.content；失败抛 LLMError。"""
        try:
            resp = self._client.post(
                self._endpoint(),
                headers=self._headers(),
                json=payload,
            )
        except httpx.HTTPError as exc:  # 网络/超时等
            raise LLMError(f"LLM 请求失败：{exc}") from exc

        if resp.status_code >= 400:
            raise LLMError(f"LLM 服务端错误：HTTP {resp.status_code} {resp.text}")

        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise LLMError(f"LLM 响应格式异常：{exc}") from exc

        if not isinstance(content, str):
            raise LLMError("LLM 响应 content 非字符串")
        return content

    # ---- 内部：JSON 解析 ----

    @staticmethod
    def _parse(content: str) -> dict:
        """剥离 ```json 围栏后 json.loads；失败抛 LLMError。"""
        text = LLMClient._strip_fence(content)
        try:
            obj = json.loads(text)
        except (ValueError, json.JSONDecodeError) as exc:
            raise LLMError(f"LLM JSON 解析失败：{exc}") from exc
        if not isinstance(obj, dict):
            raise LLMError(f"LLM JSON 顶层非对象：{type(obj).__name__}")
        return obj

    @staticmethod
    def _strip_fence(content: str) -> str:
        """剥掉 ```json ... ``` 围栏，只保留内部 JSON 文本。"""
        s = content.strip()
        if s.startswith("```"):
            lines = s.split("\n")
            # 第一行是 ``` 或 ```json，末行是闭合的 ```
            if len(lines) >= 2 and lines[-1].strip() == "```":
                return "\n".join(lines[1:-1]).strip()
            # 仅有开围栏、无闭合：丢弃围栏行
            return "\n".join(lines[1:]).strip()
        return s
