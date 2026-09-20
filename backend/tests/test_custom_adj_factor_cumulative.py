"""自定义源 adj_factor_mode=cumulative(累积因子 → 单事件比值)契约测试。

口径红线: 内部 `ex_factor` 是「每次除权事件 前收盘价/除权参考价 比值(非累积)」,
累积链由 indicators pipeline 自建。上游给累积因子时若不换算, 前复权价会静默算错。
换算需要「前一交易日」的因子, 故取数窗口必须向前多取一段回看, 换算后再裁回请求窗口。
"""

from __future__ import annotations

import json
from datetime import date, datetime

import httpx
import polars as pl
import pytest

from app.data_providers.custom.config import config_from_dict
from app.data_providers.custom.provider import GenericHTTPProvider
from app.data_providers.normalizer import cumulative_adj_factors_to_events

_ADJ_FIELD_MAP = {"ts_code": "symbol", "trade_date": "trade_date", "adj_factor": "ex_factor"}

# 实测 600519 累积因子(2024-06-19 除权: 7.858 → 8.02, 比值 1.0206)
_600519_SERIES = [
    ("20240617", 7.858),
    ("20240618", 7.858),
    ("20240619", 8.02),
    ("20240620", 8.02),
    ("20240621", 8.02),
]


def _payload(series: list[tuple[str, float]], code: str = "600519.SH") -> dict:
    return {
        "code": 0,
        "msg": "",
        "data": {
            "fields": ["ts_code", "trade_date", "adj_factor"],
            "items": [[code, day, factor] for day, factor in series],
            "has_more": False,
        },
    }


def _provider(monkeypatch, series_list: list[list[tuple[str, float]]], mode: str = "cumulative"):
    monkeypatch.setenv("TUSHARE_API_KEY", "test-key")
    calls: list[dict] = []
    frames = iter(series_list)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        calls.append({"body": body})
        return httpx.Response(200, json=_payload(next(frames)))

    provider = GenericHTTPProvider(config_from_dict({
        "name": "adj_cumulative",
        "auth": {"type": "body", "param": "token", "token_env": "TUSHARE_API_KEY"},
        "datasets": {
            "adj_factor": {
                "url": "http://upstream.local",
                "method": "POST",
                "batch": 5,
                "response_path": "data",
                "adj_factor_mode": mode,
                "body": {
                    "api_name": "adj_factor",
                    "params": {
                        "ts_code": "${symbols}",
                        "start_date": "${start:%Y%m%d}",
                        "end_date": "${end:%Y%m%d}",
                    },
                },
                "field_map": dict(_ADJ_FIELD_MAP),
                "transforms": {"trade_date": "parse_date(value, '%Y%m%d')"},
            }
        },
    }))
    provider._client = httpx.Client(transport=httpx.MockTransport(handler))
    return provider, calls


# ---- 共享换算函数 ----


def _frame(series: list[tuple[str, float]], code: str = "600519.SH") -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": [code] * len(series),
        "trade_date": [date(int(d[:4]), int(d[4:6]), int(d[6:])) for d, _ in series],
        "ex_factor": [float(f) for _, f in series],
    })


def test_cumulative_ratio_matches_real_ex_date():
    out = cumulative_adj_factors_to_events(_frame(_600519_SERIES))

    assert out.height == 1
    row = out.row(0, named=True)
    assert row["trade_date"] == date(2024, 6, 19)
    assert row["ex_factor"] == pytest.approx(8.02 / 7.858)


def test_cumulative_ratio_is_multiplicative_and_order_independent():
    """累积乘积必须还原上游累积序列(前复权口径与上游一致)。"""
    out = cumulative_adj_factors_to_events(_frame(_600519_SERIES))

    assert out["ex_factor"].product() == pytest.approx(8.02 / 7.858)


def test_cumulative_filters_revision_jitter():
    series = [("20240624", 8.02), ("20240625", 8.021), ("20240626", 8.02)]

    assert cumulative_adj_factors_to_events(_frame(series)).height == 0


def test_cumulative_keeps_smallest_real_event():
    series = [("20250610", 100.0), ("20250611", 100.0746)]

    out = cumulative_adj_factors_to_events(_frame(series))

    assert out.height == 1
    assert out.row(0, named=True)["ex_factor"] == pytest.approx(1.000746)


def test_cumulative_drops_first_row_without_previous_factor():
    out = cumulative_adj_factors_to_events(_frame([("20240619", 8.02)]))

    assert out.height == 0  # 无前值 → 无法判定, 不猜


def test_cumulative_trims_to_requested_window():
    out = cumulative_adj_factors_to_events(
        _frame(_600519_SERIES), start=date(2024, 6, 20), end=date(2024, 6, 30)
    )

    assert out.height == 0  # 事件在窗口之前


def test_cumulative_rejects_implausible_ratio(caplog):
    with caplog.at_level("WARNING"):
        out = cumulative_adj_factors_to_events(_frame([("20240618", 0.1), ("20240619", 50.0)]))

    assert out.height == 0
    assert any("超出合理区间" in r.message for r in caplog.records)


def test_cumulative_multi_symbol_isolated():
    df = pl.concat([
        _frame([("20240618", 7.858), ("20240619", 8.02)], "600519.SH"),
        _frame([("20240618", 100.0), ("20240619", 100.0)], "000001.SZ"),
    ])

    out = cumulative_adj_factors_to_events(df)

    assert out["symbol"].to_list() == ["600519.SH"]


# ---- provider 集成: 回看窗口 + 换算 + 裁窗口 ----


def test_provider_extends_fetch_window_for_lookback(monkeypatch):
    """取数窗口必须前推 _ADJ_LOOKBACK_DAYS(40) 天, 否则窗口首个除权日会丢。"""
    provider, calls = _provider(monkeypatch, [_600519_SERIES])

    out = provider.get_adj_factors(
        ["600519.SH"], datetime(2024, 6, 18), datetime(2024, 6, 21)
    )

    sent = calls[0]["body"]["params"]
    assert sent["start_date"] == "20240509"  # 2024-06-18 前推 40 天
    assert sent["end_date"] == "20240621"
    assert sent["ts_code"] == "600519.SH"
    assert calls[0]["body"]["token"] == "test-key"
    # 事件在请求窗口内 → 保留
    assert out.height == 1
    assert out.row(0, named=True)["trade_date"] == date(2024, 6, 19)
    assert out.row(0, named=True)["ex_factor"] == pytest.approx(8.02 / 7.858)
    provider.close()


def test_provider_trims_events_outside_requested_window(monkeypatch):
    """回看窗口覆盖的事件若落在请求窗口之外(更早), 不得写进结果。"""
    provider, _ = _provider(monkeypatch, [_600519_SERIES])

    out = provider.get_adj_factors(
        ["600519.SH"], datetime(2024, 7, 1), datetime(2024, 7, 31)
    )

    assert out.is_empty()
    provider.close()


def test_provider_single_mode_keeps_legacy_behaviour(monkeypatch):
    """默认 single 模式: 上游原样给单事件比值, 不换算、不加回看窗口。"""
    provider, calls = _provider(
        monkeypatch, [[("20240619", 1.0206)]], mode="single"
    )

    out = provider.get_adj_factors(
        ["600519.SH"], datetime(2024, 6, 18), datetime(2024, 6, 21)
    )

    assert calls[0]["body"]["params"]["start_date"] == "20240618"  # 不加回看
    assert out.row(0, named=True)["ex_factor"] == pytest.approx(1.0206)
    provider.close()


def test_provider_cumulative_multi_symbol_matches_manual_math(monkeypatch):
    provider, _ = _provider(monkeypatch, [
        [("20240618", 7.858), ("20240619", 8.02)],
    ])

    out = provider.get_adj_factors(["600519.SH"], datetime(2024, 6, 18), datetime(2024, 6, 21))

    assert out["ex_factor"].to_list() == [pytest.approx(8.02 / 7.858)]
    provider.close()
