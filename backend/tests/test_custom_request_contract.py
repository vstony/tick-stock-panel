"""自定义源请求模板 / 请求体鉴权 / 列式信封 / 单位变换契约测试。

背景: 纯 YAML 接入 Tushare 这类网关需要三件能力, 缺一就静默返回 0 行(实测):
  1. 请求格式: token 进请求体、业务参数嵌套、ts_code 逗号串、日期紧凑 YYYYMMDD;
  2. 响应格式: 列式信封 `data.fields` + `data.items` 按字段名解包;
  3. 单位: 日K amount 千元 → 元。
全部用 httpx MockTransport 断言真实发出的请求体与最终帧, 不依赖网络与真实 Key。
"""

from __future__ import annotations

import json
from datetime import datetime

import httpx
import pytest

from app.data_providers.custom import template as tpl
from app.data_providers.custom.config import config_from_dict
from app.data_providers.custom.mapper import apply_transforms, extract_rows, payload_error
from app.data_providers.custom.provider import GenericHTTPProvider

# 实测列式信封(Tushare): 字段名与数据分离
TUSHARE_PAYLOAD = {
    "code": 0,
    "msg": "",
    "data": {
        "fields": ["ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount"],
        "items": [["600519.SH", "20250918", 1492.0, 1497.8, 1463.5, 1467.96, 49721.25, 7347477.479]],
        "has_more": False,
        "count": 1,
    },
}

_DAILY_FIELD_MAP = {
    "ts_code": "symbol", "trade_date": "date",
    "open": "open", "high": "high", "low": "low", "close": "close",
    "vol": "volume", "amount": "amount",
}

TUSHARE_LIKE_CONFIG = {
    "name": "tushare_like",
    "auth": {"type": "body", "param": "token", "token_env": "TUSHARE_API_KEY"},
    "datasets": {
        "daily": {
            "url": "http://upstream.local",
            "method": "POST",
            "batch": 5,
            "response_path": "data",
            "body": {
                "api_name": "daily",
                "fields": "ts_code,trade_date,open,high,low,close,vol,amount",
                "params": {
                    "ts_code": "${symbols}",
                    "start_date": "${start:%Y%m%d}",
                    "end_date": "${end:%Y%m%d}",
                },
            },
            "field_map": dict(_DAILY_FIELD_MAP),
            "transforms": {"date": "parse_date(value, '%Y%m%d')", "amount": "value * 1000"},
        }
    },
}


def _provider(monkeypatch, config: dict, handler) -> tuple[GenericHTTPProvider, list[dict]]:
    monkeypatch.setenv("TUSHARE_API_KEY", "test-key")
    sent: list[dict] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        sent.append({
            "url": str(request.url),
            "method": request.method,
            "body": json.loads(request.content.decode()) if request.content else None,
        })
        return handler(request)

    provider = GenericHTTPProvider(config_from_dict(config))
    provider._client = httpx.Client(transport=httpx.MockTransport(_handler))
    return provider, sent


# ---- 请求模板单元契约 ----


def test_template_renders_symbols_and_time_formats():
    ctx = {"symbols": "600519.SH,000001.SZ", "start": datetime(2025, 9, 1), "end": datetime(2025, 9, 18)}
    out = tpl.render({
        "params": {
            "ts_code": "${symbols}",
            "start_date": "${start:%Y%m%d}",
            "end_date": "${end:%Y-%m-%d %H:%M:%S}",
            "raw_end": "${end}",
        },
    }, ctx)

    assert out["params"]["ts_code"] == "600519.SH,000001.SZ"
    assert out["params"]["start_date"] == "20250901"
    assert out["params"]["end_date"] == "2025-09-18 00:00:00"
    assert out["params"]["raw_end"] == "2025-09-18T00:00:00"


def test_template_none_value_becomes_empty_string():
    assert tpl.render("${start}", {"start": None}) == ""


def test_template_unknown_variable_kept_literal_with_warning(caplog):
    with caplog.at_level("WARNING"):
        out = tpl.render("${nope}", {"start": None})

    assert out == "${nope}"  # 保持原样发送, 便于上游报错时定位
    assert any("未知变量" in r.message for r in caplog.records)


def test_template_format_on_non_time_value_warns(caplog):
    with caplog.at_level("WARNING"):
        out = tpl.render("${symbols:%Y%m%d}", {"symbols": "600519.SH"})

    assert out == "600519.SH"
    assert any("不是时间类型" in r.message for r in caplog.records)


def test_has_placeholder_detects_nested_usage():
    assert tpl.has_placeholder({"a": [{"b": "${symbols}"}]}) is True
    assert tpl.has_placeholder({"a": [{"b": "plain"}]}) is False
    assert tpl.has_placeholder({"a": None}) is False


# ---- 列式信封 + 千元变换 + 业务错误 ----


def test_columnar_envelope_unwrapped_to_records():
    rows = extract_rows(TUSHARE_PAYLOAD, "data")

    assert len(rows) == 1
    assert rows[0]["ts_code"] == "600519.SH"
    assert rows[0]["close"] == 1467.96
    assert rows[0]["amount"] == 7347477.479


def test_columnar_envelope_drops_malformed_rows_with_warning(caplog):
    payload = {"data": {"fields": ["a", "b"], "items": [["1", "2"], ["3"]]}}

    with caplog.at_level("WARNING"):
        rows = extract_rows(payload, "data")

    assert rows == [{"a": "1", "b": "2"}]
    assert any("列数" in r.message for r in caplog.records)


def test_transform_thousand_yuan_to_yuan():
    import polars as pl

    df = apply_transforms(pl.DataFrame({"amount": ["7347477.479"]}), {"amount": "value * 1000"})

    assert df["amount"][0] == pytest.approx(7347477.479 * 1000)


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"code": 0, "msg": ""}, None),
        ({"code": "0"}, None),
        ({"code": 40101, "msg": "您上传Token"}, "code=40101, msg=您上传Token"),
        ({"code": 40203, "detail": "无权限"}, "code=40203, msg=无权限"),
        ([{"code": 1}], None),
    ],
)
def test_payload_error_detection(payload, expected):
    assert payload_error(payload) == expected


def test_upstream_business_error_is_warned_not_silent(monkeypatch, caplog):
    """HTTP 200 + code!=0(token 放错位置/权限不足) 必须留告警, 不能只表现为 0 行。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 40101, "msg": "您上传Token", "data": None})

    provider, _ = _provider(monkeypatch, TUSHARE_LIKE_CONFIG, handler)

    with caplog.at_level("WARNING"):
        df = provider.get_daily(["600519.SH"], datetime(2025, 9, 1), datetime(2025, 9, 18))

    assert df.is_empty()
    assert any("业务错误" in r.message and "40101" in r.message for r in caplog.records)
    provider.close()


# ---- 端到端: 真实发出的请求体 + 最终帧 ----


def test_tushare_like_request_shape_and_frame(monkeypatch):
    """一次断言: token 进 body、参数嵌套 params、ts_code 逗号串、日期紧凑、千元→元。"""
    provider, sent = _provider(monkeypatch, TUSHARE_LIKE_CONFIG, lambda r: httpx.Response(200, json=TUSHARE_PAYLOAD))

    df = provider.get_daily(
        ["600519.SH", "000001.SZ"], datetime(2025, 9, 1), datetime(2025, 9, 18)
    )

    assert len(sent) == 1
    body = sent[0]["body"]
    assert sent[0]["method"] == "POST"
    assert body["api_name"] == "daily"
    assert body["token"] == "test-key"          # auth.type=body
    assert body["fields"].startswith("ts_code")
    assert body["params"]["ts_code"] == "600519.SH,000001.SZ"   # 逗号串, 不是数组
    assert body["params"]["start_date"] == "20250901"           # 紧凑 YYYYMMDD
    assert body["params"]["end_date"] == "20250918"
    # 模板接管后不再做默认注入(注入的 symbols 列表会污染请求体)
    assert "symbols" not in body and "ts_code" not in body

    assert df.height == 1
    row = df.row(0, named=True)
    assert row["symbol"] == "600519.SH"
    assert row["volume"] == pytest.approx(49721.25)             # 手, 不换算
    assert row["amount"] == pytest.approx(7347477.479 * 1000)   # 千元 → 元
    provider.close()


def test_non_templated_config_keeps_default_injection(monkeypatch):
    """没有占位符的数据集保持原有行为(symbols 数组进 body, ISO 时间), 不得回归。"""
    config = {
        "name": "legacy",
        "datasets": {
            "daily": {
                "url": "http://upstream.local",
                "method": "POST",
                "response_path": "data",
                "symbols_param": "codes",
                "start_param": "from",
                "end_param": "to",
                "field_map": dict(_DAILY_FIELD_MAP),
                "transforms": {"date": "parse_date(value, '%Y-%m-%d')"},
            }
        },
    }
    payload = {"data": [{
        "ts_code": "600519.SH", "trade_date": "2025-09-18",
        "open": 1492.0, "high": 1497.8, "low": 1463.5, "close": 1467.96,
        "vol": 49721.25, "amount": 7347477479.0,
    }]}
    provider, sent = _provider(monkeypatch, config, lambda r: httpx.Response(200, json=payload))

    df = provider.get_daily(["600519.SH"], datetime(2025, 9, 1), datetime(2025, 9, 18))

    body = sent[0]["body"]
    assert body["codes"] == ["600519.SH"]           # 仍按原契约传数组
    assert body["from"] == "2025-09-01T00:00:00"    # 仍是 ISO
    assert df.height == 1
    provider.close()


# ---- 请求体鉴权的边界 ----


def test_auth_body_requires_post_datasets():
    config = {
        "name": "get_body_auth",
        "auth": {"type": "body", "param": "token", "token_env": "TUSHARE_API_KEY"},
        "datasets": {
            "daily": {"url": "http://x", "method": "GET", "field_map": dict(_DAILY_FIELD_MAP)},
        },
    }
    provider = GenericHTTPProvider(config_from_dict(config))

    errors = provider.validate()

    assert any("auth.type=body" in e for e in errors)


def test_auth_body_warns_when_token_missing(monkeypatch, caplog):
    # 用不存在的环境变量名: _token_from_env 会回退读 .env, 借真实变量名测不出缺 Key 分支
    config = {**TUSHARE_LIKE_CONFIG, "auth": {
        "type": "body", "param": "token", "token_env": "TSP_TEST_MISSING_TOKEN",
    }}
    monkeypatch.delenv("TSP_TEST_MISSING_TOKEN", raising=False)
    provider = GenericHTTPProvider(config_from_dict(config))
    provider._client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=TUSHARE_PAYLOAD))
    )

    with caplog.at_level("WARNING"):
        sent_body = provider._request_rows(provider._dataset("daily"), symbols=["600519.SH"])

    assert sent_body  # 请求仍然发出(由上游报错), 只是没有 token 字段
    assert any("token is not set" in r.message for r in caplog.records)
    provider.close()


def test_adj_factor_mode_parsing_and_validation():
    cfg = config_from_dict({
        "name": "adjmode",
        "datasets": {
            "adj_factor": {"url": "http://x", "adj_factor_mode": "cumulative",
                           "field_map": {"ts_code": "symbol", "trade_date": "trade_date",
                                         "adj_factor": "ex_factor"}},
        },
    })
    assert cfg.datasets["adj_factor"].adj_factor_mode == "cumulative"

    with pytest.raises(ValueError, match="adj_factor_mode"):
        config_from_dict({
            "name": "bad",
            "datasets": {"adj_factor": {"url": "http://x", "adj_factor_mode": "accumulated"}},
        })
