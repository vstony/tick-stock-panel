"""自定义源请求模板: `body` / `params` 里的 `${name}` / `${name:%格式}` 占位符。

为什么需要: 有些供应商的请求格式无法用「固定 URL + 固定参数名」表达——
Tushare 要求 token 在请求体、参数嵌套在 `params` 下、`ts_code` 是逗号串、
日期是紧凑 `YYYYMMDD`(实测短横与 ISO 都被静默当无数据), 分钟时间又是
`YYYY-MM-DD HH:MM:SS`。模板让这类协议可以用纯 YAML 描述, 不必为每家写代码。

语法与语义:
  - ``${symbols}``        → 逗号拼接的标的串(如 ``600519.SH,000001.SZ``)
  - ``${start}``          → 请求起始时间; ``${start:%Y%m%d}`` 指定 strftime 格式
  - ``${end}``            → 请求结束时间, 同上
  - ``${table}``          → 财务表名(financial 数据集)
  - ``${asset_type}`` / ``${freq}`` → 资产类型/周期(minute、full_minute)
  - 值不存在(None) → 渲染为空串; 未知变量 → **保留原样**并告警, 便于上游报错时定位
  - 声明了任一占位符的数据集, 不再做默认的参数注入(见 provider._request_rows)

安全: 只做字面替换, 不求值、不 eval。
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Any

logger = logging.getLogger(__name__)

_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?\}")


def has_placeholder(value: Any) -> bool:
    """递归判断 dict/list/str 中是否含有占位符(用于决定是否跳过默认参数注入)。"""
    if isinstance(value, str):
        return _PLACEHOLDER.search(value) is not None
    if isinstance(value, dict):
        return any(has_placeholder(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(has_placeholder(v) for v in value)
    return False


def _format(value: Any, fmt: str | None, name: str) -> str:
    if value is None:
        return ""
    if fmt:
        if isinstance(value, (datetime, date)):
            return value.strftime(fmt)
        logger.warning("请求模板 ${%s:%s}: 值不是时间类型(%s), 忽略格式直接转字符串",
                       name, fmt, type(value).__name__)
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _render_str(text: str, context: dict[str, Any]) -> str:
    def _replace(match: re.Match) -> str:
        name, fmt = match.group(1), match.group(2)
        if name not in context:
            logger.warning(
                "请求模板引用了未知变量 ${%s}(可用: %s), 保持原样发送",
                name, ", ".join(sorted(context)),
            )
            return match.group(0)
        return _format(context[name], fmt, name)

    return _PLACEHOLDER.sub(_replace, text)


def render(value: Any, context: dict[str, Any]) -> Any:
    """递归渲染 dict/list/str 模板; 其他类型原样返回。"""
    if isinstance(value, str):
        return _render_str(value, context)
    if isinstance(value, dict):
        return {key: render(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [render(item, context) for item in value]
    return value
