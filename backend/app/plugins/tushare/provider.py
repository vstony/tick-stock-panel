"""Tushare Pro 内置数据源 provider(零依赖 HTTP, runtime: none)。

方法签名对齐 custom.GenericHTTPProvider(service 分流点按这套签名调用),
注入 custom loader 注册表后, 各 service 无需改动即可路由到本 provider。

实现数据集:
  - daily      A 股日K(``daily``) / ETF(``fund_daily``) / 指数(``index_daily``), 不复权原始价
  - adj_factor 除权因子: A 股 ``adj_factor`` / ETF ``fund_adj``
  - minute     分钟K(``stk_mins``, 股票/ETF/指数均可取), 支持 1/5/15/30/60min
  - financial  财务四表: ``metrics``(fina_indicator) / ``income`` / ``balance_sheet`` /
    ``cash_flow``, 按标的逐只请求(接口不支持多标的)
  - 标的维表   get_instruments: ``stock_basic`` + 最新交易日 ``daily_basic`` 股本
未声明的数据集(``realtime`` / ``depth5`` / ``full_minute``)由
``provider_has_dataset`` 判为 False → 自动回退 TickFlow:
  - realtime: Tushare 无全市场快照接口(``realtime_quote`` 只支持少量标的),
    撑不住 6s 一轮的全市场轮询, 故不声明;
  - depth5: Tushare 不提供五档盘口;
  - full_minute: ``stk_mins`` 按标的分批拉取(全市场一轮 ≈279 请求), 无全市场批量端点,
    也没有可用的实时分钟端点(``rt_min`` 必填 ts_code 且返回无 ``trade_time``,
    ``rt_min_daily`` 无权限), 支撑不了盘中落盘服务, 故不声明。

单位与口径 (CONTRIBUTING §3, 均为 2026-09 实测, 不可凭字段名推断):
  - daily / fund_daily / index_daily 的 ``amount`` 单位为**千元** → 显式 x1000 得元;
    ``vol`` 已是**手**(实测 600519 20250918: 49721.25 手 = 4,972,125 股,
    均价 1477.7 元 x 股数 ≈ amount 千元 x 1000), 与内部契约一致, 不换算。
  - ``stk_mins`` 的 ``vol`` 单位为**股**(实测 600519 2026-09-18 全天分钟 vol 合计
    2,489,087 = 日线 24,890.87 手 x 100), ``amount`` 单位为**元**
    (分钟合计 3,135,849,115 元 = 日线 3,135,849.108 千元 x 1000)
    → 分钟 vol 除以 100 得手, amount 原样。
  - 日K OHLC 为不复权原始价, 复权一律交给 indicators pipeline(provider 不得自行复权)。
  - 分钟 ``trade_time`` 为北京时间墙钟字符串, 直接解析入库, 不做时区换算(CONTRIBUTING §3.3)。
  - ``adj_factor`` / ``fund_adj`` 是**累积复权因子**(实测 600519 2023-2025 仅在除权日
    跳变, 且 2024-06-19 除权日的比值 8.02/7.858 = 1.0206 与当日年度分红比例一致),
    本项目 ``ex_factor`` 契约是**单事件比值** → 本 provider 用
    ``ex_factor = adj(D) / adj(前一交易日)`` 换算, 累积链由 pipeline 重建。
  - 财务四表(实测 2026-09): 金额类字段(fina_indicator 的 eps/bps 除外)单位为**元**、
    指标类(``grossprofit_margin``/``netprofit_margin``/``debt_to_assets``/``roe_waa``/
    ``or_yoy``/``netprofit_yoy``)为**百分数**, 与内部契约一致 → 不换算;
    报告期 ``end_date``/公告日 ``ann_date`` 是 ``YYYYMMDD`` 字符串; 报告期口径 =
    ``(symbol, period_end)`` 唯一, 同期多公告日保留最新那行(准则调整以新披露为准)。
    另需注意 ``grossprofit_margin`` 是**毛利率**、``gross_margin`` 是**毛利额**,
    两者不可混用(``netprofit_margin`` 同理)。
  - 财务接口**不支持多标的**(实测逗号串静默返回 0 行, ``batch: 1`` 是硬约束),
    且各接口配额约 500 次/分钟 → 客户端自限速 400 次/分钟下全 A ≈ 14 分钟/表。
"""

from __future__ import annotations

import contextlib
import logging
import math
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo

import polars as pl

from app.data_providers.normalizer import normalize_daily
from app.plugins.tushare import client as tushare_client
from app.plugins.tushare.client import TushareClient, TushareError

logger = logging.getLogger(__name__)

_DATASETS = ("daily", "adj_factor", "minute", "financial")

API_KEY_ENV = "TUSHARE_API_KEY"
SECRETS_FIELD = "tushare_api_key"  # 设置页配置的 Key 存 secrets.json, 优先级高于 .env

_BEIJING = ZoneInfo("Asia/Shanghai")

# 接口单次返回行数上限(实测: daily 6000 行, stk_mins 8000 行)。
# 超限由上游**静默截断**: 实测 50 只 x 单日 1min 只回 8000 行(≈33 只的完整数据),
# 因此分批必须按行数预算, 并留余量。
_DAILY_ROW_LIMIT = 6000
_MINUTE_ROW_LIMIT = 8000
_ROW_HEADROOM = 0.95
# 逗号拼标的的批量上限(实测 daily 50 只 x 10 天 = 450 行正常返回)
_DAILY_CODES_CAP = 50
_MINUTE_CODES_CAP = 40
# 无明确窗口时的默认回溯(日K/复权/分钟)。与 TickFlow 路径的 count 兜底语义一致。
_DAILY_DEFAULT_DAYS = 365
_ADJ_DEFAULT_DAYS = 365
_MINUTE_DEFAULT_DAYS = 5

# Tushare 分钟周期 → 单日 bar 数。1min 实测 241 根(09:30 竞价 + 09:31-11:30 + 13:01-15:00)。
_FREQ_BARS_PER_DAY = {"1min": 241, "5min": 49, "15min": 17, "30min": 9, "60min": 5}
_FREQ_MAP = {
    "1m": "1min", "1min": "1min",
    "5m": "5min", "5min": "5min",
    "15m": "15min", "15min": "15min",
    "30m": "30min", "30min": "30min",
    "60m": "60min", "60min": "60min",
}

# 资产类型 → 日K/复权接口。指数无复权因子(指数本身不除权), 故 _ADJ_API 无 index。
_DAILY_API = {"stock": "daily", "etf": "fund_daily", "index": "index_daily"}
_ADJ_API = {"stock": "adj_factor", "etf": "fund_adj"}

_MINUTE_COLS_ORDER = ["symbol", "datetime", "open", "high", "low", "close", "volume", "amount"]

_DAILY_FIELDS = "ts_code,trade_date,open,high,low,close,vol,amount"
_MINUTE_FIELDS = "ts_code,trade_time,open,high,low,close,vol,amount"
_ADJ_FIELDS = "ts_code,trade_date,adj_factor"

# 响应必含列。接口结构变化(字段改名/变空)时宁可整批丢弃并告警, 不能静默当空数据处理。
_DAILY_REQUIRED = ("ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount")
_MINUTE_REQUIRED = ("ts_code", "trade_time", "open", "high", "low", "close", "vol", "amount")

# 交易所代码归一: Tushare 用 SSE/SZSE/BSE, 项目维表用 SH/SZ/BJ(与 symbol 后缀一致)。
_EXCHANGE_MAP = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}
_SHARES_LOOKBACK_DAYS = 25  # 找最近交易日取股本: 覆盖春节/国庆长假

# 复权事件判定阈值(比值相对 1 的最小偏差)。实测依据(60 只样本 x 2023-2025):
#   - Tushare 因子保留 4 位小数且存在修订抖动, 如 600519 2024-06-25 8.02→8.021、
#     次日回退 8.021→8.02, 2024-05-15 7.8576→7.858, 抖动幅度 ≤1.3e-4;
#   - 同期真实除权事件的最小偏差为 7.5e-4(样本最小比值 1.000746)。
#   3e-4 落在两者之间(对抖动 2.3 倍余量, 对最小真实事件 2.5 倍余量)。
_ADJ_EVENT_MIN_REL_DIFF = 3e-4
# 窗口前多取一段: 事件比值需要"前一交易日"的因子, 否则窗口首个除权日会丢失。
_ADJ_LOOKBACK_DAYS = 40
# 单事件比值合理区间兜底(送转/配股可 >1 较多, 但不应超出量级)。
_ADJ_FACTOR_RANGE = (0.2, 20.0)

# ---------------------------------------------------------------- 财务四表
# 内部表 → Tushare 接口。实测这四张表**不支持多标的**(逗号串静默返回 0 行), 必须逐只请求;
# ``shares``(历史股本)不接入: Tushare 只有按交易日的 ``daily_basic``, 单标的数千行、
# 全 A 约 3000 万行, 默认每次同步跑代价过高(需要时应在指标侧解决, 不是每轮重拉)。
_FINANCIAL_API = {
    "metrics": "fina_indicator",
    "income": "income",
    "balance_sheet": "balancesheet",
    "cash_flow": "cashflow",
}
# 并集字段: 各接口忽略自己不存在的字段(实测), 一份清单通吃四表, 省一处漂移。
_FINANCIAL_FIELDS = (
    "ts_code,end_date,ann_date,basic_eps,total_revenue,revenue,oper_cost,sell_exp,admin_exp,"
    "rd_exp,fin_exp,operate_profit,total_profit,income_tax,n_income,n_income_attr_p,"
    "total_cur_assets,total_nca,money_cap,accounts_receiv,total_assets,total_liab,"
    "total_hldr_eqy_exc_min_int,n_cashflow_act,n_cashflow_inv_act,n_cash_flows_fnc_act,"
    "c_pay_acq_const_fiolta,n_incr_cash_cash_equ,eps,bps,roe_waa,roa,grossprofit_margin,"
    "netprofit_margin,debt_to_assets,or_yoy,netprofit_yoy"
)
# 上游字段 → 内部契约列名(与 data_providers/custom 路线的 field_map 完全一致)。
_FINANCIAL_FIELD_MAP = {
    "ts_code": "symbol",
    "end_date": "period_end",
    "ann_date": "announce_date",
    "basic_eps": "basic_eps",
    "total_revenue": "total_revenue",
    "revenue": "revenue",
    "oper_cost": "operating_cost",
    "sell_exp": "selling_expense",
    "admin_exp": "admin_expense",
    "rd_exp": "rd_expense",
    "fin_exp": "financial_expense",
    "operate_profit": "operating_profit",
    "total_profit": "total_profit",
    "income_tax": "income_tax",
    "n_income": "net_income",
    "n_income_attr_p": "net_income_attributable",
    "total_cur_assets": "total_current_assets",
    "total_nca": "total_non_current_assets",
    "money_cap": "cash_and_equivalents",
    "accounts_receiv": "accounts_receivable",
    "total_assets": "total_assets",
    "total_liab": "total_liabilities",
    "total_hldr_eqy_exc_min_int": "total_equity",
    "n_cashflow_act": "net_operating_cash_flow",
    "n_cashflow_inv_act": "net_investing_cash_flow",
    "n_cash_flows_fnc_act": "net_financing_cash_flow",
    "c_pay_acq_const_fiolta": "capex",
    "n_incr_cash_cash_equ": "net_cash_change",
    "eps": "eps_basic",
    "bps": "bps",
    "roe_waa": "roe",
    "roa": "roa",
    "grossprofit_margin": "gross_margin",
    "netprofit_margin": "net_margin",
    "debt_to_assets": "debt_to_asset_ratio",
    "or_yoy": "revenue_yoy",
    "netprofit_yoy": "net_income_yoy",
}
_FINANCIAL_REQUIRED = ("ts_code", "end_date")
_FINANCIAL_KEYS = ("symbol", "period_end", "announce_date")
# 逐只请求时单只失败不该刷屏, 也不该掩埋整表失败: 前几条打明细, 末尾给汇总。
_FINANCIAL_WARN_LIMIT = 3


def get_api_key() -> str:
    from app import secrets_store

    return secrets_store.get_env_backed_secret(SECRETS_FIELD, API_KEY_ENV)


def availability() -> tuple[bool, str]:
    """loader 启动自检: 配了 Key(secrets.json 或 .env)才注册为可切换数据源。不抛异常。"""
    if get_api_key():
        return True, "ok"
    # 状态行会拼在「未配置」标签之后, 文案不再重复"未配置"字样
    return False, f"缺少 API Key(可在下方输入框直接填写,或配置环境变量 {API_KEY_ENV})"


def probe_api_key(api_key: str) -> tuple[bool, str]:
    """用候选 Key 实探一次轻量接口(交易日历, 8 行), 先探后存。不落盘。"""
    client = None
    try:
        client = tushare_client.TushareClient(token=api_key, timeout=10.0)
        today = datetime.now().date()
        rows = client.query(
            "trade_cal",
            {
                "exchange": "SSE",
                "start_date": (today - timedelta(days=7)).strftime("%Y%m%d"),
                "end_date": today.strftime("%Y%m%d"),
            },
            "cal_date,is_open",
        )
        if not rows:
            return False, "Key 有效但交易日历返回空(请稍后重试)"
        return True, "ok"
    except TushareError as e:
        return False, f"Key 无效或网络失败: {e}"
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()


@dataclass
class _TushareConfig:
    """轻量 config shim, 让 custom loader 的 provider_has_dataset 能识别本 provider。"""

    name: str = "tushare"
    display_name: str = "Tushare"
    datasets: dict = field(default_factory=lambda: dict.fromkeys(_DATASETS))
    path: None = None
    builtin: bool = True


# ---------------------------------------------------------------- 通用小工具

def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _chunked(items: list[str], size: int) -> list[list[str]]:
    size = max(1, size)
    return [items[i : i + size] for i in range(0, len(items), size)]


def _beijing_date(value: datetime | None, default: date) -> date:
    """datetime → 日期。带时区的先换算到北京时间; naive 视为项目内的北京时间墙钟。"""
    if value is None:
        return default
    if value.tzinfo is not None:
        return value.astimezone(tz=_BEIJING).date()
    return value.date()


def _window(
    start_time: datetime | None,
    end_time: datetime | None,
    default_days: int,
) -> tuple[date, date]:
    end = _beijing_date(end_time, datetime.now().date())
    start = _beijing_date(start_time, end - timedelta(days=default_days))
    return (start, end) if start <= end else (end, start)


def _span_days(start: date, end: date) -> int:
    return max(1, (end - start).days + 1)


def _est_trading_days(calendar_days: int) -> int:
    """自然日 → 交易日的上界估算(x5/7), 用于行数预算。宁可高估(批次更小)。"""
    return max(1, math.ceil(calendar_days * 5 / 7))


def _to_float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _parse_date(value) -> date | None:
    """'YYYYMMDD' / 'YYYY-MM-DD' → date。"""
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _iso_date(value) -> str | None:
    d = _parse_date(value)
    return d.isoformat() if d else None


def _empty(schema: dict[str, pl.DataType]) -> pl.DataFrame:
    return pl.DataFrame(schema=schema)


# ---------------------------------------------------------------- 行 → 标准帧

def _daily_frame(rows: list[dict], symbols: set[str], start: date, end: date) -> pl.DataFrame:
    """Tushare 日K行 → 内部标准日K帧(单位换算 + normalize_daily 收口 + 停牌过滤)。"""
    if not rows:
        return pl.DataFrame()
    df = pl.DataFrame(rows, infer_schema_length=None)
    missing = [c for c in _DAILY_REQUIRED if c not in df.columns]
    if missing:
        logger.warning("tushare 日K响应缺少列 %s(接口结构可能变化), 丢弃本轮 %d 行", missing, df.height)
        return pl.DataFrame()
    # 日期显式按 YYYYMMDD 解析: 接口给字符串, 直接 cast(pl.Date) 会静默变 null(实测),
    # 那会整批丢数据而不是报错。
    # 单位: amount 千元 → 元; vol 已是手(见模块 docstring 实测)
    df = df.with_columns(
        pl.col("trade_date").cast(pl.Utf8).str.replace_all("-", "").str.to_date("%Y%m%d", strict=False),
        pl.col("amount").cast(pl.Float64, strict=False) * 1000.0,
        pl.col("vol").cast(pl.Float64, strict=False),
    )
    df = df.filter(
        pl.col("trade_date").is_not_null()
        & pl.col("ts_code").cast(pl.Utf8).is_in(sorted(symbols))
    )
    # normalize_daily: ts_code→symbol / trade_date→date / vol→volume / 类型收口 / 停牌过滤
    out = normalize_daily(df)
    if out.is_empty():
        return out
    return out.filter(
        pl.col("date").is_between(pl.lit(start), pl.lit(end), closed="both")
    ).sort("symbol", "date")


def _minute_frame(rows: list[dict], symbols: set[str], start: datetime, end: datetime) -> pl.DataFrame:
    """Tushare 分钟行 → canonical 8 列(datetime 为北京墙钟 naive)。"""
    if not rows:
        return pl.DataFrame()
    df = pl.DataFrame(rows, infer_schema_length=None)
    missing = [c for c in _MINUTE_REQUIRED if c not in df.columns]
    if missing:
        logger.warning("tushare 分钟响应缺少列 %s(接口结构可能变化), 丢弃本轮 %d 行", missing, df.height)
        return pl.DataFrame()
    df = df.with_columns(
        pl.col("trade_time").cast(pl.Utf8).str.to_datetime("%Y-%m-%d %H:%M:%S", strict=False),
        # 单位: 分钟 vol 为股 → 手(除以 100); amount 已是元, 原样
        pl.col("vol").cast(pl.Float64, strict=False).truediv(100.0).alias("volume"),
        pl.col("amount").cast(pl.Float64, strict=False),
    )
    for col in ("open", "high", "low", "close"):
        df = df.with_columns(pl.col(col).cast(pl.Float64, strict=False))
    df = df.rename({"ts_code": "symbol", "trade_time": "datetime"})
    df = df.filter(
        pl.col("symbol").cast(pl.Utf8).is_in(sorted(symbols))
        & pl.col("datetime").is_not_null()
    )
    if df.is_empty():
        return df
    df = df.filter(pl.col("datetime").is_between(pl.lit(start), pl.lit(end), closed="both"))
    keep = [c for c in _MINUTE_COLS_ORDER if c in df.columns]
    return df.select(keep).sort("symbol", "datetime")


def _adj_events(rows: list[dict], start: date, end: date) -> list[dict]:
    """累积复权因子行 → 单事件比值(仅保留窗口内事件, 并剔除修订抖动)。"""
    series: dict[str, list[tuple[date, float]]] = {}
    for row in rows:
        symbol = str(row.get("ts_code") or "").strip()
        trade_date = _parse_date(row.get("trade_date"))
        factor = _to_float(row.get("adj_factor"))
        if not symbol or trade_date is None or factor is None or factor <= 0:
            continue
        series.setdefault(symbol, []).append((trade_date, factor))
    if rows and not series:
        logger.warning("tushare 复权因子响应无法识别任何 ts_code/trade_date/adj_factor(接口结构可能变化)")
        return []

    low, high = _ADJ_FACTOR_RANGE
    events: list[dict] = []
    for symbol, seq in series.items():
        seq.sort(key=lambda item: item[0])
        for (_, prev_factor), (cur_date, factor) in pairwise(seq):
            ratio = factor / prev_factor
            if abs(ratio - 1.0) < _ADJ_EVENT_MIN_REL_DIFF:
                continue  # 因子修订抖动, 非除权事件
            if not low <= ratio <= high:
                logger.warning(
                    "tushare 除权因子: %s %s 单事件比值 %.4f 超出合理区间, 跳过",
                    symbol, cur_date, ratio,
                )
                continue
            if start <= cur_date <= end:
                events.append({"symbol": symbol, "trade_date": cur_date, "ex_factor": ratio})
    return events


def _financial_frame(rows: list[dict], table: str) -> pl.DataFrame:
    """Tushare 财务行 → 内部财务表帧(symbol/period_end/announce_date + 映射后的列)。"""
    if not rows:
        return pl.DataFrame()
    df = pl.DataFrame(rows, infer_schema_length=None)
    missing = [c for c in _FINANCIAL_REQUIRED if c not in df.columns]
    if missing:
        logger.warning(
            "tushare 财务表 %s 响应缺少列 %s(接口结构可能变化), 丢弃本轮 %d 行",
            table, missing, df.height,
        )
        return pl.DataFrame()
    # 只保留响应里真实存在的上游字段(不伪造补齐), 并按内部契约重命名
    pairs = [(up, inner) for up, inner in _FINANCIAL_FIELD_MAP.items() if up in df.columns]
    df = df.select([pl.col(up).alias(inner) for up, inner in pairs])
    # 报告期/公告日是 YYYYMMDD 字符串(偶有短横格式): 直接 cast(pl.Date) 会静默变 null,
    # 必须按格式解析(与日K同一坑)。
    for col in ("period_end", "announce_date"):
        if col in df.columns:
            df = df.with_columns(
                pl.col(col)
                .cast(pl.Utf8)
                .str.replace_all("-", "")
                .str.to_date("%Y%m%d", strict=False)
            )
    numeric = [c for c in df.columns if c not in _FINANCIAL_KEYS]
    # 金额/指标统一收口 Float64: 接口对不同表返回 int/float/字符串不一, 不统一会让
    # parquet 落盘后的下游 join 出现类型冲突。
    df = df.with_columns([pl.col(c).cast(pl.Float64, strict=False) for c in numeric])
    df = df.filter(pl.col("period_end").is_not_null() & pl.col("symbol").is_not_null())
    if df.is_empty():
        return df
    # 契约: (symbol, period_end) 唯一。实测各接口对同一报告期会返回多行(含完全重复行),
    # 同期有多个公告日时保留最新公告的那行(重述/准则调整以新披露为准)。
    if "announce_date" in df.columns:
        df = df.sort(["symbol", "period_end", "announce_date"], nulls_last=False)
    df = df.unique(subset=["symbol", "period_end"], keep="last")
    head = [c for c in _FINANCIAL_KEYS if c in df.columns]
    return df.select(head + [c for c in df.columns if c not in _FINANCIAL_KEYS]).sort(
        ["symbol", "period_end"]
    )


class TushareProvider:
    """Tushare Pro 数据源(日K / 除权因子 / 分钟K / 财务四表 / 标的维表)。"""

    name = "tushare"
    builtin = True

    def __init__(self) -> None:
        self.config = _TushareConfig()
        self._client: TushareClient | None = None

    def close(self) -> None:  # loader.load_all 重建注册表时会对每个 provider 调 close
        if self._client is not None:
            with contextlib.suppress(Exception):
                self._client.close()
            self._client = None

    def _get_client(self) -> TushareClient:
        if self._client is None:
            self._client = tushare_client.TushareClient(token=get_api_key())
        return self._client

    # ---- 标的维表 ----

    def get_instruments(self, asset_type: str = "stock") -> list[dict]:
        """A 股标的维表 → instrument_sync 期待的 TickFlow Instrument 形状。

        股本取最新交易日 ``daily_basic`` 全市场快照(2 次请求), 单位由万股换算为股
        (换手率降级路径依赖该口径); 取不到时置 None, 不伪造。
        ETF/指数维表不由本 provider 提供(该项目仍走 TickFlow)。
        """
        if asset_type != "stock":
            logger.info("tushare 维表只覆盖 A 股股票, %s 维表仍走 TickFlow", asset_type)
            return []
        client = self._get_client()
        try:
            rows = client.query(
                "stock_basic",
                {"list_status": "L"},
                "ts_code,name,exchange,market,list_date",
            )
        except TushareError as e:
            logger.warning("tushare stock_basic 失败: %s", e)
            return []
        if not rows:
            logger.warning("tushare stock_basic 返回空, 维表本轮不更新")
            return []

        shares = self._latest_shares()
        out: list[dict] = []
        for row in rows:
            ts_code = str(row.get("ts_code") or "").strip()
            if not ts_code:
                continue
            total, float_shares = shares.get(ts_code, (None, None))
            out.append({
                "symbol": ts_code,
                "name": row.get("name") or ts_code,
                "code": ts_code.split(".")[0],
                "exchange": _EXCHANGE_MAP.get(str(row.get("exchange") or "").upper(),
                                               str(row.get("exchange") or "")),
                "region": "CN",
                "type": "stock",
                "ext": {
                    "listing_date": _iso_date(row.get("list_date")),
                    "total_shares": total,
                    "float_shares": float_shares,
                    "tick_size": None,
                    "limit_up": None,
                    "limit_down": None,
                },
            })
        if shares:
            logger.info("tushare 维表: %d 只股票, 其中 %d 只取到股本", len(out), len(shares))
        else:
            logger.info("tushare 维表: %d 只股票(未取到股本, 换手率走降级路径)", len(out))
        return out

    def _latest_shares(self) -> dict[str, tuple[float | None, float | None]]:
        """最新交易日全市场股本(股)。失败返回空 dict(降级, 不阻断维表同步)。"""
        client = self._get_client()
        today = datetime.now().date()
        try:
            cal = client.query(
                "trade_cal",
                {
                    "exchange": "SSE",
                    "start_date": (today - timedelta(days=_SHARES_LOOKBACK_DAYS)).strftime("%Y%m%d"),
                    "end_date": today.strftime("%Y%m%d"),
                },
                "cal_date,is_open",
            )
            open_days = sorted(
                d for d in (_parse_date(r.get("cal_date")) for r in cal if str(r.get("is_open")) == "1")
                if d is not None
            )
            if not open_days:
                return {}
            rows = client.query(
                "daily_basic",
                {"trade_date": open_days[-1].strftime("%Y%m%d")},
                "ts_code,total_share,float_share",
            )
        except TushareError as e:
            logger.info("tushare 股本快照(daily_basic)不可用, 维表股本置空: %s", e)
            return {}
        out: dict[str, tuple[float | None, float | None]] = {}
        for row in rows:
            ts_code = str(row.get("ts_code") or "").strip()
            if not ts_code:
                continue
            total = _to_float(row.get("total_share"))
            float_share = _to_float(row.get("float_share"))
            # daily_basic 单位为万股 → 股(维表契约: float_shares 以股计, 见 pipeline 换手率公式)
            out[ts_code] = (
                total * 10_000 if total is not None else None,
                float_share * 10_000 if float_share is not None else None,
            )
        return out

    # ---- 日K ----

    def get_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        """日K(不复权原始价)。iter_daily 为优先路径, 本方法供未实现迭代的调用方使用。"""
        frames = [
            df for df in self.iter_daily(
                symbols, start_time=start_time, end_time=end_time,
                asset_type=asset_type, on_chunk_done=on_chunk_done,
            )
            if not df.is_empty()
        ]
        if not frames:
            return pl.DataFrame()
        return frames[0] if len(frames) == 1 else pl.concat(frames, how="diagonal_relaxed")

    def iter_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> Iterator[pl.DataFrame]:
        """有界分批产出日K(每批 ≤ 一个接口请求), 供全市场历史同步流式消费。

        批次标的数按行数预算: 一屏窗口内 单标的行数 ≈ 交易日数, 接口单次上限 6000 行。
        单批失败只跳过该批(on_chunk_done 仍覆盖), 不中断整个同步。
        """
        api = _DAILY_API.get(asset_type)
        if api is None:
            logger.warning("tushare 不支持资产类型 %s 的日K", asset_type)
            return
        if not symbols:
            return

        start, end = _window(start_time, end_time, _DAILY_DEFAULT_DAYS)
        span = _span_days(start, end)
        codes_per_req = _clamp(int(_DAILY_ROW_LIMIT * _ROW_HEADROOM) // span, 1, _DAILY_CODES_CAP)
        chunks = _chunked(list(symbols), codes_per_req)
        symset = set(symbols)
        client = self._get_client()
        params_base = {
            "start_date": start.strftime("%Y%m%d"),
            "end_date": end.strftime("%Y%m%d"),
        }
        failed = 0
        for index, chunk in enumerate(chunks):
            df = pl.DataFrame()
            try:
                rows = client.query(
                    api, {"ts_code": ",".join(chunk), **params_base}, _DAILY_FIELDS
                )
                df = _daily_frame(rows, symset, start, end)
            except TushareError as e:
                failed += 1
                logger.warning("tushare 日K批次 %d/%d 失败: %s", index + 1, len(chunks), e)
            yield df
            if on_chunk_done:
                on_chunk_done(index + 1, len(chunks))
        if failed:
            logger.warning(
                "tushare 日K同步: %d/%d 批次失败, 这些标的本轮无数据(将保持旧值)",
                failed, len(chunks),
            )

    # ---- 除权因子 ----

    def get_adj_factors(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        """单事件除权因子(symbol/trade_date/ex_factor)。指数无复权因子, 返回空帧。"""
        api = _ADJ_API.get(asset_type)
        if not symbols or api is None:
            if api is None and symbols:
                logger.info("tushare 无 %s 复权因子接口(指数不除权)", asset_type)
            return _empty({"symbol": pl.String, "trade_date": pl.Date, "ex_factor": pl.Float64})

        start, end = _window(start_time, end_time, _ADJ_DEFAULT_DAYS)
        # 向前多取一段: 比值需要前一交易日的累积因子, 否则窗口首个除权日会被漏掉
        fetch_start = start - timedelta(days=_ADJ_LOOKBACK_DAYS)
        span = _span_days(fetch_start, end)
        codes_per_req = _clamp(int(_DAILY_ROW_LIMIT * _ROW_HEADROOM) // span, 1, _DAILY_CODES_CAP)
        chunks = _chunked(list(symbols), codes_per_req)
        client = self._get_client()
        params_base = {
            "start_date": fetch_start.strftime("%Y%m%d"),
            "end_date": end.strftime("%Y%m%d"),
        }
        events: list[dict] = []
        failed = 0
        for index, chunk in enumerate(chunks):
            try:
                rows = client.query(
                    api, {"ts_code": ",".join(chunk), **params_base}, _ADJ_FIELDS
                )
                events.extend(_adj_events(rows, start, end))
            except TushareError as e:
                failed += 1
                logger.warning("tushare 除权因子批次 %d/%d 失败: %s", index + 1, len(chunks), e)
            if on_chunk_done:
                on_chunk_done(index + 1, len(chunks))
        if failed:
            logger.warning(
                "tushare 除权因子: %d/%d 批次失败, 这些标的将保持旧复权价",
                failed, len(chunks),
            )
        if not events:
            return _empty({"symbol": pl.String, "trade_date": pl.Date, "ex_factor": pl.Float64})
        return (
            pl.DataFrame(events, schema={"symbol": pl.String, "trade_date": pl.Date, "ex_factor": pl.Float64})
            .unique(subset=["symbol", "trade_date"], keep="last")
            .sort(["symbol", "trade_date"])
        )

    # ---- 分钟K ----

    def get_minute(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        freq: str = "1m",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        """分钟K(datetime 为北京墙钟 naive; volume 手, amount 元)。

        ``stk_mins`` 按标的分批且单次上限 8000 行, 故请求按「标的数 x 窗口交易日数」
        的行预算切分: 先定每批标的数, 再按剩余预算切时间窗。
        """
        tfreq = _FREQ_MAP.get(str(freq or "").lower())
        if tfreq is None:
            logger.warning("tushare 不支持分钟周期 %r (支持 1m/5m/15m/30m/60m)", freq)
            return pl.DataFrame()
        if not symbols:
            return pl.DataFrame()

        start_d, end_d = _window(start_time, end_time, _MINUTE_DEFAULT_DAYS)
        start_dt = datetime.combine(start_d, datetime.min.time())
        end_dt = datetime.combine(end_d, datetime.max.time())
        bars_per_day = _FREQ_BARS_PER_DAY[tfreq]
        row_budget = int(_MINUTE_ROW_LIMIT * _ROW_HEADROOM)

        trading_days = _est_trading_days(_span_days(start_d, end_d))
        codes_per_req = _clamp(row_budget // (bars_per_day * trading_days), 1, _MINUTE_CODES_CAP)
        codes_per_req = min(codes_per_req, len(symbols))
        # 每请求可覆盖的交易日数(溢出的部分用时间窗分段兜住, 防静默截断)
        days_per_req = max(1, row_budget // (bars_per_day * codes_per_req))
        segments = _time_segments(start_dt, end_dt, days_per_req)
        chunks = _chunked(list(symbols), codes_per_req)
        total = len(segments) * len(chunks)

        client = self._get_client()
        symset = set(symbols)
        frames: list[pl.DataFrame] = []
        failed = 0
        step = 0
        for seg_start, seg_end in segments:
            params_seg = {
                "freq": tfreq,
                "start_date": seg_start.strftime("%Y-%m-%d %H:%M:%S"),
                "end_date": seg_end.strftime("%Y-%m-%d %H:%M:%S"),
            }
            for chunk in chunks:
                step += 1
                try:
                    rows = client.query(
                        "stk_mins", {"ts_code": ",".join(chunk), **params_seg}, _MINUTE_FIELDS
                    )
                    df = _minute_frame(rows, symset, start_dt, end_dt)
                    if not df.is_empty():
                        frames.append(df)
                except TushareError as e:
                    failed += 1
                    logger.warning("tushare 分钟K %d/%d 失败: %s", step, total, e)
                if on_chunk_done:
                    on_chunk_done(step, total)
        if failed:
            logger.warning("tushare 分钟K: %d/%d 请求失败, 本轮结果可能不完整", failed, total)
        if not frames:
            return pl.DataFrame()
        merged = pl.concat(frames, how="vertical") if len(frames) > 1 else frames[0]
        return merged.unique(subset=["symbol", "datetime"], keep="last").sort("symbol", "datetime")

    # ---- 财务四表 ----

    def get_financials(
        self,
        table: str,
        symbols: list[str],
        latest_only: bool = False,
    ) -> pl.DataFrame:
        """财务表(symbol / period_end / announce_date + 该表的映射列)。

        Tushare 财务接口**不支持多标的**(逗号串静默 0 行), 只能逐只请求; 单只失败只跳过
        这一只(其余标的结果照常返回), 与日K/分钟K 的分批软失败同语义。

        ``latest_only`` 只作提示, 不影响本实现: 上游 ``period``/报告期过滤参数在高积分档
        之外不可靠(实测本 key 无权限), 故一律返回完整报告期, 由 ``financial_sync`` 的
        合并逻辑按 (symbol, period_end) 保留最新公告行。``shares`` 表不接入(见模块常量注释)。
        """
        api = _FINANCIAL_API.get(table)
        if api is None:
            logger.info(
                "tushare 财务: 表 %s 未接入(仅 %s), 该表本轮跳过",
                table, "/".join(_FINANCIAL_API),
            )
            return pl.DataFrame()
        if not symbols:
            return pl.DataFrame()

        client = self._get_client()
        frames: list[pl.DataFrame] = []
        failed = 0
        for symbol in symbols:
            try:
                rows = client.query(api, {"ts_code": symbol}, _FINANCIAL_FIELDS)
                df = _financial_frame(rows, table)
                if not df.is_empty():
                    frames.append(df)
            except TushareError as e:
                failed += 1
                if failed <= _FINANCIAL_WARN_LIMIT:
                    logger.warning("tushare 财务表 %s 标的 %s 失败: %s", table, symbol, e)
        if failed:
            logger.warning(
                "tushare 财务表 %s: %d/%d 只标的失败(其余已归集), 失败标的保持旧值",
                table, failed, len(symbols),
            )
        if not frames:
            return pl.DataFrame()
        merged = pl.concat(frames, how="diagonal_relaxed") if len(frames) > 1 else frames[0]
        if "announce_date" in merged.columns:
            merged = merged.sort(["symbol", "period_end", "announce_date"], nulls_last=False)
        return merged.unique(subset=["symbol", "period_end"], keep="last").sort(
            ["symbol", "period_end"]
        )

    # ---- 设置页试拉 ----

    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        if dataset not in _DATASETS:
            return {
                "provider": self.name,
                "dataset": dataset,
                "rows": 0,
                "error": f"Tushare 插件未接入 {dataset} 数据集(自动回退 TickFlow)",
            }
        syms = [s for s in (symbols or [])][:2] or ["600519.SH"]
        now = datetime.now()
        try:
            # 先直连预检一次: 取数方法对上游错误是软失败(空帧 + 日志), 而设置页必须
            # 看到具体原因(积分不足 / 无接口权限 / Key 无效)。
            self._preflight(dataset, syms[0])
            if dataset == "daily":
                df = self.get_daily(syms, now - timedelta(days=30), now)
            elif dataset == "adj_factor":
                df = self.get_adj_factors(syms, now - timedelta(days=_ADJ_DEFAULT_DAYS), now)
            elif dataset == "financial":
                # 财务四表按标的逐只请求, 试拉用 metrics(字段最全)最能暴露权限/积分问题
                df = self.get_financials("metrics", syms)
            else:
                df = self.get_minute(syms, now - timedelta(days=_MINUTE_DEFAULT_DAYS), now)
        except TushareError as e:
            return {"provider": self.name, "dataset": dataset, "rows": 0, "error": str(e)}
        head = df.head(5).to_dicts()
        for row in head:  # date/datetime → ISO 字符串, 保证 JSON 可序列化
            for key, value in list(row.items()):
                if isinstance(value, (date, datetime)):
                    row[key] = value.isoformat()
        return {
            "provider": self.name,
            "dataset": dataset,
            "rows": df.height,
            "columns": df.columns,
            "preview": head,
        }

    def _preflight(self, dataset: str, symbol: str) -> None:
        """试拉前的最小请求, 让上游错误(积分/权限/Key)以异常形式暴露给设置页。"""
        now = datetime.now()
        if dataset == "minute":
            params = {
                "ts_code": symbol,
                "freq": "1min",
                "start_date": (now - timedelta(days=_MINUTE_DEFAULT_DAYS)).strftime("%Y-%m-%d %H:%M:%S"),
                "end_date": now.strftime("%Y-%m-%d %H:%M:%S"),
            }
            api = "stk_mins"
            fields = "ts_code"
        elif dataset == "financial":
            api = _FINANCIAL_API["metrics"]
            params = {"ts_code": symbol}
            fields = "ts_code,end_date"
        else:
            api, days = ("daily", 30) if dataset == "daily" else ("adj_factor", _ADJ_DEFAULT_DAYS)
            params = {
                "ts_code": symbol,
                "start_date": (now - timedelta(days=days)).strftime("%Y%m%d"),
                "end_date": now.strftime("%Y%m%d"),
            }
            fields = "ts_code"
        self._get_client().query(api, params, fields)


def _time_segments(start: datetime, end: datetime, days_per_req: int) -> list[tuple[datetime, datetime]]:
    """把 [start, end] 切成每段约 days_per_req 个**交易日**的窗口(自然日按 x7/5 折算)。"""
    cal_days = max(1, math.ceil(days_per_req * 7 / 5))
    step = timedelta(days=cal_days)
    segments: list[tuple[datetime, datetime]] = []
    cursor = start
    while cursor < end:
        seg_end = min(cursor + step - timedelta(seconds=1), end)
        segments.append((cursor, seg_end))
        cursor = seg_end + timedelta(seconds=1)
    return segments or [(start, end)]
