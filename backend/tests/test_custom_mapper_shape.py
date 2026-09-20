"""自定义源「上游响应形状」契约测试。

自定义源接受两种记录形状: `list[dict]`(或单个对象) 与列式信封
`{fields: [...], items: [[...]]}`(按字段名与位置对应解包, Tushare 等网关的通用约定)。
其余形状(位置数组、嵌套非记录结构) 无法建立字段对应关系 —— 必须明确告警并跳过,
不能静默产出 0 行(用户只看到"无数据"), 也不能把 polars 内部异常栈抛给同步任务。
列式信封解包与千元→元变换的完整用例见 test_custom_request_contract.py。
测试不依赖网络: 用 httpx MockTransport 注入真实响应形状。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx
import polars as pl
import pytest

from app.data_providers.custom.config import config_from_dict
from app.data_providers.custom.mapper import extract_rows, map_rows
from app.data_providers.custom.provider import GenericHTTPProvider

# 实测结构(2026-09): 顶层 request_id/code/data/msg/detail, data 为列式信封
TUSHARE_STYLE_PAYLOAD = {
    "request_id": "abc",
    "code": 0,
    "msg": None,
    "detail": None,
    "data": {
        "fields": ["ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount"],
        "items": [["600519.SH", "20250918", 1492.0, 1497.8, 1463.5, 1467.96, 49721.25, 7347477.479]],
        "has_more": False,
        "count": 1,
    },
}


def test_positional_array_is_rejected_with_warning(caplog):
    """response_path 指到 items(位置数组, 无字段名可对)时拒绝并告警。"""
    with caplog.at_level("WARNING"):
        rows = extract_rows(TUSHARE_STYLE_PAYLOAD, "data.items")

    assert rows == []
    assert any("位置数组" in r.message for r in caplog.records)


def test_record_list_and_single_object_still_supported():
    """既有契约不许回归: list[dict] 正常提取; 单个对象按单条记录处理。"""
    assert extract_rows({"data": [{"a": 1}, {"a": 2}]}, "data") == [{"a": 1}, {"a": 2}]
    assert extract_rows({"data": {"a": 1}}, "data") == [{"a": 1}]
    # 信封里夹了非对象元素时只保留对象元素
    assert extract_rows({"data": [{"a": 1}, "x", 3]}, "data") == [{"a": 1}]


def test_map_rows_survives_heterogeneous_nesting(caplog):
    """列式信封若绕过 extract_rows 守卫进入 map_rows, 也不得把 polars 异常抛给同步任务。"""
    with caplog.at_level("WARNING"):
        df = map_rows([TUSHARE_STYLE_PAYLOAD["data"]], {"ts_code": "symbol"})

    assert df.is_empty()
    assert any("无法构造成记录表" in r.message for r in caplog.records)


def test_provider_warns_when_no_field_mappable(caplog):
    """端到端: 上游 200 但字段名全对不上 → 告警点名 response_path/field_map, 而非静默 0 行。"""
    def handler(request: httpx.Request) -> httpx.Response:
        # 上游用 code/px 命名, YAML 里映射的是 ts_code/trade_date/close → 一个都对不上
        return httpx.Response(200, json={"data": [{"code": "600519", "px": 1467.96}]})

    config = config_from_dict({
        "name": "shape_check",
        "datasets": {
            "daily": {
                "url": "http://upstream.local/daily",
                "method": "POST",
                "response_path": "data",
                "field_map": {"ts_code": "symbol", "trade_date": "date", "close": "close"},
            }
        },
    })
    provider = GenericHTTPProvider(config)
    provider._client = httpx.Client(transport=httpx.MockTransport(handler))

    end = datetime(2025, 9, 18)
    with caplog.at_level("WARNING"):
        df = provider.get_daily(["600519.SH"], end - timedelta(days=10), end)

    assert df.is_empty()
    assert any(
        "无任何字段可映射" in r.message and "response_path/field_map" in r.message
        for r in caplog.records
    )
    provider.close()


def test_provider_still_maps_valid_records():
    """正例守门: 记录列表形状下依然正常出帧(改动未破坏既有映射)。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "data": [{
                "ts_code": "600519.SH", "trade_date": "2025-09-18",
                "open": 1492.0, "high": 1497.8, "low": 1463.5, "close": 1467.96,
                "vol": 49721.25, "amount": 7347477479.0,
            }]
        })

    config = config_from_dict({
        "name": "shape_ok",
        "datasets": {
            "daily": {
                "url": "http://upstream.local/daily",
                "method": "POST",
                "response_path": "data",
                "field_map": {
                    "ts_code": "symbol", "trade_date": "date",
                    "open": "open", "high": "high", "low": "low", "close": "close",
                    "vol": "volume", "amount": "amount",
                },
                "transforms": {"date": "parse_date(value, '%Y-%m-%d')"},
            }
        },
    })
    provider = GenericHTTPProvider(config)
    provider._client = httpx.Client(transport=httpx.MockTransport(handler))

    end = datetime(2025, 9, 18)
    df = provider.get_daily(["600519.SH"], end - timedelta(days=10), end)

    assert df.height == 1
    assert df.row(0, named=True)["symbol"] == "600519.SH"
    assert df.schema["date"] == pl.Date
    provider.close()


@pytest.mark.parametrize("path", ["data.items", ""])
def test_no_crash_on_positional_payload_end_to_end(path, caplog):
    """位置数组写法不能让同步任务崩, 只能得到空结果 + 告警。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"items": [["600519.SH", "20250918", 1467.96]]}})

    config = config_from_dict({
        "name": "shape_crash",
        "datasets": {
            "daily": {
                "url": "http://upstream.local/daily",
                "method": "POST",
                "response_path": path,
                "field_map": {
                    "ts_code": "symbol", "trade_date": "date",
                    "open": "open", "high": "high", "low": "low", "close": "close",
                    "vol": "volume", "amount": "amount",
                },
            }
        },
    })
    provider = GenericHTTPProvider(config)
    provider._client = httpx.Client(transport=httpx.MockTransport(handler))

    end = datetime(2025, 9, 18)
    with caplog.at_level("WARNING"):
        df = provider.get_daily(["600519.SH"], end - timedelta(days=10), end)

    assert df.is_empty()
    assert caplog.records, "必须留下告警, 不得静默"
    provider.close()
