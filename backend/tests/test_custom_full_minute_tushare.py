"""Tushare 全量分钟(full_minute)契约 —— 声明式 YAML 接入 doc_id=370 股票历史分钟行情。

回归背景(用户实测): `full_minute` 数据集**在 YAML 加载路径被静默丢弃** ——
`config_from_dict` 的数据集白名单漏了 full_minute, 而 loader 的保存白名单、provider 的
必填列表、前端编辑器都支持它。于是「设置 → 数据源 → 全量分钟」里选不到该源, 只在日志里
看不到任何提示。本文件同时守住: 声明不丢、请求按 full_minute 自己的模板发、当日窗口口径。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from app.data_providers.custom import loader
from app.data_providers.custom import provider as provider_module
from app.data_providers.custom.config import config_from_dict, load_config
from app.data_providers.custom.provider import GenericHTTPProvider

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_YAML = REPO_ROOT / "docs" / "examples" / "tushare.yaml"

_MINUTE_FIELDS = ("symbol", "datetime", "open", "high", "low", "close", "volume", "amount")

# 上游列名 → 内部列名的映射(两份数据集共用, 只区分 url 与 api 名)
_UPSTREAM_FIELD_MAP = {
    "ts_code": "symbol",
    "trade_time": "datetime",
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "vol": "volume",
    "amount": "amount",
}

# 单日 1min ≈ 241 根/股 (9:30-11:30 + 13:00-15:00); stk_mins 单请求 8000 行硬上限:
# 超限是静默截断且丢最旧(实测 20 标的跨 2 日应回 9640 行, 实得恰好 8000)。
_BARS_PER_DAY = 241
_TUSHARE_ROW_CAP = 8000


def _yaml(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "full_minute_source.yaml"
    path.write_text(content, encoding="utf-8")
    return path


_FULL_MINUTE_ONLY = """
name: fm_source
auth:
  type: none
datasets:
  full_minute:
    url: http://upstream.local/full_minute
    method: POST
    batch: 20
    response_path: data
    field_map:
      ts_code: symbol
      trade_time: datetime
      open: open
      high: high
      low: low
      close: close
      vol: volume
      amt: amount
    transforms:
      datetime: "parse_datetime(value, '%Y-%m-%d %H:%M:%S')"
      volume: "value / 100"
"""


def _provider(monkeypatch, config: dict, handler) -> tuple[GenericHTTPProvider, list[dict]]:
    sent: list[dict] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        sent.append({
            "url": str(request.url),
            "body": json.loads(request.content.decode()) if request.content else None,
        })
        return handler(request)

    provider = GenericHTTPProvider(config_from_dict(config))
    provider._client = httpx.Client(transport=httpx.MockTransport(_handler))
    return provider, sent


# ---- 声明不丢: 这是本次的真实故障 ----


def test_full_minute_declared_in_yaml_survives_load(tmp_path):
    path = _yaml(tmp_path, _FULL_MINUTE_ONLY)

    config = load_config(path)

    assert "full_minute" in config.datasets
    assert config.datasets["full_minute"].url == "http://upstream.local/full_minute"


def test_full_minute_survives_settings_sanitize(tmp_path):
    """设置页保存(load → _sanitize_for_yaml → 写回)不能把 full_minute 丢掉。"""
    path = _yaml(tmp_path, _FULL_MINUTE_ONLY)
    config = load_config(path)

    cleaned = loader._sanitize_for_yaml(loader._config_to_dict(config))

    assert "full_minute" in cleaned["datasets"]
    assert cleaned["datasets"]["full_minute"]["batch"] == 20


def test_unknown_dataset_name_is_reported(tmp_path, caplog):
    """拼错的数据集名(如 full_minutes)只丢掉不吭声 = 用户以为声明生效 → 必须告警。"""
    path = _yaml(tmp_path, _FULL_MINUTE_ONLY.replace("full_minute:", "full_minutes:"))

    with caplog.at_level(logging.WARNING):
        config = load_config(path)

    assert config.datasets == {}
    assert any("full_minutes" in record.getMessage() for record in caplog.records)


def test_declarable_datasets_match_loader_whitelist():
    """声明集合是唯一真源: 两边漂移正是 full_minute 被静默丢弃的成因。"""
    from app.data_providers.custom.config import DECLARABLE_DATASETS

    expected = {"daily", "adj_factor", "realtime", "minute", "full_minute", "financial"}
    assert sorted(DECLARABLE_DATASETS) == sorted(expected)
    payload = {
        name: {"url": "http://upstream.local", "response_path": "data"}
        for name in DECLARABLE_DATASETS
    }
    cleaned = loader._sanitize_for_yaml({"name": "all", "datasets": payload})
    assert set(cleaned["datasets"]) == DECLARABLE_DATASETS


# ---- 请求合同: 走 full_minute 自己的模板 + 当日窗口 ----


def test_intraday_batch_uses_full_minute_dataset_template(monkeypatch):
    """get_intraday_batch 必须按 full_minute 段发请求(而不是 minute 段)。"""
    payload = {
        "code": 0,
        "data": {
            "fields": ["ts_code", "trade_time", "open", "high", "low", "close", "vol", "amt"],
            "items": [["600519.SH", "2026-09-18 09:31:00", 1.0, 1.0, 1.0, 1.0, 100.0, 1000.0]],
        },
    }
    config = {
        "name": "fm_source",
        "auth": {"type": "none"},
        "datasets": {
            "minute": {
                "url": "http://upstream.local/minute",
                "method": "POST",
                "response_path": "data",
                "field_map": dict(_UPSTREAM_FIELD_MAP),
                "transforms": {"datetime": "parse_datetime(value, '%Y-%m-%d %H:%M:%S')"},
            },
            "full_minute": {
                "url": "http://upstream.local/full_minute",
                "method": "POST",
                "response_path": "data",
                "field_map": dict(_UPSTREAM_FIELD_MAP),
                "transforms": {"datetime": "parse_datetime(value, '%Y-%m-%d %H:%M:%S')"},
            },
        },
    }
    provider, sent = _provider(monkeypatch, config, lambda _: httpx.Response(200, json=payload))

    df = provider.get_intraday_batch(["600519.SH"])

    assert [item["url"] for item in sent] == ["http://upstream.local/full_minute"]
    assert df.height == 1
    assert df.get_column("datetime").to_list() == [datetime(2026, 9, 18, 9, 31)]


def test_intraday_batch_window_covers_whole_day(monkeypatch, tmp_path):
    """修复轮窗口 = 当日 00:00 → 现在(北京时间墙钟), 且按 batch 分块。"""
    config = config_from_dict({
        "name": "fm_source",
        "auth": {"type": "none"},
        "datasets": {
            "full_minute": {
                "url": "http://upstream.local",
                "method": "POST",
                "batch": 20,
                "response_path": "data",
                "body": {
                    "start_date": "${start:%Y-%m-%d %H:%M:%S}",
                    "end_date": "${end:%Y-%m-%d %H:%M:%S}",
                    "ts_code": "${symbols}",
                },
                "field_map": {name: name for name in _MINUTE_FIELDS},
            }
        },
    })
    captured: list[dict] = []

    def _request_rows(cfg, **kwargs):
        captured.append(kwargs)
        return []

    monkeypatch.setattr(provider_module, "cn_now", lambda: datetime(2026, 9, 18, 14, 30, 5))
    provider = GenericHTTPProvider(config)
    provider._request_rows = _request_rows
    try:
        provider.get_intraday_batch([f"6000{i:02d}.SH" for i in range(45)])
    finally:
        provider.close()

    assert [item["start_time"] for item in captured] == [datetime(2026, 9, 18, 0, 0)] * 3
    assert [item["end_time"] for item in captured] == [datetime(2026, 9, 18, 14, 30, 5)] * 3
    assert [len(item["symbols"]) for item in captured] == [20, 20, 5]


# ---- 随仓示例: Tushare 的 full_minute 段必须与接口上限自洽 ----


def test_shipped_tushare_example_declares_full_minute():
    config = load_config(EXAMPLE_YAML)
    provider = GenericHTTPProvider(config)
    try:
        assert provider.validate() == []
        assert "full_minute" in provider.config.datasets
        section = config.datasets["full_minute"]
        assert section.body["api_name"] == "stk_mins"
        assert section.body["params"]["freq"] == "1min"
        assert section.body["params"]["ts_code"] == "${symbols}"
    finally:
        provider.close()


@pytest.mark.parametrize("dataset", ["minute", "full_minute"])
def test_shipped_tushare_example_batch_fits_row_cap(dataset):
    """batch × 单日 241 根 必须留在 8000 行内: stk_mins 超限是静默截断且丢最旧。"""
    section = load_config(EXAMPLE_YAML).datasets[dataset]

    assert section.batch * _BARS_PER_DAY <= _TUSHARE_ROW_CAP


def test_shipped_example_full_minute_returns_canonical_minute_frame(monkeypatch):
    """列式信封 + 单位换算(vol 股→手) 后必须是分钟K canonical 帧。"""
    payload = {
        "code": 0,
        "msg": "",
        "data": {
            "fields": ["ts_code", "trade_time", "open", "high", "low", "close", "vol", "amount"],
            "items": [
                ["600519.SH", "2026-09-18 09:31:00", 1250.0, 1252.0, 1249.0, 1251.0, 49721.0, 6.2e7],
            ],
        },
    }
    provider, sent = _provider(
        monkeypatch, {"name": "x", "auth": {"type": "none"}, **_example_datasets()},
        lambda _: httpx.Response(200, json=payload),
    )

    df = provider.get_intraday_batch(["600519.SH"])

    assert sorted(df.columns) == sorted(_MINUTE_FIELDS)
    assert df.get_column("volume").to_list() == [pytest.approx(497.21)]
    assert df.get_column("amount").to_list() == [pytest.approx(6.2e7)]
    assert sent[0]["body"]["api_name"] == "stk_mins"


def _example_datasets() -> dict:
    import yaml

    raw = yaml.safe_load(EXAMPLE_YAML.read_text(encoding="utf-8"))
    return {"datasets": raw["datasets"]}


def test_example_full_minute_requires_all_minute_columns():
    config = load_config(EXAMPLE_YAML)

    mapped = set(config.datasets["full_minute"].field_map.values())
    assert set(_MINUTE_FIELDS) <= mapped
