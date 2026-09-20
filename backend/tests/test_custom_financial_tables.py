"""自定义源 financial 数据集契约: 表名映射(table_map)、单位、去重、行数上限。

背景: 一个 `financial` 数据集要覆盖多张上游接口(利润表/资产负债表/…), 只能靠 `table_map`
把内部表名映射成上游取值, 再由 `${table}` 注入请求体。缺这一层就只能对着一张表拉五份数据。
全部用 httpx MockTransport 断言真实发出的请求体与最终帧, 不依赖网络与真实 Key。
"""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from app.data_providers.custom.config import config_from_dict
from app.data_providers.custom.loader import _config_to_dict, _sanitize_for_yaml
from app.data_providers.custom.provider import GenericHTTPProvider

# 实测列式信封(Tushare): 字段名与数据分离; income 与 balancesheet 都常见同期重复行
TUSHARE_FIELDS = ["ts_code", "end_date", "ann_date", "revenue", "n_income_attr_p", "total_assets"]

_FINANCIAL_CONFIG = {
    "name": "tushare_like",
    "auth": {"type": "body", "param": "token", "token_env": "TUSHARE_API_KEY"},
    "datasets": {
        "financial": {
            "url": "http://api.tushare.pro",
            "method": "POST",
            "batch": 1,
            "response_path": "data",
            "table_map": {
                "metrics": "fina_indicator",
                "income": "income",
                "balance_sheet": "balancesheet",
                "cash_flow": "cashflow",
            },
            "body": {
                "api_name": "${table}",
                "fields": "ts_code,end_date,ann_date,revenue,n_income_attr_p,total_assets",
                "params": {"ts_code": "${symbols}"},
            },
            "field_map": {
                "ts_code": "symbol",
                "end_date": "period_end",
                "ann_date": "announce_date",
                "revenue": "revenue",
                "n_income_attr_p": "net_income_attributable",
                "total_assets": "total_assets",
            },
            "transforms": {
                "period_end": "parse_date(value, '%Y%m%d')",
                "announce_date": "parse_date(value, '%Y%m%d')",
            },
        }
    },
}


def _envelope(items: list[list]) -> dict:
    return {"code": 0, "msg": "", "data": {"fields": list(TUSHARE_FIELDS), "items": items}}


def _row(end_date: str, revenue: float, assets: float, ts_code: str = "600519.SH", ann: str = "20240403"):
    return [ts_code, end_date, ann, revenue, revenue / 2, assets]


def _provider(monkeypatch, config: dict, handler) -> tuple[GenericHTTPProvider, list[dict]]:
    monkeypatch.setenv("TUSHARE_API_KEY", "test-key")
    sent: list[dict] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        sent.append({
            "body": json.loads(request.content.decode()) if request.content else None,
        })
        return handler(request)

    provider = GenericHTTPProvider(config_from_dict(config))
    provider._client = httpx.Client(transport=httpx.MockTransport(_handler))
    return provider, sent


# ---- 表名映射: 每个内部表都用自己的上游接口名请求 ----


@pytest.mark.parametrize(
    ("table", "api_name"),
    [
        ("metrics", "fina_indicator"),
        ("income", "income"),
        ("balance_sheet", "balancesheet"),
        ("cash_flow", "cashflow"),
    ],
)
def test_table_map_routes_internal_table_to_upstream_api(monkeypatch, table, api_name):
    provider, sent = _provider(monkeypatch, _FINANCIAL_CONFIG, lambda _: httpx.Response(200, json=_envelope([])))

    provider.get_financials(table, ["600519.SH"])

    assert [item["body"]["api_name"] for item in sent] == [api_name]


def test_table_without_table_map_entry_is_skipped_without_request(monkeypatch):
    """table_map 声明即支持范围: 未声明的表(shares)不发请求、返回空表。"""
    provider, sent = _provider(monkeypatch, _FINANCIAL_CONFIG, lambda _: httpx.Response(200, json=_envelope([])))

    df = provider.get_financials("shares", ["600519.SH"])

    assert df.is_empty()
    assert sent == []


def test_table_map_absent_keeps_legacy_table_param(monkeypatch):
    """没有 table_map 的源(通用上游)仍把内部表名原样发出去, 保持向后兼容。"""
    config = {
        **_FINANCIAL_CONFIG,
        "datasets": {
            "financial": {
                key: value
                for key, value in _FINANCIAL_CONFIG["datasets"]["financial"].items()
                if key != "table_map"
            }
        },
    }
    provider, sent = _provider(monkeypatch, config, lambda _: httpx.Response(200, json=_envelope([])))

    provider.get_financials("income", ["600519.SH"])

    assert [item["body"]["api_name"] for item in sent] == ["income"]


# ---- 行数 / 重复行 / 单位 ----


def test_large_response_keeps_all_rows_with_mixed_numeric_types(monkeypatch):
    """回归: >100 行且后段出现前段未见的浮点列时, 不许整块丢数据。

    实测 Tushare 单股可返回 119~129 期报表; polars 默认只扫前 100 行推断类型,
    构造失败会让整批(而非个别行)变成空帧。
    """
    items = [_row(f"{2000 + i // 4}{((i % 4) + 1) * 3:02d}30", 100.0 + i, 1_000.0) for i in range(120)]
    items.append(_row("20491231", 1.5e11, 3_000_000.0, ann="20500331"))
    provider, _ = _provider(monkeypatch, _FINANCIAL_CONFIG, lambda _: httpx.Response(200, json=_envelope(items)))

    df = provider.get_financials("income", ["600519.SH"])

    assert df.height == 121
    assert df.get_column("revenue").max() == pytest.approx(1.5e11)


def test_duplicate_report_period_rows_are_deduplicated(monkeypatch):
    """上游对同一报告期返回完全重复行: 落盘契约是 (symbol, period_end) 唯一。"""
    items = [
        _row("20240331", 100.0, 1_000.0, ann="20240403"),
        _row("20240331", 100.0, 1_000.0, ann="20240403"),
        _row("20240630", 200.0, 2_000.0, ann="20240815"),
    ]
    provider, _ = _provider(monkeypatch, _FINANCIAL_CONFIG, lambda _: httpx.Response(200, json=_envelope(items)))

    df = provider.get_financials("income", ["600519.SH"])

    assert df.height == 2


def test_dedup_keeps_latest_announcement_of_same_period(monkeypatch):
    """同期多公告日(重述/调整)保留最新公告那行, 与 financial_sync._merge_report_history 同语义。"""
    items = [
        _row("20240331", 100.0, 1_000.0, ann="20240403"),
        _row("20240331", 111.0, 1_000.0, ann="20240620"),
    ]
    provider, _ = _provider(monkeypatch, _FINANCIAL_CONFIG, lambda _: httpx.Response(200, json=_envelope(items)))

    df = provider.get_financials("income", ["600519.SH"])

    assert df.height == 1
    assert df.get_column("revenue").to_list() == [pytest.approx(111.0)]


def test_batch_one_sends_one_request_per_symbol(monkeypatch):
    """不支持多标的的接口靠 batch: 1 保证每标的一次请求(逗号串会被静默当无数据)。"""
    provider, sent = _provider(monkeypatch, _FINANCIAL_CONFIG, lambda _: httpx.Response(200, json=_envelope([])))

    provider.get_financials("income", ["600519.SH", "000001.SZ", "000002.SZ"])

    assert [item["body"]["params"]["ts_code"] for item in sent] == [
        "600519.SH", "000001.SZ", "000002.SZ",
    ]


def test_columnar_envelope_maps_to_canonical_columns(monkeypatch):
    items = [_row("20240331", 1.4769e11, 2.7270e11)]
    provider, _ = _provider(monkeypatch, _FINANCIAL_CONFIG, lambda _: httpx.Response(200, json=_envelope(items)))

    df = provider.get_financials("balance_sheet", ["600519.SH"])

    assert {"symbol", "period_end", "announce_date", "total_assets"} <= set(df.columns)
    assert df.get_column("period_end").to_list() == [date(2024, 3, 31)]
    assert df.get_column("total_assets").to_list() == [pytest.approx(2.7270e11)]


def test_share_units_convert_wan_to_gu(monkeypatch):
    """股本单位: 上游万股 → 内部股(转手数计算依赖绝对股数)。"""
    config = {
        **_FINANCIAL_CONFIG,
        "datasets": {
            "financial": {
                **_FINANCIAL_CONFIG["datasets"]["financial"],
                "table_map": {"shares": "daily_basic"},
                "body": {
                    "api_name": "${table}",
                    "fields": "ts_code,trade_date,float_share",
                    "params": {"ts_code": "${symbols}"},
                },
                "field_map": {
                    "ts_code": "symbol",
                    "trade_date": "period_end",
                    "float_share": "float_shares",
                },
                "transforms": {
                    "period_end": "parse_date(value, '%Y%m%d')",
                    "float_shares": "value * 10000",
                },
            }
        },
    }
    payload = {
        "code": 0,
        "data": {
            "fields": ["ts_code", "trade_date", "float_share"],
            "items": [["600519.SH", "20240110", 125619.78]],
        },
    }
    provider, _ = _provider(monkeypatch, config, lambda _: httpx.Response(200, json=payload))

    df = provider.get_financials("shares", ["600519.SH"])

    assert {"symbol", "period_end", "float_shares"} <= set(df.columns)
    assert df.get_column("float_shares").to_list() == [pytest.approx(1_256_197_800)]


# ---- 配置: 校验与回环 ----


def test_validate_rejects_table_map_on_non_financial_dataset(monkeypatch):
    config = {
        "name": "bad_map",
        "auth": {"type": "none"},
        "datasets": {
            "daily": {
                "url": "http://upstream.local",
                "response_path": "data",
                "table_map": {"metrics": "fina_indicator"},
                "field_map": {"ts_code": "symbol", "trade_date": "date"},
            }
        },
    }
    provider = GenericHTTPProvider(config_from_dict(config))

    errors = provider.validate()

    assert any("table_map" in err for err in errors)


def test_config_parse_rejects_non_mapping_table_map():
    config = {
        "name": "bad_map",
        "auth": {"type": "none"},
        "datasets": {
            "financial": {
                "url": "http://upstream.local",
                "response_path": "data",
                "table_map": ["metrics", "fina_indicator"],
                "field_map": {"ts_code": "symbol"},
            }
        },
    }

    with pytest.raises(ValueError, match="table_map"):
        config_from_dict(config)


def test_config_roundtrip_preserves_table_map():
    config = config_from_dict(_FINANCIAL_CONFIG)

    out = _config_to_dict(config)

    assert out["datasets"]["financial"]["table_map"] == {
        "metrics": "fina_indicator",
        "income": "income",
        "balance_sheet": "balancesheet",
        "cash_flow": "cashflow",
    }
    assert _sanitize_for_yaml(out)["datasets"]["financial"]["table_map"]["income"] == "income"


def test_sanitize_rejects_table_map_on_non_financial_dataset():
    payload = {
        **_FINANCIAL_CONFIG,
        "datasets": {
            "daily": {
                "url": "http://upstream.local",
                "response_path": "data",
                "table_map": {"a": "b"},
            }
        },
    }

    with pytest.raises(ValueError, match="table_map"):
        _sanitize_for_yaml(payload)
