"""Tushare Pro REST 客户端 — 直接 POST api.tushare.pro, 不依赖 tushare SDK。

为什么不用官方 SDK:
  SDK 是同一 HTTP 端点的薄封装(pandas 中转 + 15MB 级依赖), 而本插件只需要一次
  JSON POST。项目已自带 httpx, 且插件必须能在 Docker / 打包桌面版中工作,
  故按 HTTP 契约自行实现 (runtime: none, 与 fuyao 同模式)。

协议:
  请求 POST {"api_name": "daily", "token": "...", "params": {...}, "fields": "a,b,c"}
  响应 {"code": 0, "msg": null, "data": {"fields": [...], "items": [[...], ...]}}
  code != 0 表示业务错误(积分不足 / 无接口权限 / 频率超限 / 参数非法), 抛 TushareError。

Key 只用于请求体, 不写日志、不进异常文案。
"""

from __future__ import annotations

import logging
import time
from collections import deque

import httpx

logger = logging.getLogger(__name__)

# 官方端点: 文档与 SDK 均走 http (HTTPS 侧证书链不完整, 部分环境握手失败)。
API_URL = "http://api.tushare.pro"
DEFAULT_TIMEOUT = 20.0

# 频率自限速: Tushare 按积分等级限制「每分钟调用次数」(常见 500 次/分钟档)。
# 取 400 作为保守默认, 避免用户档位较低时整批失败; 全市场日K同步约 250 次请求,
# 即使被限到 400/分钟也只需 ~40 秒。
CALLS_PER_MINUTE = 400
_RATE_WINDOW_S = 60.0

# 网络抖动重试(仅传输层/非 JSON 响应; 业务错误不重试, 重试也不会变好)
_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_S = 0.6


class TushareError(Exception):
    """Tushare 业务错误或不可恢复的传输错误。"""


class TushareClient:
    """Tushare Pro HTTP 客户端。线程安全由调用方保证(provider 侧为串行同步流程)。"""

    def __init__(
        self,
        token: str = "",
        timeout: float = DEFAULT_TIMEOUT,
        calls_per_minute: int = CALLS_PER_MINUTE,
        client: httpx.Client | None = None,
    ) -> None:
        self._token = (token or "").strip()
        self._calls_per_minute = max(1, int(calls_per_minute))
        self._calls: deque[float] = deque()
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=timeout,
            headers={"User-Agent": "tick-stock-panel-tushare-plugin"},
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _throttle(self) -> None:
        """滑动窗口限速: 窗口内调用数达上限时睡到最早一次调用滑出窗口。"""
        while True:
            now = time.monotonic()
            while self._calls and now - self._calls[0] > _RATE_WINDOW_S:
                self._calls.popleft()
            if len(self._calls) < self._calls_per_minute:
                self._calls.append(now)
                return
            time.sleep(min(_RATE_WINDOW_S, _RATE_WINDOW_S - (now - self._calls[0]) + 0.05))

    def query(self, api_name: str, params: dict | None = None, fields: str = "") -> list[dict]:
        """调用一个接口, 返回按 fields 对齐的行字典列表。空数据返回 []。

        业务错误(积分/权限/频率/参数)抛 TushareError; 传输错误重试后仍失败也抛 TushareError。
        """
        payload = {
            "api_name": api_name,
            "token": self._token,
            "params": params or {},
            "fields": fields or "",
        }
        body: dict | None = None
        last_error: Exception | None = None
        for attempt in range(_MAX_ATTEMPTS):
            self._throttle()
            try:
                resp = self._client.post(API_URL, json=payload)
                resp.raise_for_status()
                body = resp.json()
                break
            except (httpx.HTTPError, ValueError) as e:
                # ValueError: 网关/代理返回的非 JSON 响应(HTML 错误页等)
                last_error = e
                if attempt + 1 < _MAX_ATTEMPTS:
                    time.sleep(_RETRY_BACKOFF_S * (attempt + 1))
        if body is None:
            raise TushareError(f"{api_name} 请求失败(重试 {_MAX_ATTEMPTS} 次): {last_error}")

        code = body.get("code")
        if code != 0:
            raise TushareError(f"{api_name} 返回错误 code={code}: {body.get('msg') or '无消息'}")

        data = body.get("data") or {}
        columns = data.get("fields") or []
        items = data.get("items") or []
        if not columns or not items:
            return []
        return [dict(zip(columns, row, strict=False)) for row in items]
