"""Normalize provider responses into internal Polars schemas."""
from __future__ import annotations

import logging
from datetime import date

import polars as pl

from app.indicators.pipeline import filter_halt_days

logger = logging.getLogger(__name__)

DAILY_COLS = ["symbol", "date", "open", "high", "low", "close", "volume", "amount", "quote_ts"]
ADJ_FACTOR_COLS = ["symbol", "trade_date", "ex_factor"]
INSTRUMENT_COLS = ["symbol", "name", "code", "exchange", "asset_type", "source"]

# 复权事件判定阈值(单事件比值相对 1 的最小偏差)。实测依据(60 只样本 x 2023-2025):
#   - 供应商因子常只保留 4 位小数且存在修订抖动, 如 600519 2024-06-25 8.02→8.021、
#     次日回退 8.021→8.02, 抖动幅度 ≤1.3e-4;
#   - 同期真实除权事件的最小偏差为 7.5e-4(样本最小比值 1.000746)。
#   3e-4 落在两者之间(对抖动 2.3 倍余量, 对最小真实事件 2.5 倍余量)。
ADJ_EVENT_MIN_REL_DIFF = 3e-4
# 单事件比值合理区间兜底(送转/配股可 >1 较多, 但不应超出量级)。
ADJ_FACTOR_RANGE = (0.2, 20.0)


def cumulative_adj_factors_to_events(
    df: pl.DataFrame,
    *,
    start: date | None = None,
    end: date | None = None,
) -> pl.DataFrame:
    """累积复权因子序列 → 单事件比值。

    内部 ``ex_factor`` 契约是「每次除权事件的 前收盘价/除权参考价 比值」(非累积),
    累积链由 indicators pipeline 自建(见 ``_apply_adj_factor``)。Tushare ``adj_factor``、
    同花顺事件 dump 等供应商给的是**累积**因子, 这里用
    ``ratio = factor(D) / factor(前一交易日)`` 换算成单事件比值 —— 累积乘积恰好还原
    供应商的累积序列, 故前复权口径与上游一致。

    输入需含 symbol/trade_date/ex_factor(未类型化会被强制转换并丢弃空值);
    抖动(小于 ADJ_EVENT_MIN_REL_DIFF)与超出量级的值一律剔除, 不留模糊中间态。
    start/end 用于把结果裁回调用方请求的窗口(调用方通常多取一段回看窗口,
    否则窗口首个除权日会因缺少前一交易日因子而丢失)。
    """
    schema = {"symbol": pl.String, "trade_date": pl.Date, "ex_factor": pl.Float64}
    required = {"symbol", "trade_date", "ex_factor"}
    if df.is_empty() or not required.issubset(set(df.columns)):
        return pl.DataFrame(schema=schema)

    frame = (
        df.select("symbol", "trade_date", "ex_factor")
        .with_columns(
            pl.col("symbol").cast(pl.String, strict=False),
            pl.col("trade_date").cast(pl.Date, strict=False),
            pl.col("ex_factor").cast(pl.Float64, strict=False),
        )
        .drop_nulls()
        .filter(pl.col("ex_factor") > 0)
        .sort(["symbol", "trade_date"])
    )
    if frame.is_empty():
        return pl.DataFrame(schema=schema)

    with_ratio = frame.with_columns(
        (pl.col("ex_factor") / pl.col("ex_factor").shift(1).over("symbol")).alias("_ratio")
    ).drop_nulls("_ratio")  # 每个 symbol 的首行无前值 → 无法判定, 丢弃
    if with_ratio.is_empty():
        return pl.DataFrame(schema=schema)

    low, high = ADJ_FACTOR_RANGE
    in_range = with_ratio.filter(pl.col("_ratio").is_between(low, high, closed="both"))
    dropped = with_ratio.height - in_range.height
    if dropped:
        logger.warning(
            "复权因子: %d 个单事件比值超出合理区间 %s, 已剔除(疑似上游异常数据)",
            dropped, ADJ_FACTOR_RANGE,
        )

    events = in_range.filter((pl.col("_ratio") - 1.0).abs() >= ADJ_EVENT_MIN_REL_DIFF)
    if start is not None:
        events = events.filter(pl.col("trade_date") >= start)
    if end is not None:
        events = events.filter(pl.col("trade_date") <= end)
    if events.is_empty():
        return pl.DataFrame(schema=schema)
    return (
        events.select("symbol", "trade_date", pl.col("_ratio").alias("ex_factor"))
        .sort(["symbol", "trade_date"])
    )


def to_polars(data) -> pl.DataFrame:
    if data is None:
        return pl.DataFrame()
    if isinstance(data, pl.DataFrame):
        return data
    if isinstance(data, dict):
        rows: list[dict] = []
        for sym, values in data.items():
            for item in values or []:
                row = dict(item or {})
                row.setdefault("symbol", sym)
                rows.append(row)
        return pl.DataFrame(rows) if rows else pl.DataFrame()
    if hasattr(data, "reset_index"):
        return pl.from_pandas(data.reset_index())
    try:
        return pl.DataFrame(data)
    except Exception:  # noqa: BLE001
        return pl.DataFrame()


def normalize_daily(data, default_symbol: str | None = None, source: str = "tickflow") -> pl.DataFrame:  # noqa: ARG001
    df = to_polars(data)
    if df.is_empty():
        return df
    rename_map = {
        "ts_code": "symbol",
        "trade_date": "date",
        "datetime": "date",
        "vol": "volume",
        "amt": "amount",
        "timestamp": "quote_ts",
    }
    df = df.rename({k: v for k, v in rename_map.items() if k in df.columns})
    if "symbol" not in df.columns and default_symbol:
        df = df.with_columns(pl.lit(default_symbol).alias("symbol"))
    if "date" in df.columns and df.schema["date"] != pl.Date:
        df = df.with_columns(pl.col("date").cast(pl.Date, strict=False))
    # quote_ts: 毫秒级行情时间戳, 用于盘后校验/量比折算。保留为 Int64, 缺失则置 null。
    if "quote_ts" in df.columns:
        df = df.with_columns(pl.col("quote_ts").cast(pl.Int64, strict=False))
    for col in ("open", "high", "low", "close", "volume", "amount"):
        if col in df.columns:
            df = df.with_columns(pl.col(col).cast(pl.Float64, strict=False))
    df = filter_halt_days(df)
    keep = [c for c in DAILY_COLS if c in df.columns]
    return df.select(keep) if keep else pl.DataFrame()


def normalize_adj_factors(data, source: str = "tickflow") -> pl.DataFrame:  # noqa: ARG001
    df = to_polars(data)
    if df.is_empty():
        return df
    rename_map = {
        "timestamp": "trade_date",
        "date": "trade_date",
        "adj_factor": "ex_factor",
    }
    df = df.rename({k: v for k, v in rename_map.items() if k in df.columns})
    if "trade_date" in df.columns:
        if df.schema["trade_date"] in {pl.Int64, pl.Int32, pl.UInt64, pl.UInt32, pl.Float64, pl.Float32}:
            # 毫秒时间戳 → 北京墙钟日期 (直接 from_epoch().dt.date() 是 UTC 日期,
            # 除权事件时间戳为北京零点 = UTC 前一日 16:00, 会整体早一天)。
            df = df.with_columns(
                pl.from_epoch(pl.col("trade_date").cast(pl.Int64), time_unit="ms")
                .dt.replace_time_zone("UTC")
                .dt.convert_time_zone("Asia/Shanghai")
                .dt.replace_time_zone(None)
                .dt.date()
                .alias("trade_date")
            )
        else:
            df = df.with_columns(pl.col("trade_date").cast(pl.Date, strict=False))
    if "ex_factor" in df.columns:
        df = df.with_columns(pl.col("ex_factor").cast(pl.Float64, strict=False))
    keep = [c for c in ADJ_FACTOR_COLS if c in df.columns]
    return df.select(keep).drop_nulls() if len(keep) == len(ADJ_FACTOR_COLS) else pl.DataFrame()


def normalize_instruments(rows: list[dict], asset_type: str, source: str = "tickflow") -> pl.DataFrame:
    if not rows:
        return pl.DataFrame()
    out: list[dict] = []
    for item in rows:
        symbol = item.get("symbol")
        if not symbol:
            continue
        out.append({
            "symbol": str(symbol),
            "name": item.get("name") or str(symbol),
            "code": item.get("code") or str(symbol).split(".")[0],
            "exchange": item.get("exchange"),
            "asset_type": asset_type,
            "source": source,
        })
    if not out:
        return pl.DataFrame()
    return pl.DataFrame(out).select(INSTRUMENT_COLS).unique(subset=["symbol"], keep="last").sort("symbol")
