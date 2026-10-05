import json
import re
from typing import Literal
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field, field_validator


ReasoningEffort = Literal["default", "none", "minimal", "low", "medium", "high", "xhigh", "max"]


class APIConfig(BaseModel):
    base_url: str = Field(default="https://api.openai.com/v1", max_length=2048)
    api_key: str = Field(min_length=1, max_length=4096, repr=False)
    model: str = Field(min_length=1, max_length=200)
    reasoning_effort: ReasoningEffort = "default"

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value):
        value = value.strip().rstrip("/")
        parsed = urlparse(value)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("请输入有效的 API Base URL。")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("远程 API 地址必须使用 HTTPS；本地模型可使用 HTTP。")
        return value

    @field_validator("api_key", "model")
    @classmethod
    def trim_value(cls, value):
        if not value.strip():
            raise ValueError("API Key 和模型名称不能为空。")
        return value.strip()

class ProviderError(Exception):
    pass


def provider_error(status):
    return {401: "API Key 无效，请检查设置。", 403: "API 拒绝访问，请检查 Key 权限或服务地区。", 404: "模型或 API 地址不存在，请检查 Base URL 和模型名称。", 429: "API 配额不足或请求过于频繁，请稍后再试。"}.get(status, f"API 服务返回错误（HTTP {status}），请稍后再试或检查设置。")


async def completion(config, messages):
    url = config.base_url + "/chat/completions"
    payload = {"model": config.model, "messages": messages, "stream": True}
    if config.reasoning_effort != "default":
        payload["reasoning_effort"] = config.reasoning_effort
    # Disable redirects so an upstream cannot redirect the user's credential.
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=20), follow_redirects=False) as client:
            async with client.stream("POST", url, headers={"Authorization": f"Bearer {config.api_key}"}, json=payload) as response:
                if response.status_code != 200:
                    if config.reasoning_effort != "default" and response.status_code in {400, 422}:
                        raise ProviderError("服务未接受当前请求。请确认模型支持所选思考强度，或在 API 设置中改为“服务默认”。")
                    raise ProviderError(provider_error(response.status_code))
                got_text = False
                finished = False
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if raw == "[DONE]":
                        finished = True
                        break
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if event.get("error"):
                        raise ProviderError("模型服务中断了生成，请重试。")
                    for choice in event.get("choices", []):
                        if choice.get("finish_reason") == "length":
                            raise ProviderError("模型输出达到长度上限；请更换模型后重试。")
                        if choice.get("finish_reason") == "content_filter":
                            raise ProviderError("模型服务未能处理这段论文内容。")
                        if choice.get("finish_reason") in {"stop", "tool_calls"}:
                            finished = True
                        content = choice.get("delta", {}).get("content")
                        if isinstance(content, str) and content:
                            got_text = True
                            yield content
                if not got_text:
                    raise ProviderError("模型没有返回文字，请检查模型是否支持 Chat Completions。")
                if not finished:
                    raise ProviderError("API 连接提前结束，内容未完整生成，请重试。")
    except httpx.TimeoutException:
        raise ProviderError("API 请求超时，请检查网络或稍后重试。") from None
    except httpx.RequestError:
        raise ProviderError("无法连接 API 服务，请检查网络和 Base URL。") from None


async def english_query(config, question):
    if not re.search(r"[\u4e00-\u9fff]", question):
        return question
    parts = []
    async for text in completion(config, [
        {"role": "system", "content": "Convert the research question into 5-15 English search keywords for retrieving passages of an academic paper. Only output keywords, no explanation. Do not answer the question."},
        {"role": "user", "content": question},
    ]):
        parts.append(text)
    return question + " " + "".join(parts)[:1500]


def sse(event, **data):
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
