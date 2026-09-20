"""自定义源配置回环契约: body / params / adj_factor_mode 不得在保存-回填链路上丢失。

这三项没有设置页表单控件(由 YAML 手写), 但一旦在设置页保存一次就被静默丢弃,
源会退化成"请求体缺失"进而全线 0 行 —— 属于静默数据丢失, 必须有测试守住。
"""

from __future__ import annotations

import pytest

from app.data_providers.custom import loader
from app.data_providers.custom.config import config_from_dict

_TEMPLATED = {
    "name": "tushare_like",
    "display_name": "Tushare Like",
    "auth": {"type": "body", "param": "token", "token_env": "TUSHARE_API_KEY"},
    "datasets": {
        "daily": {
            "url": "http://api.tushare.pro",
            "method": "POST",
            "batch": 20,
            "response_path": "data",
            "body": {
                "api_name": "daily",
                "params": {"ts_code": "${symbols}", "start_date": "${start:%Y%m%d}"},
            },
            "params": {"fields": "ts_code,trade_date"},
            "field_map": {
                "ts_code": "symbol", "trade_date": "date",
                "open": "open", "high": "high", "low": "low", "close": "close",
                "vol": "volume", "amount": "amount",
            },
            "transforms": {"date": "parse_date(value, '%Y%m%d')", "amount": "value * 1000"},
        },
        "adj_factor": {
            "url": "http://api.tushare.pro",
            "method": "POST",
            "response_path": "data",
            "adj_factor_mode": "cumulative",
            "field_map": {"ts_code": "symbol", "trade_date": "trade_date", "adj_factor": "ex_factor"},
        },
    },
}


def test_config_to_dict_keeps_template_and_factor_mode():
    config = config_from_dict(_TEMPLATED)

    out = loader._config_to_dict(config)

    daily = out["datasets"]["daily"]
    assert daily["body"]["api_name"] == "daily"
    assert daily["body"]["params"]["ts_code"] == "${symbols}"
    assert daily["params"] == {"fields": "ts_code,trade_date"}
    assert daily["transforms"]["amount"] == "value * 1000"
    assert out["datasets"]["adj_factor"]["adj_factor_mode"] == "cumulative"


def test_sanitize_keeps_template_and_factor_mode():
    cleaned = loader._sanitize_for_yaml(_TEMPLATED)

    assert cleaned["datasets"]["daily"]["body"]["params"]["ts_code"] == "${symbols}"
    assert cleaned["datasets"]["daily"]["params"] == {"fields": "ts_code,trade_date"}
    assert cleaned["datasets"]["adj_factor"]["adj_factor_mode"] == "cumulative"


def test_sanitize_drops_default_single_mode():
    """single 是默认值, 不必写回 YAML(避免无意义 diff)。"""
    payload = {**_TEMPLATED, "datasets": {
        "adj_factor": {**_TEMPLATED["datasets"]["adj_factor"], "adj_factor_mode": "single"},
    }}

    cleaned = loader._sanitize_for_yaml(payload)

    assert "adj_factor_mode" not in cleaned["datasets"]["adj_factor"]


def test_sanitize_rejects_adj_factor_mode_on_other_datasets():
    payload = {**_TEMPLATED, "datasets": {
        "daily": {**_TEMPLATED["datasets"]["daily"], "adj_factor_mode": "cumulative"},
    }}

    with pytest.raises(ValueError, match="adj_factor_mode"):
        loader._sanitize_for_yaml(payload)


def test_sanitize_rejects_invalid_adj_factor_mode():
    payload = {**_TEMPLATED, "datasets": {
        "adj_factor": {**_TEMPLATED["datasets"]["adj_factor"], "adj_factor_mode": "accumulated"},
    }}

    with pytest.raises(ValueError, match="adj_factor_mode"):
        loader._sanitize_for_yaml(payload)


def test_sanitize_rejects_non_object_body():
    payload = {**_TEMPLATED, "datasets": {
        "daily": {**_TEMPLATED["datasets"]["daily"], "body": "api_name=daily"},
    }}

    with pytest.raises(ValueError, match="body"):
        loader._sanitize_for_yaml(payload)


def test_load_config_round_trip_via_yaml_file(tmp_path):
    """真实 YAML 文件 → 加载 → 生成 provider → 请求模板生效(回环闭环)。"""
    import yaml

    path = tmp_path / "tushare_like.yaml"
    path.write_text(yaml.safe_dump(_TEMPLATED, allow_unicode=True), encoding="utf-8")

    config = loader.load_config(path)
    provider = loader.GenericHTTPProvider(config)

    assert provider.config.datasets["daily"].body["params"]["ts_code"] == "${symbols}"
    assert provider.config.datasets["adj_factor"].adj_factor_mode == "cumulative"
    assert provider.validate() == []
    provider.close()
