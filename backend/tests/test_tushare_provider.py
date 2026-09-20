"""TushareProvider 契约与单位标准化测试。

不依赖真实网络与真实 Key: 用假 TushareClient 注入预置响应, 验证
字段映射与单位口径(amount 千元→元、分钟 vol 股→手、datetime 北京墙钟)、
累积复权因子→单事件比值换算与修订抖动剔除、分批预算(接口行数上限)、
软失败语义、能力声明、Key 语义(先探后存 / secrets.json 优先)与 loader 注册集成。

实测口径依据见 app/plugins/tushare/provider.py 模块 docstring。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import polars as pl
import pytest

from app.plugins.tushare import client as ts_client
from app.plugins.tushare import provider as tp
from app.plugins.tushare.provider import TushareProvider


class _FakeClient:
    """按 api_name 返回预置响应, 记录调用供分批/参数断言。"""

    def __init__(self, responses: dict | None = None, error: Exception | None = None):
        self.responses = responses or {}
        self.error = error
        self.calls: list[dict] = []

    def query(self, api_name: str, params: dict | None = None, fields: str = "") -> list[dict]:
        params = dict(params or {})
        self.calls.append({"api": api_name, "params": params, "fields": fields})
        if self.error is not None:
            raise self.error
        handler = self.responses.get(api_name)
        if handler is None:
            return []
        return list(handler(params)) if callable(handler) else list(handler)

    def close(self) -> None:
        pass


def _provider(monkeypatch, responses=None, error=None) -> tuple[TushareProvider, _FakeClient]:
    fake = _FakeClient(responses, error)
    monkeypatch.setattr(tp, "tushare_client", type("M", (), {"TushareClient": lambda **kw: fake}))
    monkeypatch.setattr(tp, "get_api_key", lambda: "test-key")
    return TushareProvider(), fake


def _daily_row(ts_code: str, trade_date: str, close: float = 1467.96, vol=49721.25, amount=7347477.479):
    """实测 daily 行结构(2025-09-18 600519.SH): amount 单位千元, vol 单位手。"""
    return {
        "ts_code": ts_code,
        "trade_date": trade_date,
        "open": 1492.0,
        "high": 1497.8,
        "low": 1463.5,
        "close": close,
        "vol": vol,
        "amount": amount,
    }


def _minute_row(ts_code: str, trade_time: str, vol: float, amount: float, close: float = 1259.01):
    """实测 stk_mins 行结构(600519.SH 2026-09-18): vol 单位股, amount 单位元。"""
    return {
        "ts_code": ts_code,
        "trade_time": trade_time,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "vol": vol,
        "amount": amount,
    }


# ---- 日K: 单位与字段映射 ----

def test_daily_units_amount_thousand_yuan_volume_stays_hand(monkeypatch):
    """核心口径: daily amount 千元 → 元; vol 已是手, 不得再换算。"""
    provider, _ = _provider(monkeypatch, {"daily": [_daily_row("600519.SH", "20250918")]})

    df = provider.get_daily(
        ["600519.SH"], datetime(2025, 9, 1), datetime(2025, 9, 30)
    )

    assert df.height == 1
    row = df.row(0, named=True)
    assert row["symbol"] == "600519.SH"
    assert row["date"] == date(2025, 9, 18)
    assert row["volume"] == pytest.approx(49721.25)  # 手
    assert row["amount"] == pytest.approx(7347477.479 * 1000.0)  # 千元 → 元
    assert row["close"] == pytest.approx(1467.96)
    # 权威历史批数据不带 quote_ts(data_integrity 靠 null 区分盘中快照)
    assert "quote_ts" not in df.columns


def test_daily_drops_halted_rows_and_out_of_window_rows(monkeypatch):
    rows = [
        _daily_row("600519.SH", "20250918"),
        # 停牌日: open/high 为 0 且量额为 0 → 必须由 filter_halt_days 剔除
        {**_daily_row("600519.SH", "20250919"), "open": 0.0, "high": 0.0, "vol": 0, "amount": 0},
        _daily_row("600519.SH", "20250801"),  # 窗口外
    ]
    provider, _ = _provider(monkeypatch, {"daily": rows})

    df = provider.get_daily(["600519.SH"], datetime(2025, 9, 1), datetime(2025, 9, 30))

    assert df["date"].to_list() == [date(2025, 9, 18)]


def test_daily_api_routed_by_asset_type(monkeypatch):
    provider, fake = _provider(monkeypatch, {"daily": [], "fund_daily": [], "index_daily": []})

    for asset_type, api in (("stock", "daily"), ("etf", "fund_daily"), ("index", "index_daily")):
        fake.calls.clear()
        provider.get_daily(["510300.SH"], datetime(2025, 9, 1), datetime(2025, 9, 18), asset_type)
        assert [c["api"] for c in fake.calls] == [api]


def test_daily_unknown_asset_type_makes_no_request(monkeypatch):
    provider, fake = _provider(monkeypatch, {"daily": []})

    df = provider.get_daily(["600519.SH"], None, None, asset_type="bond")

    assert df.is_empty()
    assert fake.calls == []


def test_daily_schema_change_yields_empty_not_broken_frame(monkeypatch, caplog):
    """接口结构变化(缺列)时整批丢弃并告警, 不得静默产出错列/空数据。"""
    provider, _ = _provider(monkeypatch, {
        "daily": [{"ts_code": "600519.SH", "trade_date": "20250918", "close": 1467.96}],
    })

    with caplog.at_level("WARNING"):
        df = provider.get_daily(["600519.SH"], datetime(2025, 9, 1), datetime(2025, 9, 30))

    assert df.is_empty()
    assert any("缺少列" in r.message for r in caplog.records)


def test_minute_schema_change_yields_empty_not_broken_frame(monkeypatch, caplog):
    provider, _ = _provider(monkeypatch, {
        "stk_mins": [{"ts_code": "600519.SH", "trade_time": "2026-09-18 09:35:00"}],
    })

    with caplog.at_level("WARNING"):
        df = provider.get_minute(
            ["600519.SH"], datetime(2026, 9, 18, 9, 0), datetime(2026, 9, 18, 15, 0)
        )

    assert df.is_empty()
    assert any("缺少列" in r.message for r in caplog.records)


# ---- 日K: 分批预算与软失败 ----

def test_iter_daily_batch_budget_respects_row_limit(monkeypatch):
    """一屏 250 自然日的窗口 → 单批标的数 ≈ 6000x0.95/250 = 22, 不得超限。"""
    provider, fake = _provider(monkeypatch, {"daily": []})
    symbols = [f"{600000 + i}.SH" for i in range(45)]

    list(provider.iter_daily(symbols, datetime(2025, 1, 1), datetime(2025, 9, 7)))

    requested = [len(c["params"]["ts_code"].split(",")) for c in fake.calls]
    assert requested == [22, 22, 1]
    # 所有请求都带同一窗口, 且标的无重复遗漏
    sent: list[str] = []
    for call in fake.calls:
        assert call["params"]["start_date"] == "20250101"
        assert call["params"]["end_date"] == "20250907"
        sent.extend(call["params"]["ts_code"].split(","))
    assert sorted(sent) == sorted(symbols)


def test_iter_daily_soft_fails_per_batch_and_callback_covers_all(monkeypatch):
    """单批失败只跳过该批: 不抛异常, 进度回调仍覆盖全部批次。"""
    provider, _ = _provider(monkeypatch, error=ts_client.TushareError("daily 返回错误 code=40203"))
    progress: list[tuple[int, int]] = []

    frames = list(provider.iter_daily(
        ["600519.SH", "000001.SZ"], datetime(2025, 9, 1), datetime(2025, 9, 18),
        on_chunk_done=lambda cur, total: progress.append((cur, total)),
    ))

    assert len(frames) == 1 and frames[0].is_empty()
    assert progress[-1][0] == progress[-1][1]  # 最终 cur == total


def test_get_daily_empty_symbols_makes_no_request(monkeypatch):
    provider, fake = _provider(monkeypatch, {"daily": []})

    assert provider.get_daily([], None, None).is_empty()
    assert fake.calls == []


# ---- 分钟K ----

def test_minute_units_and_beijing_wallclock(monkeypatch):
    """核心口径: stk_mins vol 股 → 手(÷100), amount 已是元; trade_time 为北京墙钟 naive。"""
    provider, _ = _provider(monkeypatch, {
        "stk_mins": [
            _minute_row("600519.SH", "2026-09-18 09:36:00", vol=31000, amount=39000000.0),
            _minute_row("600519.SH", "2026-09-18 09:35:00", vol=31700, amount=39915370.0),
        ]
    })

    df = provider.get_minute(
        ["600519.SH"], datetime(2026, 9, 18, 9, 0), datetime(2026, 9, 18, 15, 0)
    )

    assert df.columns == ["symbol", "datetime", "open", "high", "low", "close", "volume", "amount"]
    # 接口按时间倒序返回 → 必须正序入库
    assert df["datetime"].to_list() == [
        datetime(2026, 9, 18, 9, 35), datetime(2026, 9, 18, 9, 36),
    ]
    assert df.schema["datetime"] == pl.Datetime("us")
    first = df.row(0, named=True)
    assert first["volume"] == pytest.approx(317.0)  # 31700 股 → 317 手
    assert first["amount"] == pytest.approx(39915370.0)  # 元, 不换算


def test_minute_freq_mapping_and_unsupported_freq(monkeypatch):
    provider, fake = _provider(monkeypatch, {"stk_mins": []})

    provider.get_minute(
        ["600519.SH"], datetime(2026, 9, 18, 9, 0), datetime(2026, 9, 18, 15, 0), freq="5m"
    )
    assert fake.calls[-1]["params"]["freq"] == "5min"

    fake.calls.clear()
    df = provider.get_minute(
        ["600519.SH"], datetime(2026, 9, 18, 9, 0), datetime(2026, 9, 18, 15, 0), freq="7m"
    )
    assert df.is_empty()
    assert fake.calls == []


def test_minute_long_window_is_split_by_row_budget(monkeypatch):
    """1 只 x 1 年: 单请求最多覆盖 8000x0.95/241 ≈ 31 个交易日 → 必须按时窗分段。"""
    provider, fake = _provider(monkeypatch, {"stk_mins": []})

    provider.get_minute(["600519.SH"], datetime(2025, 9, 18), datetime(2026, 9, 18))

    assert len(fake.calls) > 1
    assert all(len(c["params"]["ts_code"].split(",")) == 1 for c in fake.calls)
    starts = [datetime.strptime(c["params"]["start_date"], "%Y-%m-%d %H:%M:%S") for c in fake.calls]
    ends = [datetime.strptime(c["params"]["end_date"], "%Y-%m-%d %H:%M:%S") for c in fake.calls]
    assert starts == sorted(starts)
    # 分段连续且覆盖整窗; 段长为 31 个交易日 = 44 自然日(x7/5), 不超过行数预算
    assert starts[0] == datetime(2025, 9, 18, 0, 0, 0)
    assert ends[-1] >= datetime(2026, 9, 17, 0, 0, 0)
    for start, end in zip(starts, ends, strict=True):
        assert (end - start).days <= 44


def test_minute_batch_symbols_shrink_when_window_is_wide(monkeypatch):
    """窗口越宽, 单批标的数越小(整窗行数预算控制), 否则会被上游静默截断。"""
    provider, fake = _provider(monkeypatch, {"stk_mins": []})
    symbols = [f"{600000 + i}.SH" for i in range(100)]

    provider.get_minute(symbols, datetime(2026, 9, 14), datetime(2026, 9, 18))

    per_request = {len(c["params"]["ts_code"].split(",")) for c in fake.calls}
    # 5 自然日 ≈ 4 交易日: 单批 8000x0.95/(241x4) = 7 只, 末批余数 2 只
    assert per_request == {7, 2}
    assert len(fake.calls) == 15
    sent: list[str] = []
    for call in fake.calls:
        sent.extend(call["params"]["ts_code"].split(","))
    assert sorted(sent) == sorted(symbols)


def test_minute_soft_fails_on_api_error(monkeypatch):
    provider, _ = _provider(monkeypatch, error=ts_client.TushareError("stk_mins 返回错误 code=40203"))
    progress: list[tuple[int, int]] = []

    df = provider.get_minute(
        ["600519.SH"], datetime(2026, 9, 18, 9, 0), datetime(2026, 9, 18, 15, 0),
        on_chunk_done=lambda cur, total: progress.append((cur, total)),
    )

    assert df.is_empty()
    assert progress == [(1, 1)]


# ---- 除权因子: 累积 → 单事件比值 ----

def test_adj_factor_cumulative_ratio_to_single_event(monkeypatch):
    """实测 600519 除权日 2024-06-19: 7.858 → 8.02, 比值 1.0206(与当日分红比例一致)。"""
    provider, fake = _provider(monkeypatch, {
        "adj_factor": [
            {"ts_code": "600519.SH", "trade_date": "20240618", "adj_factor": 7.858},
            {"ts_code": "600519.SH", "trade_date": "20240619", "adj_factor": 8.02},
            {"ts_code": "600519.SH", "trade_date": "20240620", "adj_factor": 8.02},
        ]
    })

    df = provider.get_adj_factors(["600519.SH"], datetime(2024, 6, 1), datetime(2024, 6, 30))

    assert df.columns == ["symbol", "trade_date", "ex_factor"]
    assert df.height == 1
    row = df.row(0, named=True)
    assert row["trade_date"] == date(2024, 6, 19)
    assert row["ex_factor"] == pytest.approx(8.02 / 7.858)
    # 取数窗口必须前推, 否则首个除权日缺少"前一交易日"因子
    assert fake.calls[0]["params"]["start_date"] == (date(2024, 6, 1) - timedelta(days=40)).strftime("%Y%m%d")


def test_adj_factor_filters_revision_jitter(monkeypatch):
    """实测抖动(8.02→8.021→8.02, 7.8576→7.858)比值偏差 ≤1.3e-4, 不是除权事件。"""
    provider, _ = _provider(monkeypatch, {
        "adj_factor": [
            # 同一标的的两次抖动: 跳升后次日回退、以及 4 位小数修订
            {"ts_code": "600519.SH", "trade_date": "20240624", "adj_factor": 8.02},
            {"ts_code": "600519.SH", "trade_date": "20240625", "adj_factor": 8.021},
            {"ts_code": "600519.SH", "trade_date": "20240626", "adj_factor": 8.02},
            {"ts_code": "000001.SZ", "trade_date": "20240514", "adj_factor": 7.8576},
            {"ts_code": "000001.SZ", "trade_date": "20240515", "adj_factor": 7.858},
        ]
    })

    df = provider.get_adj_factors(
        ["600519.SH", "000001.SZ"], datetime(2024, 5, 1), datetime(2024, 7, 31)
    )

    assert df.height == 0


def test_adj_factor_keeps_smallest_real_event(monkeypatch):
    """实测真实事件最小比值 1.000746 < 抖动阈值之上 → 必须保留, 不得被误杀。"""
    provider, _ = _provider(monkeypatch, {
        "adj_factor": [
            {"ts_code": "000001.SZ", "trade_date": "20250610", "adj_factor": 100.0},
            {"ts_code": "000001.SZ", "trade_date": "20250611", "adj_factor": 100.0746},
        ]
    })

    df = provider.get_adj_factors(["000001.SZ"], datetime(2025, 6, 1), datetime(2025, 6, 30))

    assert df.height == 1
    assert df.row(0, named=True)["ex_factor"] == pytest.approx(1.000746)


def test_adj_factor_excludes_events_outside_window(monkeypatch):
    provider, _ = _provider(monkeypatch, {
        "adj_factor": [
            {"ts_code": "600519.SH", "trade_date": "20240618", "adj_factor": 7.858},
            {"ts_code": "600519.SH", "trade_date": "20240619", "adj_factor": 8.02},
        ]
    })

    df = provider.get_adj_factors(["600519.SH"], datetime(2024, 7, 1), datetime(2024, 7, 31))

    assert df.height == 0


def test_adj_factor_rejects_implausible_ratio(monkeypatch):
    provider, _ = _provider(monkeypatch, {
        "adj_factor": [
            {"ts_code": "600519.SH", "trade_date": "20240618", "adj_factor": 0.1},
            {"ts_code": "600519.SH", "trade_date": "20240619", "adj_factor": 50.0},
        ]
    })

    df = provider.get_adj_factors(["600519.SH"], datetime(2024, 6, 1), datetime(2024, 6, 30))

    assert df.height == 0


def test_adj_factor_unrecognizable_payload_warns(monkeypatch, caplog):
    provider, _ = _provider(monkeypatch, {"adj_factor": [{"foo": "bar"}, {"baz": 1}]})

    with caplog.at_level("WARNING"):
        df = provider.get_adj_factors(["600519.SH"], None, None)

    assert df.height == 0
    assert any("接口结构可能变化" in r.message for r in caplog.records)


def test_adj_factor_index_has_no_api_and_no_request(monkeypatch):
    provider, fake = _provider(monkeypatch, {"adj_factor": []})

    df = provider.get_adj_factors(["000001.SH"], None, None, asset_type="index")

    assert df.height == 0
    assert df.columns == ["symbol", "trade_date", "ex_factor"]
    assert fake.calls == []


def test_adj_factor_etf_uses_fund_adj(monkeypatch):
    provider, fake = _provider(monkeypatch, {"fund_adj": []})

    provider.get_adj_factors(["510300.SH"], datetime(2025, 9, 1), datetime(2025, 9, 18), asset_type="etf")

    assert [c["api"] for c in fake.calls] == ["fund_adj"]


def test_adj_factor_soft_fails_on_api_error(monkeypatch):
    provider, _ = _provider(monkeypatch, error=ts_client.TushareError("adj_factor 无权限"))
    progress: list[tuple[int, int]] = []

    df = provider.get_adj_factors(
        ["600519.SH"], datetime(2024, 6, 1), datetime(2024, 6, 30),
        on_chunk_done=lambda cur, total: progress.append((cur, total)),
    )

    assert df.height == 0
    assert progress == [(1, 1)]


# ---- 标的维表 ----

def test_instruments_exchange_mapping_and_shares_units(monkeypatch):
    """exchange SSE/SZSE/BSE → SH/SZ/BJ; 股本 daily_basic 万股 → 股。"""
    def daily_basic(params):
        # 只有在取到最近交易日之后才应该被查到, 且日期必须是交易历里最后一个开市日
        assert params["trade_date"] == "20250918"
        return [{"ts_code": "600519.SH", "total_share": 125227.0215, "float_share": 125227.0215}]

    provider, _ = _provider(monkeypatch, {
        "stock_basic": [
            {"ts_code": "600519.SH", "name": "贵州茅台", "exchange": "SSE",
             "market": "主板", "list_date": "20010827"},
            {"ts_code": "000001.SZ", "name": "平安银行", "exchange": "SZSE",
             "market": "主板", "list_date": "19910403"},
            {"ts_code": "430047.BJ", "name": "诺思兰德", "exchange": "BSE",
             "market": "北交所", "list_date": "20201223"},
        ],
        "trade_cal": [
            {"cal_date": "20250917", "is_open": 1},
            {"cal_date": "20250918", "is_open": 1},
            {"cal_date": "20250920", "is_open": 0},
        ],
        "daily_basic": daily_basic,
    })

    rows = provider.get_instruments("stock")

    assert [r["exchange"] for r in rows] == ["SH", "SZ", "BJ"]
    assert rows[0]["symbol"] == "600519.SH"
    assert rows[0]["code"] == "600519"
    assert rows[0]["region"] == "CN"
    assert rows[0]["type"] == "stock"
    assert rows[0]["ext"]["listing_date"] == "2001-08-27"
    assert rows[0]["ext"]["float_shares"] == pytest.approx(125227.0215 * 10_000)
    assert rows[1]["ext"]["float_shares"] is None  # 未覆盖的标的置 None, 不伪造


def test_instruments_degrades_when_shares_snapshot_fails(monkeypatch):
    provider, _ = _provider(monkeypatch, {
        "stock_basic": [{"ts_code": "600519.SH", "name": "贵州茅台", "exchange": "SSE",
                         "market": "主板", "list_date": "20010827"}],
    })

    rows = provider.get_instruments("stock")

    assert len(rows) == 1
    assert rows[0]["ext"]["total_shares"] is None


def test_instruments_etf_delegates_to_tickflow(monkeypatch):
    provider, fake = _provider(monkeypatch, {"stock_basic": []})

    assert provider.get_instruments("etf") == []
    assert fake.calls == []


def test_instruments_soft_fails_on_api_error(monkeypatch):
    provider, _ = _provider(monkeypatch, error=ts_client.TushareError("stock_basic 无权限"))

    assert provider.get_instruments("stock") == []


# ---- 能力声明与 Key 语义 ----

def test_declared_datasets_exclude_unsupported(monkeypatch):
    provider, _ = _provider(monkeypatch)

    assert set(provider.config.datasets) == {"daily", "adj_factor", "minute"}
    for dataset in ("realtime", "depth5", "financial", "full_minute"):
        assert dataset not in provider.config.datasets
    assert provider.name == "tushare"
    assert provider.builtin is True


def test_availability_reports_missing_key(monkeypatch):
    monkeypatch.setattr(tp, "get_api_key", lambda: "")

    ok, reason = tp.availability()

    assert ok is False
    assert tp.API_KEY_ENV in reason


def test_api_key_priority_secrets_over_env(monkeypatch):
    monkeypatch.setenv(tp.API_KEY_ENV, "key-from-env")
    monkeypatch.setattr("app.secrets_store.load", lambda: {})
    assert tp.get_api_key() == "key-from-env"

    monkeypatch.setattr("app.secrets_store.load", lambda: {tp.SECRETS_FIELD: "key-from-file"})
    assert tp.get_api_key() == "key-from-file"


def test_probe_api_key_reports_invalid_candidate(monkeypatch):
    class _Boom:
        def __init__(self, **kw):
            pass

        def query(self, *a, **kw):
            raise ts_client.TushareError("无接口访问权限")

        def close(self):
            pass

    monkeypatch.setattr(tp.tushare_client, "TushareClient", lambda **kw: _Boom(**kw))

    ok, reason = tp.probe_api_key("bad-key")

    assert ok is False
    assert "无效" in reason


def test_probe_api_key_accepts_valid_candidate(monkeypatch):
    provider, _ = _provider(monkeypatch)
    calls: list[str] = []

    class _Ok:
        def __init__(self, **kw):
            pass

        def query(self, api_name, params=None, fields=""):
            calls.append(api_name)
            return [{"cal_date": "20250918", "is_open": 1}]

        def close(self):
            pass

    monkeypatch.setattr(tp.tushare_client, "TushareClient", lambda **kw: _Ok(**kw))

    ok, reason = tp.probe_api_key("good-key")

    assert (ok, reason) == (True, "ok")
    assert calls == ["trade_cal"]
    assert provider is not None


# ---- 设置页试拉 ----

def test_test_dataset_rejects_undeclared_dataset(monkeypatch):
    provider, _ = _provider(monkeypatch)

    out = provider.test_dataset("financial")

    assert out["rows"] == 0
    assert "financial" in out["error"]


def test_test_dataset_preview_is_json_serializable(monkeypatch):
    recent = (datetime.now() - timedelta(days=5)).strftime("%Y%m%d")
    provider, _ = _provider(monkeypatch, {
        "daily": [_daily_row("600519.SH", recent)],
    })

    out = provider.test_dataset("daily", ["600519.SH"])

    assert out["dataset"] == "daily"
    assert out["rows"] == 1
    assert out["preview"][0]["date"] == datetime.strptime(recent, "%Y%m%d").date().isoformat()


def test_test_dataset_surfaces_api_error(monkeypatch):
    provider, _ = _provider(monkeypatch, error=ts_client.TushareError("daily 返回错误 code=40203"))

    out = provider.test_dataset("daily", ["600519.SH"])

    assert out["rows"] == 0
    assert "40203" in out["error"]


# ---- 客户端协议 ----

def test_client_maps_fields_to_rows_and_raises_on_business_error():
    import httpx

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["payload"] = __import__("json").loads(request.content.decode())
        return httpx.Response(200, json={
            "code": 0,
            "msg": None,
            "data": {"fields": ["ts_code", "close"], "items": [["600519.SH", 1467.96]]},
        })

    client = ts_client.TushareClient(
        token="secret", client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    rows = client.query("daily", {"ts_code": "600519.SH"}, "ts_code,close")

    assert rows == [{"ts_code": "600519.SH", "close": 1467.96}]
    assert captured["payload"]["api_name"] == "daily"
    assert captured["payload"]["token"] == "secret"
    client.close()


def test_client_raises_on_non_zero_code_without_leaking_token():
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 40203, "msg": "无接口访问权限", "data": None})

    client = ts_client.TushareClient(
        token="secret-token-value", client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    with pytest.raises(ts_client.TushareError) as excinfo:
        client.query("stk_mins", {}, "")

    assert "40203" in str(excinfo.value)
    assert "secret-token-value" not in str(excinfo.value)
    client.close()


def test_client_returns_empty_on_empty_payload():
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "msg": None, "data": {"fields": [], "items": []}})

    client = ts_client.TushareClient(client=httpx.Client(transport=httpx.MockTransport(handler)))

    assert client.query("daily", {}, "") == []
    client.close()


# ---- loader 集成 ----

def test_loader_registers_plugin_and_capabilities(monkeypatch):
    from app.data_providers import custom as custom_sources

    monkeypatch.setenv(tp.API_KEY_ENV, "test-key")
    monkeypatch.setattr("app.secrets_store.load", lambda: {})
    custom_sources.load_all()

    assert "tushare" in custom_sources.names()
    assert custom_sources.provider_has_dataset("tushare", "daily")
    assert custom_sources.provider_has_dataset("tushare", "adj_factor")
    assert custom_sources.provider_has_dataset("tushare", "minute")
    for dataset in ("realtime", "depth5", "financial", "full_minute"):
        assert not custom_sources.provider_has_dataset("tushare", dataset)

    status = {p["name"]: p for p in custom_sources.list_plugins()}["tushare"]
    assert status["available"] is True
    assert status["api_key_env"] == tp.API_KEY_ENV
    assert set(status["datasets"]) == {"daily", "adj_factor", "minute"}


def test_loader_marks_plugin_unavailable_without_key(monkeypatch):
    from app.data_providers import custom as custom_sources

    monkeypatch.delenv(tp.API_KEY_ENV, raising=False)
    monkeypatch.setattr("app.secrets_store.load", lambda: {})
    custom_sources.load_all()

    status = {p["name"]: p for p in custom_sources.list_plugins()}["tushare"]
    assert status["available"] is False
    assert "tushare" not in custom_sources.names()
