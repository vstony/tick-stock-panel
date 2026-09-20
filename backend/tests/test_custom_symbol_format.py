"""标的格式契约: `600000.SH` 这种「6 位代码.交易所大写后缀」是全局唯一格式。

实测(见 docs/examples/tushare.yaml 注释): Tushare 对 600000 / sh600000 / 600000.XSHG /
600000.sz 全部返回 `code=0` **但 0 行** —— 静默无数据, 不报错。内部落盘/JOIN 也按该格式
对齐, 所以:
  1. 映射出的 symbol 不合格式时必须告警(否则"0 行"无从查起);
  2. 设置页「试拉测试」的手工输入(常只填 6 位代码)归一成规范格式并回显实际请求标的。
"""

from __future__ import annotations

import logging

import pytest

from app.api.settings import (
    CustomSourceTestIn,
    _canonical_test_symbols,
)
from app.api.settings import (
    test_data_source as run_data_source_test,
)
from app.data_providers.custom.config import config_from_dict
from app.data_providers.custom.provider import GenericHTTPProvider

_DAILY_FIELDS = ("ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount")

_CONFIG = {
    "name": "fmt_source",
    "auth": {"type": "none"},
    "datasets": {
        "daily": {
            "url": "http://upstream.local",
            "method": "POST",
            "response_path": "data",
            "field_map": {
                "ts_code": "symbol",
                "trade_date": "date",
                "open": "open",
                "high": "high",
                "low": "low",
                "close": "close",
                "vol": "volume",
                "amount": "amount",
            },
            "transforms": {"date": "parse_date(value, '%Y%m%d')"},
        }
    },
}


def _mapped(rows: list[dict]):
    provider = GenericHTTPProvider(config_from_dict(_CONFIG))
    try:
        return provider._mapped_frame(provider.config.datasets["daily"], rows)
    finally:
        provider.close()


def _row(ts_code: str) -> dict:
    return {
        "ts_code": ts_code,
        "trade_date": "20260918",
        "open": 10.0,
        "high": 10.0,
        "low": 10.0,
        "close": 10.0,
        "vol": 100.0,
        "amount": 1000.0,
    }


def test_suffixless_symbol_raises_a_locating_warning(caplog):
    with caplog.at_level(logging.WARNING):
        df = _mapped([_row("600000"), _row("000001")])

    assert df.get_column("symbol").to_list() == ["600000", "000001"]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("600000.SH" in w and "600000" in w for w in warnings), warnings


@pytest.mark.parametrize("code", ["600000.SH", "000001.SZ", "920002.BJ"])
def test_canonical_symbol_does_not_warn(caplog, code):
    with caplog.at_level(logging.WARNING):
        _mapped([_row(code)])

    assert [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING] == []


def test_canonical_test_symbols_fills_exchange_suffix():
    assert _canonical_test_symbols(["600000", "000001", "  600519.SH  "]) == [
        "600000.SH",
        "000001.SZ",
        "600519.SH",
    ]


def test_canonical_test_symbols_passthrough_when_empty():
    assert _canonical_test_symbols(None) is None
    assert _canonical_test_symbols([]) == []


def test_trial_endpoint_sends_normalized_symbols_and_echoes_them(monkeypatch):
    from app.data_providers import custom as custom_sources

    provider = type("P", (), {})()
    captured: dict = {}

    def fake_test_dataset(dataset, symbols):
        captured["dataset"] = dataset
        captured["symbols"] = symbols
        return {"provider": "tushare_api", "dataset": dataset, "rows": 0, "columns": [], "preview": []}

    provider.test_dataset = fake_test_dataset
    provider.close = lambda: None
    monkeypatch.setattr(custom_sources, "get_provider", lambda _name: provider)

    result = run_data_source_test(CustomSourceTestIn(
        provider="tushare_api",
        dataset="daily",
        symbols=["600000", "000001.SZ"],
    ))

    assert captured["symbols"] == ["600000.SH", "000001.SZ"]
    assert result["symbols"] == ["600000.SH", "000001.SZ"]


def test_trial_endpoint_without_symbols_keeps_provider_default(monkeypatch):
    from app.data_providers import custom as custom_sources

    provider = type("P", (), {})()
    captured: dict = {}

    def fake_test_dataset(dataset, symbols):
        captured["symbols"] = symbols
        return {"provider": "x", "dataset": dataset, "rows": 0, "columns": [], "preview": []}

    provider.test_dataset = fake_test_dataset
    provider.close = lambda: None
    monkeypatch.setattr(custom_sources, "get_provider", lambda _name: provider)

    result = run_data_source_test(CustomSourceTestIn(provider="x", dataset="daily"))

    assert captured["symbols"] is None
    assert "symbols" not in result


def test_daily_field_map_contract_uses_ts_code_column():
    """回归: 示例配置把 ts_code 映射到内部 symbol (格式不变, 只改名)。"""
    provider = GenericHTTPProvider(config_from_dict(_CONFIG))
    try:
        assert provider.config.datasets["daily"].field_map["ts_code"] == "symbol"
        assert set(_DAILY_FIELDS) <= set(provider.config.datasets["daily"].field_map)
    finally:
        provider.close()
