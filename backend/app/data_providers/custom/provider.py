"""Generic HTTP provider for custom market data sources."""
from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import polars as pl

from app.config import settings
from app.data_providers.base import AssetType
from app.data_providers.custom import template
from app.data_providers.custom.config import CustomSourceConfig, DatasetConfig
from app.data_providers.custom.mapper import (
    apply_transforms,
    datetime_payload,
    extract_rows,
    map_rows,
    payload_error,
)
from app.data_providers.normalizer import (
    cumulative_adj_factors_to_events,
    normalize_adj_factors,
    normalize_daily,
)
from app.market_time import cn_now
from app.tickflow.rate_limits import chunked, sleep_between_batches

logger = logging.getLogger(__name__)

_REQUIRED = {
    "daily": {"symbol", "date", "open", "high", "low", "close", "volume", "amount"},
    "adj_factor": {"symbol", "trade_date", "ex_factor"},
    "realtime": {"symbol", "last_price", "prev_close", "open", "high", "low", "volume"},
    "minute": {"symbol", "datetime", "open", "high", "low", "close", "volume", "amount"},
    # full_minute (全量分钟) 与 minute 同形: 当日窗口批量拉取, 字段映射一致
    "full_minute": {"symbol", "datetime", "open", "high", "low", "close", "volume", "amount"},
    # financial 字段由数据源决定, 只要求能映射出 symbol
    "financial": {"symbol"},
}

# 累积复权因子换算成单事件比值需要"前一交易日"的因子: 取数窗口向前多取一段回看,
# 换算后再裁回调用方请求的窗口(否则窗口首个除权日会丢)。
_ADJ_LOOKBACK_DAYS = 40

# 小数制下 change_pct 的物理上限: A股最大涨跌停 30% (+容差)。
# 中位数口径下小数制批次不可能超过该值, 百分制批次(典型中位数 0.5~3)必然超过。
# 仅对 change_pct 有效——amplitude/turnover_rate 的两种单位在数值区间上重叠
# (百分制 0.05 = 0.05% 与小数制 0.05 = 5%), 无物理依据可判。
_PCT_FRACTION_MAX = 0.31

_PCT_COLUMNS = ("change_pct", "amplitude", "turnover_rate")

# 内部全链路(落盘 / JOIN / 策略)约定的标的格式: 6 位代码.两位交易所大写
# (如 600000.SH / 000001.SZ / 920002.BJ)。
_CANONICAL_SYMBOL_PATTERN = r"^\d{6}\.[A-Z]{2}$"


def _normalize_pct_units(
    df: pl.DataFrame,
    pct_unit: str | None = None,
    transformed_cols: frozenset[str] = frozenset(),
) -> pl.DataFrame:
    """比例字段单位归一为契约小数制 (change_pct/amplitude/turnover_rate,
    0.0366 = 3.66%, CONTRIBUTING §3.1)。单位只认显式声明, 不靠数值猜:

      - pct_unit="percent"  → 三列无条件 /100 (声明即契约, 即使数值看着像小数制);
      - pct_unit="decimal"  → 原样透传 (即使数值看着像百分制也不动);
      - 未声明 → change_pct 保留截面中位数判定(涨跌停 30% 上限使其物理可判:
        样本 >= 5 用 |值| 中位数, 小样本退用最大值, 整批同除 100);
        amplitude/turnover_rate 置 None 交下游重算(enriched 管道按
        high/low/prev_close 与股本口径重算), 除非该列已被 transforms 显式
        处理过(视为用户已接管单位, 原样透传)。
    """
    dropped_undeclared = False
    for col in _PCT_COLUMNS:
        if col not in df.columns:
            continue
        df = df.with_columns(pl.col(col).cast(pl.Float64, strict=False).alias(col))
        if pct_unit == "percent":
            df = df.with_columns((pl.col(col) / 100).alias(col))
        elif pct_unit == "decimal" or col in transformed_cols:
            continue
        elif col == "change_pct":
            vals = df[col].drop_nulls().abs()
            if vals.is_empty():
                continue
            stat = vals.median() if vals.len() >= 5 else vals.max()
            if stat > _PCT_FRACTION_MAX:
                df = df.with_columns((pl.col(col) / 100).alias(col))
        else:
            df = df.with_columns(pl.lit(None, dtype=pl.Float64).alias(col))
            dropped_undeclared = True
    if dropped_undeclared:
        logger.warning(
            "自定义源 realtime 未声明 pct_unit: amplitude/turnover_rate 的单位"
            "无法从数值判定, 已置 None 交由下游按股本/价格口径重算;"
            "请在 realtime 数据集配置中显式声明 pct_unit: percent 或 decimal"
        )
    return df


class GenericHTTPProvider:
    """HTTP-backed custom source. It only handles fetching and schema mapping."""

    def __init__(self, config: CustomSourceConfig) -> None:
        self.config = config
        self.name = config.name
        self._client = httpx.Client(timeout=30.0)

    def close(self) -> None:
        self._client.close()

    def validate(self) -> list[str]:
        errors: list[str] = []
        for dataset, cfg in self.config.datasets.items():
            if not cfg.url:
                errors.append(f"{dataset}: url is required")
            required = _REQUIRED.get(dataset)
            if required:
                mapped = set(cfg.field_map.values())
                missing = sorted(required - mapped)
                if missing:
                    errors.append(f"{dataset}: missing mapped fields: {', '.join(missing)}")
            if cfg.pct_unit is not None:
                if dataset != "realtime":
                    errors.append(f"{dataset}: pct_unit 仅用于 realtime 数据集")
                elif cfg.pct_unit not in ("percent", "decimal"):
                    errors.append(f"{dataset}: pct_unit 必须是 percent 或 decimal")
            if dataset != "realtime":
                request_params = [cfg.symbols_param, cfg.start_param, cfg.end_param]
                if dataset in {"minute", "full_minute"}:
                    request_params.extend(
                        name for name in (cfg.asset_type_param, cfg.freq_param) if name
                    )
                duplicates = sorted({
                    name for name in request_params if request_params.count(name) > 1
                })
                if duplicates:
                    errors.append(
                        f"{dataset}: duplicate request parameter names: "
                        f"{', '.join(duplicates)}"
                    )
        if self.config.auth.type == "body":
            non_post = sorted(
                name for name, cfg in self.config.datasets.items()
                if cfg.method.upper() != "POST"
            )
            if non_post:
                errors.append(
                    "auth.type=body 只在 POST 请求体里有位置, "
                    f"以下数据集不是 POST: {', '.join(non_post)}"
                )
        for dataset, cfg in self.config.datasets.items():
            if cfg.table_map and dataset != "financial":
                errors.append(f"{dataset}: table_map 仅用于 financial 数据集(内部表名→上游表名)")
        return errors

    def _request_rows_retry(
        self, cfg, symbols: list[str], *, start_time=None, end_time=None, retries: int = 1
    ) -> list[dict]:
        """单批请求 + 短退避重试。仍失败抛出, 由调用方决定隔离粒度 (#226)。"""
        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                return self._request_rows(
                    cfg, symbols=symbols, start_time=start_time, end_time=end_time
                )
            except Exception as e:
                last = e
                if attempt < retries:
                    time.sleep(1.0 * (attempt + 1))
        assert last is not None
        raise last

    def get_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done=None,
    ) -> pl.DataFrame:
        cfg = self._dataset("daily")
        frames: list[pl.DataFrame] = []
        chunks = chunked(symbols, cfg.batch)
        failed: list[str] = []
        for i, chunk in enumerate(chunks):
            sleep_between_batches(i, cfg.rpm)
            try:
                rows = self._request_rows_retry(
                    cfg, chunk, start_time=start_time, end_time=end_time
                )
            except Exception as e:
                # 单批失败只隔离该批 (#226): 之前任一批 502 会让整个 stage
                # 抛异常, 已成功批次的结果留在内存里全部丢弃
                failed.extend(chunk)
                logger.warning(
                    "custom daily: batch %d/%d failed (%d symbols), skipped: %s",
                    i + 1, len(chunks), len(chunk), e,
                )
                if on_chunk_done:
                    on_chunk_done(i + 1, len(chunks))
                continue
            df = self._mapped_frame(cfg, rows)
            df = normalize_daily(df, source=self.name)
            if not df.is_empty():
                frames.append(df)
            if on_chunk_done:
                on_chunk_done(i + 1, len(chunks))
        if failed:
            logger.warning(
                "custom daily: %d/%d symbols missing due to batch failures: %s",
                len(failed), len(symbols), ", ".join(failed[:20]),
            )
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    def get_adj_factors(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done=None,
    ) -> pl.DataFrame:
        cfg = self._dataset("adj_factor")
        # adj_factor_mode=cumulative: 上游给累积因子, 本项目换算为单事件比值。
        # 换算需要前一交易日的因子 → 取数窗口向前多取一段, 换算后裁回请求窗口。
        cumulative = cfg.adj_factor_mode == "cumulative"
        fetch_start = (
            start_time - timedelta(days=_ADJ_LOOKBACK_DAYS)
            if cumulative and start_time
            else start_time
        )
        window_start = start_time.date() if (cumulative and start_time) else None
        window_end = end_time.date() if (cumulative and end_time) else None
        frames: list[pl.DataFrame] = []
        chunks = chunked(symbols, cfg.batch)
        failed: list[str] = []
        for i, chunk in enumerate(chunks):
            sleep_between_batches(i, cfg.rpm)
            try:
                rows = self._request_rows_retry(
                    cfg, chunk, start_time=fetch_start, end_time=end_time
                )
            except Exception as e:
                failed.extend(chunk)
                logger.warning(
                    "custom adj_factor: batch %d/%d failed (%d symbols), skipped: %s",
                    i + 1, len(chunks), len(chunk), e,
                )
                if on_chunk_done:
                    on_chunk_done(i + 1, len(chunks))
                continue
            df = self._mapped_frame(cfg, rows)
            df = normalize_adj_factors(df, source=self.name)
            if cumulative and not df.is_empty():
                before = df.height
                df = cumulative_adj_factors_to_events(
                    df, start=window_start, end=window_end
                )
                logger.info(
                    "custom adj_factor: 累积因子 %d 行 → 单事件比值 %d 条 (窗口 %s..%s)",
                    before, df.height, window_start, window_end,
                )
            if not df.is_empty():
                frames.append(df)
            if on_chunk_done:
                on_chunk_done(i + 1, len(chunks))
        if failed:
            logger.warning(
                "custom adj_factor: %d/%d symbols missing due to batch failures: %s",
                len(failed), len(symbols), ", ".join(failed[:20]),
            )
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    def get_realtime(self) -> list[dict]:
        cfg = self._dataset("realtime")
        rows = self._request_rows(cfg)
        df = self._mapped_frame(cfg, rows)
        # 单位归一: 显式 pct_unit 声明优先; 未声明时 amplitude/turnover_rate
        # fail-closed 置 None(交下游重算), change_pct 保留截面判定
        df = _normalize_pct_units(
            df,
            pct_unit=cfg.pct_unit,
            transformed_cols=frozenset(cfg.transforms) & set(_PCT_COLUMNS),
        )
        if df.is_empty():
            return []
        return df.to_dicts()

    def get_minute(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: AssetType = "stock",
        freq: str = "1m",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        """拉取分钟 K。

        asset_type / freq 默认不传上游 (minute dataset URL 应返回 1m 数据)。
        在 dataset 配置中设置 asset_type_param / freq_param 后, 这两个参数会以
        配置的参数名注入请求 (GET → params, POST → body), 用于上游需区分
        stock/ETF/index 或固定频率的场景。
        """
        return self._fetch_minute_dataset(
            "minute", symbols, start_time, end_time, asset_type, freq, on_chunk_done,
        )

    def get_intraday_batch(
        self,
        symbols: list[str],
        count: int = 300,
        asset_type: AssetType = "stock",
    ) -> pl.DataFrame:
        """全量分钟修复轮: 按当日窗口批量拉取 full_minute 数据集 (chunked + rpm 限速)。

        与 get_minute 同形 (字段映射/归一一致), 区别仅在数据集名与窗口由调用方
        传当日值。稳态增量 (get_intraday_latest) YAML 声明式源不提供 — 服务自动
        降级为仅修复轮模式并放慢节奏。
        """
        # 当日窗口按北京时间墙钟 (naive, 与分钟K契约及 kline_sync.fetch_intraday_custom_batch
        # 回退路径同口径): datetime.now() 取服务器本地时区, UTC 主机上窗口整体早 8 小时
        end = cn_now().replace(tzinfo=None)
        start = end.replace(hour=0, minute=0, second=0, microsecond=0)
        return self._fetch_minute_dataset(
            "full_minute", symbols, start, end, asset_type, "1m", None,
        )

    def _fetch_minute_dataset(
        self,
        ds_name: str,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: AssetType = "stock",
        freq: str = "1m",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        cfg = self._dataset(ds_name)
        override: dict[str, Any] = {}
        if cfg.asset_type_param:
            override[cfg.asset_type_param] = asset_type
        if cfg.freq_param:
            override[cfg.freq_param] = freq
        frames: list[pl.DataFrame] = []
        chunks = chunked(symbols, cfg.batch)
        for i, chunk in enumerate(chunks):
            sleep_between_batches(i, cfg.rpm)
            rows = self._request_rows(
                cfg, symbols=chunk, start_time=start_time, end_time=end_time,
                override_params=override or None, override_body=override or None,
                template_context={"asset_type": asset_type, "freq": freq},
            )
            df = self._mapped_frame(cfg, rows)
            df = self._normalize_minute(df)
            if not df.is_empty():
                frames.append(df)
            if on_chunk_done:
                on_chunk_done(i + 1, len(chunks))
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    def get_financials(
        self,
        table: str,
        symbols: list[str],
        latest_only: bool = True,
    ) -> pl.DataFrame:
        """拉取财务数据。table 包含四张财务报表及 shares 股本表。

        custom 源用一个 'financial' dataset 配置覆盖全部财务表; 请求时把 table 作为参数传给上游,
        上游根据 table 返回对应数据。字段由数据源决定, 这里只确保有 symbol 列。
        内部表名 → 上游取值由 `table_map` 映射(如 metrics→fina_indicator); 声明了 table_map
        时它就是**支持范围**: 未声明的表直接跳过(如 Tushare 的 shares 只能按交易日取、
        单标的 6000 行, 默认不开)。
        """
        cfg = self._dataset("financial")
        if cfg.table_map and table not in cfg.table_map:
            logger.info(
                "自定义源 %s: table_map 未声明 %s, 跳过该表", self.name, table,
            )
            return pl.DataFrame()
        frames: list[pl.DataFrame] = []
        chunks = chunked(symbols, cfg.batch)
        for i, chunk in enumerate(chunks):
            sleep_between_batches(i, cfg.rpm)
            upstream_table = cfg.table_map.get(table, table)
            # 把 table 注入到请求参数 (上游据此区分财务表)
            extra_params = {**cfg.params, "table": upstream_table}
            extra_body = {**cfg.body, "table": upstream_table}
            if table == "shares":
                extra_params["latest"] = latest_only
                extra_body["latest"] = latest_only
            rows = self._request_rows(
                cfg, symbols=chunk,
                override_params=extra_params, override_body=extra_body,
                template_context={"table": upstream_table},
            )
            df = self._mapped_frame(cfg, rows)
            if not df.is_empty():
                frames.append(df)
        if not frames:
            return pl.DataFrame()
        merged = pl.concat(frames, how="diagonal_relaxed")
        return self._dedup_report_periods(merged)

    @staticmethod
    def _dedup_report_periods(df: pl.DataFrame) -> pl.DataFrame:
        """财务表契约: (symbol, period_end) 唯一。

        实测 Tushare balancesheet/income 会对同一报告期返回完全重复的行(ann_date 也相同),
        留着会让落盘表出现同期两行、下游 join 翻倍。同期有多个公告日时保留最新公告的那行
        (准则调整/重述以新披露为准), 与 services.financial_sync._merge_report_history 同语义。
        """
        if df.is_empty() or not {"symbol", "period_end"} <= set(df.columns):
            return df
        if "announce_date" in df.columns:
            df = df.sort("announce_date", nulls_last=False)
        return df.unique(subset=["symbol", "period_end"], keep="last")

    @classmethod
    def _normalize_minute(cls, df: pl.DataFrame) -> pl.DataFrame:
        """把映射后的 df 规范成 minute canonical 列。"""
        if df.is_empty():
            return df
        if "datetime" in df.columns and df.schema["datetime"] != pl.Datetime("us"):
            if df.schema["datetime"] == pl.Utf8:
                # 字符串 datetime 直接 cast 会整体置 null (polars 不做字符串解析);
                # 先解析再对齐微秒精度 (#225, 参照
                # kline_sync._enforce_minute_beijing_wallclock 的处理)。
                # Series 级立即解析: 表达式错误要到 collect 才抛, 无法按格式回退
                df = df.with_columns(cls._parse_datetime_series(df["datetime"]))
            df = df.with_columns(pl.col("datetime").cast(pl.Datetime("us"), strict=False))
        for col in ("open", "high", "low", "close", "volume", "amount"):
            if col in df.columns:
                df = df.with_columns(pl.col(col).cast(pl.Float64, strict=False))
        keep = [c for c in ("symbol", "datetime", "open", "high", "low", "close", "volume", "amount") if c in df.columns]
        return df.select(keep) if keep else pl.DataFrame()

    _DATETIME_STR_FORMATS = (
        None,  # 自动推断
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
    )

    @classmethod
    def _parse_datetime_series(cls, s: pl.Series) -> pl.Series:
        """逐格式尝试解析字符串 datetime; 均失败返回全 null (宽松语义)。"""
        for fmt in cls._DATETIME_STR_FORMATS:
            try:
                return (
                    s.str.to_datetime(strict=False, format=fmt)
                    if fmt else s.str.to_datetime(strict=False)
                )
            except Exception:
                continue
        return pl.Series("datetime", [None] * s.len(), dtype=pl.Datetime("us"))

    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        cfg = self._dataset(dataset)
        test_symbols = symbols or ["000001.SZ"]
        end_time = datetime.now()
        start_time = end_time - timedelta(days=7)
        if dataset == "realtime":
            rows = self._request_rows(cfg)
        elif dataset in {"minute", "full_minute"}:
            override: dict[str, Any] = {}
            if cfg.asset_type_param:
                override[cfg.asset_type_param] = "stock"
            if cfg.freq_param:
                override[cfg.freq_param] = "1m"
            rows = self._request_rows(
                cfg,
                symbols=test_symbols,
                start_time=start_time,
                end_time=end_time,
                override_params=override or None,
                override_body=override or None,
            )
        elif dataset in {"daily", "adj_factor"}:
            rows = self._request_rows(
                cfg,
                symbols=test_symbols,
                start_time=start_time,
                end_time=end_time,
            )
        else:
            rows = self._request_rows(cfg, symbols=test_symbols)
        df = self._mapped_frame(cfg, rows)
        return {
            "provider": self.name,
            "dataset": dataset,
            "rows": len(rows),
            "columns": df.columns,
            "preview": df.head(5).to_dicts() if not df.is_empty() else [],
        }

    def _dataset(self, name: str) -> DatasetConfig:
        cfg = self.config.datasets.get(name)
        if not cfg:
            raise ValueError(f"Custom data source '{self.name}' does not configure dataset '{name}'")
        return cfg

    def _mapped_frame(self, cfg: DatasetConfig, rows: list[dict]) -> pl.DataFrame:
        df = map_rows(rows, cfg.field_map)
        if rows and df.is_empty() and not (set(rows[0]) & set(cfg.field_map)):
            # 响应有内容但一个字段都对不上: 绝不能静默当空数据(否则用户只看到 0 行)。
            # 构造失败(map_rows 已告警)时字段其实能对上, 不重复误导。
            logger.warning(
                "自定义源 %s: 上游返回 %d 条记录但无任何字段可映射, "
                "请核对 response_path/field_map 与上游字段名(响应字段样例: %s)",
                self.name,
                len(rows),
                sorted(rows[0].keys())[:12],
            )
        df = apply_transforms(df, cfg.transforms)
        self._warn_on_odd_symbols(df)
        return df

    def _warn_on_odd_symbols(self, df: pl.DataFrame) -> None:
        """symbol 不是 `600000.SH` 规范格式时告警。

        上游普遍只认「代码.交易所(大写后缀)」: 实测 Tushare 对 600000 / sh600000 /
        600000.XSHG / 600000.sz 全部返回 code=0 但 **0 行**(静默无数据, 不报错), 而内部
        落盘与 JOIN 也按该格式对齐 —— 值不对时给一条可定位的告警, 不让"0 行"无从查起。
        """
        if df.is_empty() or "symbol" not in df.columns:
            return
        values = df.get_column("symbol").cast(pl.Utf8, strict=False).drop_nulls()
        if values.is_empty():
            return
        odd = values.filter(~values.str.contains(_CANONICAL_SYMBOL_PATTERN)).unique()
        if odd.is_empty():
            return
        logger.warning(
            "自定义源 %s: symbol 不是「600000.SH」规范格式(示例: %s) —— 上游通常只接受"
            "「6 位代码.交易所大写后缀」, 其他写法会返回 0 行; 请核对传入标的与 field_map 的 symbol 映射",
            self.name,
            ", ".join(odd.head(3).to_list()),
        )

    def _request_rows(
        self,
        cfg: DatasetConfig,
        *,
        symbols: list[str] | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        override_params: dict[str, Any] | None = None,
        override_body: dict[str, Any] | None = None,
        template_context: dict[str, Any] | None = None,
    ) -> list[dict]:
        headers, auth_params = self._auth_parts()
        # 声明了占位符的数据集: 请求参数完全由 body/params 模板描述, 不再做默认注入
        # (注入的标的列表会破坏「嵌套 params + 逗号串」这类协议, 如 Tushare)
        use_template = template.has_placeholder(cfg.params) or template.has_placeholder(cfg.body)
        params = dict(cfg.params)
        params.update(auth_params)
        body = dict(cfg.body)
        if use_template:
            context: dict[str, Any] = {
                "symbols": ",".join(symbols or []),
                "start": start_time,
                "end": end_time,
                "table": None,
                "asset_type": None,
                "freq": None,
                **(template_context or {}),
            }
            params = template.render(params, context)
            body = template.render(body, context)
        else:
            if override_params:
                params.update(override_params)
            if override_body:
                body.update(override_body)
            if symbols:
                body[cfg.symbols_param] = symbols
                params.setdefault(cfg.symbols_param, ",".join(symbols))
            start_value = datetime_payload(start_time)
            end_value = datetime_payload(end_time)
            if start_value:
                body[cfg.start_param] = start_value
                params.setdefault(cfg.start_param, start_value)
            if end_value:
                body[cfg.end_param] = end_value
                params.setdefault(cfg.end_param, end_value)
        body.update(self._body_auth())

        method = cfg.method.upper()
        request_kwargs: dict[str, Any] = {"headers": headers, "timeout": cfg.timeout}
        if method == "GET":
            request_kwargs["params"] = params
        else:
            request_kwargs["params"] = auth_params
            request_kwargs["json"] = body
        resp = self._client.request(method, cfg.url, **request_kwargs)
        resp.raise_for_status()
        payload = resp.json()
        rows = extract_rows(payload, cfg.response_path)
        if not rows:
            # 上游把业务错误放在 200 响应里(code/msg)时, 不能静默当"无数据"
            err = payload_error(payload)
            if err:
                logger.warning(
                    "自定义源 %s 上游返回业务错误(HTTP %s): %s",
                    self.name, resp.status_code, err,
                )
        return rows

    def _body_auth(self) -> dict[str, str]:
        """auth.type=body: 把 Token 注入 POST 请求体(参数名 auth.param)。"""
        auth = self.config.auth
        if auth.type != "body":
            return {}
        token = _token_from_env(auth.token_env) if auth.token_env else None
        if not token:
            logger.warning("custom data source %s auth token is not set", self.name)
            return {}
        return {auth.param or "token": token}

    def _auth_parts(self) -> tuple[dict[str, str], dict[str, str]]:
        auth = self.config.auth
        if auth.type == "none":
            return {}, {}
        token = _token_from_env(auth.token_env) if auth.token_env else None
        if not token:
            logger.warning("custom data source %s auth token is not set", self.name)
            return {}, {}
        if auth.type == "bearer":
            return {auth.header: f"Bearer {token}"}, {}
        if auth.type == "header":
            return {auth.header: token}, {}
        if auth.type == "query":
            return {}, {auth.param: token}
        return {}, {}


def _token_from_env(name: str | None) -> str | None:
    if not name:
        return None
    token = os.getenv(name)
    if token:
        return token
    candidates = [settings.data_dir.parent / ".env", Path.cwd() / ".env", Path.cwd().parent / ".env"]
    env_path = next((path for path in candidates if path.exists()), None)
    if env_path is None:
        return None
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if not text or text.startswith("#") or "=" not in text:
                continue
            key, value = text.split("=", 1)
            if key.strip() == name:
                return value.strip().strip('"').strip("'")
    except Exception:
        return None
    return None
