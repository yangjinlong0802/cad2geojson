# -*- coding: utf-8 -*-
"""
大模型调用客户端（OpenAI 兼容接口，requests 直接实现）

使用 requests 直接调用 OpenAI Chat Completions API，避免 SDK 内置
连接管理导致的超时/重置问题。

支持任何兼容 OpenAI /v1/chat/completions 的服务，如：
    - apiyi.com 代理   https://api.apiyi.com/v1
    - OpenAI 官方      https://api.openai.com/v1

配置优先级：
    1. 构造函数直接传参
    2. 环境变量  LLM_API_KEY / LLM_BASE_URL / LLM_MODEL
    3. .env 文件（项目根目录）
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)   # 屏蔽 verify=False 警告

logger = logging.getLogger(__name__)

# 默认参数
_DEFAULT_BASE_URL    = "https://api.apiyi.com/v1"
_DEFAULT_MODEL       = "claude-3-7-sonnet-20250219"
_DEFAULT_MAX_TOKENS  = 8192
_DEFAULT_TEMPERATURE = 0.1
MAX_RETRIES          = 3
RETRY_DELAY          = 2.0    # 秒
REQUEST_TIMEOUT      = 300    # 每次请求最长等待秒数


def _load_env() -> None:
    """尝试加载项目根目录的 .env 文件。"""
    try:
        from dotenv import load_dotenv
        env_path = Path(__file__).parent.parent.parent / ".env"
        if env_path.exists():
            load_dotenv(env_path, override=False)
            logger.debug(f"已加载 .env: {env_path}")
    except ImportError:
        pass


# 模块加载时自动读取 .env
_load_env()


class LLMClient:
    """
    OpenAI 兼容接口调用封装（requests 实现）。

    使用示例：
        client = LLMClient()           # 从 .env / 环境变量自动读取
        svg = client.generate(messages)
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        temperature: float = _DEFAULT_TEMPERATURE,
    ):
        self.api_key     = api_key    or os.environ.get("LLM_API_KEY", "")
        self.base_url    = (base_url  or os.environ.get("LLM_BASE_URL", _DEFAULT_BASE_URL)).rstrip("/")
        self.model       = model      or os.environ.get("LLM_MODEL", _DEFAULT_MODEL)
        self.max_tokens  = max_tokens
        self.temperature = temperature

        if not self.api_key:
            raise RuntimeError(
                "未找到 API Key。\n"
                "请在 .env 文件中设置 LLM_API_KEY=sk-...，\n"
                "或通过环境变量 LLM_API_KEY 传入。"
            )

    @property
    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def generate(
        self,
        messages: List[Dict[str, str]],
    ) -> str:
        """
        调用 LLM，以流式接收响应并拼接为完整文本。
        使用流式是为了避免"大响应超 60s 导致中间网络断连"问题：
        流式模式下服务器会持续吐 token，连接不会空闲超时。

        参数:
            messages: [{"role": "system"/"user"/"assistant", "content": str}, ...]

        返回:
            模型生成的完整文本（去除首尾空白）
        """
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "stream": True,    # 流式，避免 60s 空闲超时断连
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        logger.debug(f"调用 LLM（流式）: model={self.model}, payload={len(body)/1024:.1f}KB")

        last_error = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                t0 = time.time()
                parts: List[str] = []
                prompt_tokens = 0
                completion_tokens = 0

                with requests.post(
                    f"{self.base_url}/chat/completions",
                    headers=self._headers,
                    data=body,
                    timeout=(30, REQUEST_TIMEOUT),  # (连接超时, 读超时)
                    stream=True,
                    verify=False,
                ) as resp:
                    if resp.status_code != 200:
                        err = resp.text[:300]
                        raise RuntimeError(f"HTTP {resp.status_code}: {err}")

                    # 逐行解析 SSE（Server-Sent Events）
                    for raw_line in resp.iter_lines():
                        if not raw_line:
                            continue
                        line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
                        if not line.startswith("data:"):
                            continue
                        data_str = line[5:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue

                        # 提取 delta 内容
                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        if "content" in delta and delta["content"]:
                            parts.append(delta["content"])

                        # 提取 usage（部分 API 在最后一块中携带）
                        if "usage" in chunk:
                            usage = chunk["usage"]
                            prompt_tokens = usage.get("prompt_tokens", 0)
                            completion_tokens = usage.get("completion_tokens", 0)

                elapsed = time.time() - t0
                result = "".join(parts).strip()
                logger.info(
                    f"LLM 响应完成: 耗时 {elapsed:.1f}s，"
                    f"输入 {prompt_tokens or '?'} tokens，"
                    f"输出 {completion_tokens or len(result)//4} tokens"
                )
                return result

            except Exception as e:
                last_error = e
                logger.warning(f"LLM 调用失败（第 {attempt} 次）: {e}")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY * attempt)

        raise RuntimeError(f"LLM 调用失败（已重试 {MAX_RETRIES} 次）: {last_error}")

    def stream_generate(
        self,
        messages: List[Dict[str, str]],
    ) -> Generator[str, None, None]:
        """
        流式调用，逐 token 产出文本（Server-Sent Events）。
        """
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "stream": True,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        logger.debug(f"流式调用 LLM: model={self.model}")

        with requests.post(
            f"{self.base_url}/chat/completions",
            headers=self._headers,
            data=body,
            timeout=REQUEST_TIMEOUT,
            stream=True,
            verify=False,
        ) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line:
                    continue
                text = line.decode("utf-8")
                if text.startswith("data: "):
                    text = text[6:]
                if text.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(text)
                    delta = chunk["choices"][0]["delta"]
                    if "content" in delta:
                        yield delta["content"]
                except (json.JSONDecodeError, KeyError):
                    pass
