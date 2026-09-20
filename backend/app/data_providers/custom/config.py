"""Custom HTTP data source configuration."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

logger = logging.getLogger(__name__)

DatasetName = Literal["daily", "adj_factor", "realtime", "minute", "full_minute", "financial"]
# 声明式支持的数据集: 与 loader._sanitize_dataset、provider._REQUIRED 以及
# 前端 DataSourceEditor 的 DATASETS 必须同集 —— 漏一个就会在 YAML 加载时被静默丢弃
# (踩过: full_minute 长期缺在此集合里, 声明了也不生效, 源无法被路由为全量分钟)。
DECLARABLE_DATASETS = frozenset({
    "daily", "adj_factor", "realtime", "minute", "full_minute", "financial",
})
DEFAULT_TIMEOUT = 30.0
MAX_TIMEOUT = 300.0
# 复权因子口径: single = 上游直接给单事件比值(默认); cumulative = 上游给累积因子,
# 本项目按 adj(D)/adj(D-1) 换算成单事件比值(见 normalizer.cumulative_adj_factors_to_events)。
ADJ_FACTOR_MODES = ("single", "cumulative")


@dataclass(frozen=True)
class AuthConfig:
    type: str = "none"
    token_env: str | None = None
    header: str = "Authorization"
    param: str = "token"


@dataclass(frozen=True)
class DatasetConfig:
    url: str
    method: str = "GET"
    batch: int | None = None
    rpm: int | None = None
    timeout: float = DEFAULT_TIMEOUT
    response_path: str = ""
    field_map: dict[str, str] = field(default_factory=dict)
    transforms: dict[str, str] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    body: dict[str, Any] = field(default_factory=dict)
    symbols_param: str = "symbols"
    start_param: str = "start_time"
    end_param: str = "end_time"
    asset_type_param: str | None = None
    freq_param: str | None = None
    # realtime 比例字段(change_pct/amplitude/turnover_rate)的单位声明:
    # "percent"(返回 3.66 表示 3.66%)或 "decimal"(返回 0.0366 表示 3.66%)。
    pct_unit: str | None = None
    # adj_factor 专用: 上游因子是单事件比值还是累积值(默认 single)。
    adj_factor_mode: str = "single"
    # financial 专用: 内部表名 → 上游取值(参数值/接口名)。
    # 内部固定五张表(metrics/income/balance_sheet/cash_flow/shares), 各供应商命名不同,
    # 且不少网关把接口名放在请求体里(Tushare 的 api_name), 故需显式映射;
    # 未列出的表名原样传给上游。
    table_map: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CustomSourceConfig:
    name: str
    display_name: str
    auth: AuthConfig = field(default_factory=AuthConfig)
    datasets: dict[str, DatasetConfig] = field(default_factory=dict)
    path: Path | None = None

    def has_dataset(self, name: DatasetName) -> bool:
        return name in self.datasets


def _auth_from_dict(raw: dict[str, Any] | None) -> AuthConfig:
    raw = raw or {}
    return AuthConfig(
        type=str(raw.get("type", "none") or "none").lower(),
        token_env=raw.get("token_env"),
        header=str(raw.get("header", "Authorization") or "Authorization"),
        param=str(raw.get("param", "token") or "token"),
    )


def _dataset_from_dict(raw: dict[str, Any]) -> DatasetConfig:
    timeout_raw = raw.get("timeout")
    if timeout_raw is None:
        timeout = DEFAULT_TIMEOUT
    else:
        try:
            timeout = float(timeout_raw)
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"timeout must be a number between 0 and {MAX_TIMEOUT:g} seconds"
            ) from e
        if not 0 < timeout <= MAX_TIMEOUT:
            raise ValueError(f"timeout must be between 0 and {MAX_TIMEOUT:g} seconds")

    pct_unit = str(raw.get("pct_unit") or "").strip().lower() or None
    if pct_unit not in (None, "percent", "decimal"):
        raise ValueError(f"pct_unit must be 'percent' or 'decimal', got {pct_unit!r}")

    adj_mode = str(raw.get("adj_factor_mode") or "single").strip().lower() or "single"
    if adj_mode not in ADJ_FACTOR_MODES:
        raise ValueError(
            f"adj_factor_mode must be one of {', '.join(ADJ_FACTOR_MODES)}, got {adj_mode!r}"
        )

    table_map_raw = raw.get("table_map") or {}
    if not isinstance(table_map_raw, dict):
        raise ValueError("table_map must be a mapping of {内部表名: 上游取值}")
    table_map = {
        str(key).strip(): str(value).strip()
        for key, value in table_map_raw.items()
        if str(key).strip() and str(value).strip()
    }

    return DatasetConfig(
        url=str(raw.get("url", "") or ""),
        method=str(raw.get("method", "GET") or "GET").upper(),
        batch=int(raw["batch"]) if raw.get("batch") is not None else None,
        rpm=int(raw["rpm"]) if raw.get("rpm") is not None else None,
        timeout=timeout,
        response_path=str(raw.get("response_path", "") or ""),
        field_map={str(k): str(v) for k, v in (raw.get("field_map") or {}).items()},
        transforms={str(k): str(v) for k, v in (raw.get("transforms") or {}).items()},
        params=dict(raw.get("params") or {}),
        body=dict(raw.get("body") or {}),
        symbols_param=str(raw.get("symbols_param", "symbols") or "symbols").strip() or "symbols",
        start_param=str(raw.get("start_param", "start_time") or "start_time").strip() or "start_time",
        end_param=str(raw.get("end_param", "end_time") or "end_time").strip() or "end_time",
        asset_type_param=(str(raw.get("asset_type_param") or "").strip() or None),
        freq_param=(str(raw.get("freq_param") or "").strip() or None),
        pct_unit=pct_unit,
        adj_factor_mode=adj_mode,
        table_map=table_map,
    )


def config_from_dict(raw: dict[str, Any], path: Path | None = None) -> CustomSourceConfig:
    declared = raw.get("datasets") or {}
    unknown = sorted(
        name for name, cfg in declared.items()
        if isinstance(cfg, dict) and name not in DECLARABLE_DATASETS
    )
    if unknown:
        # 静默丢弃会让用户以为"数据集已声明"但路由里根本不存在(拼错 full_minute 这类),
        # 必须留一条可定位的告警。
        logger.warning(
            "自定义源 %s: 忽略不支持的数据集 %s(支持: %s)",
            raw.get("name") or (path.stem if path else "preview"),
            ", ".join(unknown),
            ", ".join(sorted(DECLARABLE_DATASETS)),
        )
    datasets = {
        name: _dataset_from_dict(cfg)
        for name, cfg in declared.items()
        if name in DECLARABLE_DATASETS and isinstance(cfg, dict)
    }
    default_name = path.stem if path else "preview"
    name = str(raw.get("name", default_name) or default_name).lower()
    return CustomSourceConfig(
        name=name,
        display_name=str(raw.get("display_name", name) or name),
        auth=_auth_from_dict(raw.get("auth")),
        datasets=datasets,
        path=path,
    )


def load_config(path: Path) -> CustomSourceConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return config_from_dict(raw, path)
