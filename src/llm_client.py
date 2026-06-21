"""统一视觉大模型客户端(OpenAI 兼容接口)。

所有业务模块只面对 ``VisionLLMClient``，换 provider(OpenAI / 通义千问 / 智谱GLM /
本地部署)无需改业务代码。

内置职责：
- 图像上送前按长边缩放控制 token；
- base64 编码；
- 失败重试(指数退避)；
- 优先请求 response_format=json_object，解析失败时兜底正则提取 JSON；
- 统计 token 用量。
"""
from __future__ import annotations

import base64
import io
import json
import logging
import re
import time
from dataclasses import dataclass, field

from PIL import Image

from .config_schema import LLMConfig

log = logging.getLogger("qr.llm")


@dataclass
class LLMResponse:
    text: str                       # 模型原始文本
    raw_json: dict | None = None    # 解析出的 JSON(若成功)
    finish_reason: str = ""
    usage: dict = field(default_factory=dict)
    attempts: int = 1

    @property
    def ok(self) -> bool:
        return self.raw_json is not None


class LLMError(Exception):
    """LLM 调用类错误(超时/限流/解析失败等)，由上层捕获并记入复核日志。"""


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", re.DOTALL)
_JSON_OBJ_RE = re.compile(r"(\{.*\}|\[.*\])", re.DOTALL)


def _extract_json(text: str) -> dict | None:
    """从可能含解释文字/markdown 围栏的回复中提取首个 JSON 对象。"""
    if not text:
        return None
    for pat in (_JSON_FENCE_RE, _JSON_OBJ_RE):
        m = pat.search(text)
        if m:
            try:
                obj = json.loads(m.group(1))
                if isinstance(obj, dict):
                    return obj
                if isinstance(obj, list):
                    return {"_list": obj}
            except json.JSONDecodeError:
                continue
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {"_list": obj}
    except json.JSONDecodeError:
        return None


class VisionLLMClient:
    def __init__(self, cfg: LLMConfig):
        try:
            from openai import OpenAI
        except ImportError as e:  # pragma: no cover
            raise LLMError("未安装 openai 库，请 pip install openai") from e
        self.cfg = cfg
        self._client = OpenAI(
            api_key=cfg.api_key,
            base_url=cfg.base_url,
            timeout=cfg.timeout,
            max_retries=0,  # 我们自己做重试
        )

    # ---------- 图像预处理 ----------
    def _prepare_image(self, image_bytes: bytes) -> str:
        """缩放到长边上限并返回 data URL(base64)。"""
        img = Image.open(io.BytesIO(image_bytes))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        long_edge = self.cfg.max_image_long_edge
        w, h = img.size
        scale = min(1.0, long_edge / max(w, h))
        if scale < 1.0:
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                             Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return f"data:image/png;base64,{b64}"

    # ---------- 单次调用 ----------
    def _chat_once(self, system: str, user: str, image_url: str,
                   json_mode: bool, temperature: float) -> tuple[str, str, dict]:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": [
                {"type": "text", "text": user},
                {"type": "image_url", "image_url": {"url": image_url}},
            ]},
        ]
        kwargs = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": temperature,
        }
        if json_mode and self.cfg.json_mode:
            # 部分服务不支持该字段，失败时上层回退后重试
            kwargs["response_format"] = {"type": "json_object"}
        resp = self._client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        text = choice.message.content or ""
        finish = choice.finish_reason or ""
        usage = {}
        if getattr(resp, "usage", None):
            usage = {
                "prompt_tokens": getattr(resp.usage, "prompt_tokens", 0),
                "completion_tokens": getattr(resp.usage, "completion_tokens", 0),
                "total_tokens": getattr(resp.usage, "total_tokens", 0),
            }
        return text, finish, usage

    # ---------- 对外主接口 ----------
    def ask_image(self, image_bytes: bytes, system: str, user: str,
                  json_mode: bool = True, temperature: float | None = None
                  ) -> LLMResponse:
        """发送一张图 + 提示词，返回 ``LLMResponse``。

        json_mode=True 时强制解析 JSON，解析失败会触发重试并最终抛 ``LLMError``。
        """
        temperature = self.cfg.temperature if temperature is None else temperature
        image_url = self._prepare_image(image_bytes)

        last_err: Exception | None = None
        tried_without_jsonfmt = False
        attempts = 0
        for attempt in range(1, self.cfg.max_retries + 1):
            attempts = attempt
            try:
                text, finish, usage = self._chat_once(
                    system, user, image_url, json_mode, temperature)
                parsed = _extract_json(text) if json_mode else None
                if json_mode and parsed is None:
                    # 内容回来了但不像 JSON：换更强约束(关闭 json_object、加围栏要求)重试
                    raise LLMError(f"回复无法解析为 JSON: {text[:160]}")
                return LLMResponse(text=text, raw_json=parsed,
                                   finish_reason=finish, usage=usage, attempts=attempts)
            except LLMError as e:
                last_err = e
                # 若是「服务不支持 response_format」导致的报错，回退重试
                if not tried_without_jsonfmt and json_mode and self.cfg.json_mode \
                        and _looks_like_unsupported_feature(e):
                    self.cfg.json_mode = False
                    tried_without_jsonfmt = True
                    log.warning("服务商疑似不支持 response_format，回退为普通模式重试。")
                    continue
                _backoff(attempt, self.cfg.max_retries)
            except Exception as e:  # 网络/超时/限流
                last_err = e
                msg = str(e)
                log.warning("LLM 调用失败(第%d次): %s", attempt, msg[:200])
                _backoff(attempt, self.cfg.max_retries)

        raise LLMError(f"重试 {attempts} 次仍失败: {last_err}")


def _looks_like_unsupported_feature(err: Exception) -> bool:
    s = str(err).lower()
    keys = ("response_format", "unsupported", "not support", "unrecognized",
            "unknown argument", "invalid_request")
    return any(k in s for k in keys)


def _backoff(attempt: int, max_retries: int) -> None:
    if attempt >= max_retries:
        return
    time.sleep(min(8.0, 0.8 * (2 ** (attempt - 1))))
