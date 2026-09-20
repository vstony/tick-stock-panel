"""Mapping helpers for custom data sources."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import polars as pl

logger = logging.getLogger(__name__)


def _columnar_rows(node: Any) -> list[dict] | None:
    """列式信封(`fields` + `items`) → 记录列表; 不是该形状时返回 None。

    判定纯结构性(字段名必须是字符串列表, 且 items 是等长数组), 不做数值猜测: 这类
    响应只有一种合理读法, 即 ``items[i][j]`` 对应 ``fields[j]``(Tushare 等网关的通用约定)。
    """
    if not isinstance(node, dict):
        return None
    fields, items = node.get("fields"), node.get("items")
    if not isinstance(fields, list) or not isinstance(items, list):
        return None
    if not all(isinstance(name, str) for name in fields):
        return None
    if not all(isinstance(row, (list, tuple)) for row in items):
        return None
    if not fields or not items:
        return []  # 空信封 = 无数据
    rows: list[dict] = []
    malformed = 0
    for row in items:
        if len(row) != len(fields):
            malformed += 1
            continue
        rows.append(dict(zip(fields, row, strict=True)))
    if malformed:
        logger.warning(
            "列式信封中有 %d/%d 行的列数与 fields 不一致, 已丢弃这些行",
            malformed, len(items),
        )
    return rows


def _is_columnar_envelope(node: Any) -> bool:
    """判断是不是「字段名与数据分离」的列式信封(如 Tushare 的 data.fields + data.items)。"""
    return _columnar_rows(node) is not None


def extract_rows(payload: Any, response_path: str = "") -> list[dict]:
    """Extract a list of row dicts from a JSON payload using dot-path lookup.

    接受两种记录形状: ``list[dict]``(或单个对象), 以及列式信封 ``{fields: [...], items: [[...]]}``
    (按字段名与位置对应解包)。其他形状(纯位置数组、嵌套非记录结构)无法建立字段
    对应关系, 这里明确告警后返回空, 不猜测列序——猜错会静默生成看似合理的错误行情。
    """
    data = payload
    if response_path:
        for part in response_path.split("."):
            if not part:
                continue
            if isinstance(data, dict):
                data = data.get(part)
            else:
                data = None
                break
    if data is None:
        return []
    if isinstance(data, dict):
        columnar = _columnar_rows(data)
        if columnar is not None:
            return columnar
        return [data]
    if isinstance(data, list):
        rows = [item for item in data if isinstance(item, dict)]
        if data and not rows:
            logger.warning(
                "自定义源响应是 %s 数组(位置数组), 元素不是对象, 无法作为记录映射 "
                "(response_path=%r)",
                type(data[0]).__name__,
                response_path,
            )
        return rows
    return []


def payload_error(payload: Any) -> str | None:
    """把上游的「HTTP 200 + 业务错误码」文文案识别出来(code/msg 风格)。

    Tushare 等网关用 ``code``/``msg`` 表达错误(积分不足/无权限/参数非法), HTTP 状态
    码恒为 200。不识别时会把错误当成"无数据", 用户只看到 0 行。
    """
    if not isinstance(payload, dict):
        return None
    code = payload.get("code")
    if code is None or code == 0 or code == "0":
        return None
    msg = payload.get("msg") or payload.get("message") or payload.get("detail")
    return f"code={code}" + (f", msg={msg}" if msg else "")


def map_rows(rows: list[dict], field_map: dict[str, str]) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame()
    try:
        df = pl.DataFrame(rows)
    except (TypeError, ValueError, pl.exceptions.PolarsError) as e:
        # 异构嵌套(如把整个信封当一行)会让 polars 构造失败; 返回空 + 告警,
        # 避免同步任务带着 polars 内部异常栈失败
        logger.warning("自定义源响应无法构造成记录表(字段混型/嵌套结构): %s", e)
        return pl.DataFrame()
    rename = {src: dst for src, dst in field_map.items() if src in df.columns and src != dst}
    if rename:
        df = df.rename(rename)
    keep = list(dict.fromkeys(field_map.values()))
    keep = [col for col in keep if col in df.columns]
    return df.select(keep) if keep else pl.DataFrame()


def apply_transforms(df: pl.DataFrame, transforms: dict[str, str]) -> pl.DataFrame:
    """Apply a small safe transform set. No eval is used."""
    if df.is_empty() or not transforms:
        return df
    out = df
    for col, expr in transforms.items():
        if col not in out.columns:
            continue
        text = expr.strip()
        if text == "value * 100":
            out = out.with_columns((pl.col(col).cast(pl.Float64, strict=False) * 100).alias(col))
        elif text == "value * 1000":
            # 常见上游单位: 成交额以千元计(Tushare daily/fund_daily/index_daily)
            out = out.with_columns((pl.col(col).cast(pl.Float64, strict=False) * 1000).alias(col))
        elif text == "value / 100":
            out = out.with_columns((pl.col(col).cast(pl.Float64, strict=False) / 100).alias(col))
        elif text == "value / 10000":
            out = out.with_columns((pl.col(col).cast(pl.Float64, strict=False) / 10000).alias(col))
        elif text.startswith("parse_date("):
            fmt = _extract_format(text) or "%Y-%m-%d"
            out = out.with_columns(
                pl.col(col).cast(pl.Utf8, strict=False).str.strptime(pl.Date, format=fmt, strict=False).alias(col)
            )
        elif text.startswith("parse_datetime("):
            fmt = _extract_format(text) or "%Y-%m-%d %H:%M:%S"
            out = out.with_columns(
                pl.col(col).cast(pl.Utf8, strict=False).str.strptime(pl.Datetime, format=fmt, strict=False).alias(col)
            )
    return out


def _extract_format(expr: str) -> str | None:
    for quote in ("'", '"'):
        if quote in expr:
            parts = expr.split(quote)
            if len(parts) >= 3:
                return parts[1]
    return None


def datetime_payload(value: datetime | None) -> str | None:
    return value.isoformat() if value else None
