"""扶摇「全量分钟」(全市场快照轮询合成) 契约测试。

覆盖:
- ``minute_synth.SnapshotMinuteSynthesizer``: 桶归属(区间起点分钟)、同分钟累加、
  无成交不产出、跨午休归到恢复首分钟、断档整段丢弃、换日重置、累计量回退、
  开盘首分钟种子(含集合竞价量)、陈旧快照整轮不采信、保留窗口裁剪;
- ``FuyaoProvider.get_intraday_batch``: canonical 帧契约(列/单位/北京墙钟)、软失败、
  仅修复轮语义(不实现 get_intraday_latest);
- 真实边界层 ``kline_sync.fetch_intraday_custom_batch``(时区守卫)+
  ``_write_minute_partition`` 落盘;
- ``MinuteRefreshService`` 端到端: 路由到本源的一轮修复轮写盘与状态。

不依赖真实网络与 API Key: FakeSnapshotClient 注入, 时钟与快照页由用例显式给出。
"""
from __future__ import annotations

from datetime import datetime

import polars as pl
import pytest

from app.plugins.fuyao import client as fc
from app.plugins.fuyao import minute_synth as ms
from app.plugins.fuyao import provider as fp
from app.plugins.fuyao.provider import FuyaoProvider
from app.services.minute_refresh import MinuteRefreshService

DAY = (2026, 9, 18)  # 周五


def at(hour: int, minute: int, second: int = 0) -> datetime:
    """测试用北京墙钟(naive)。"""
    return datetime(*DAY, hour, minute, second)


def next_day_at(hour: int, minute: int, second: int = 0) -> datetime:
    """下一交易日(2026-09-21 周一)同一时刻 — 用于换日用例。"""
    return datetime(2026, 9, 21, hour, minute, second)


def row(
    symbol: str = "600519.SH",
    price: float = 10.0,
    shares: float = 1_000_000,
    amount: float = 10_000_000.0,
) -> dict:
    """快照原始行: volume 单位**股**, turnover 单位**元**(扶摇实测口径)。"""
    return {"thscode": symbol, "last_price": price, "volume": shares, "turnover": amount}


# ── 合成内核 ────────────────────────────────────────────────────────


def test_seed_round_emits_nothing_then_bucket_by_interval_start_minute():
    s = ms.SnapshotMinuteSynthesizer()
    assert s.update([row()], at(10, 0, 20)) == []  # 首轮只建基线

    bars = s.update(
        [row(price=10.10, shares=1_000_600, amount=10_006_100.0)], at(10, 1, 25),
    )
    assert len(bars) == 1
    b = bars[0]
    assert b["symbol"] == "600519.SH"
    assert b["datetime"] == at(10, 0)  # 区间起点分钟 (60s 轮询 ≈93% 的成交时间在此)
    assert (b["open"], b["high"], b["low"], b["close"]) == (10.0, 10.10, 10.0, 10.10)
    assert b["volume"] == pytest.approx(6.0)  # 600 股 → 6 手
    assert b["amount"] == pytest.approx(6100.0)


def test_volume_keeps_fractional_hands():
    """不逐分钟 floor: 否则全天累计会系统性偏小 (12345 股 = 123.45 手)。"""
    s = ms.SnapshotMinuteSynthesizer()
    s.update([row()], at(10, 0, 20))
    bars = s.update([row(shares=1_012_345, amount=10_123_450.0)], at(10, 1, 25))
    assert bars[0]["volume"] == pytest.approx(123.45)


def test_same_minute_samples_accumulate_into_one_bar():
    s = ms.SnapshotMinuteSynthesizer()
    s.update([row()], at(10, 0, 20))
    s.update([row(price=10.05, shares=1_000_200, amount=10_002_100.0)], at(10, 0, 45))
    bars = s.update([row(price=10.02, shares=1_000_300, amount=10_003_010.0)], at(10, 0, 58))
    assert len(bars) == 1
    b = bars[0]
    assert b["datetime"] == at(10, 0)
    assert b["open"] == 10.0          # 桶内首次观测价, 不随后续采样改写
    assert b["close"] == 10.02
    assert (b["high"], b["low"]) == (10.05, 10.0)
    assert b["volume"] == pytest.approx(3.0)  # 200 + 100 股
    assert b["amount"] == pytest.approx(3010.0)


def test_no_trade_interval_emits_nothing_but_advances_baseline():
    s = ms.SnapshotMinuteSynthesizer()
    s.update([row()], at(10, 0, 20))
    assert s.update([row()], at(10, 1, 25)) == []  # 无成交且价未变 → 不产出空杆

    bars = s.update([row(price=10.2, shares=1_000_500, amount=10_005_000.0)], at(10, 2, 30))
    assert [b["datetime"] for b in bars] == [at(10, 1)]
    assert bars[0]["volume"] == pytest.approx(5.0)  # 只计本区间增量


def test_lunch_interval_lands_on_resume_minute():
    """跨午休: 区间含闭市时间 → 归到恢复后的首分钟。

    若归到起点分钟, 会把**午后开盘价写进上午最后一根**(时序倒错), 故取当前分钟。
    """
    s = ms.SnapshotMinuteSynthesizer()
    s.update([row(price=10.0, shares=5_000_000, amount=50_000_000.0)], at(11, 29, 30))
    bars = s.update([row(price=11.0, shares=5_040_000, amount=50_440_000.0)], at(13, 0, 30))
    assert [b["datetime"] for b in bars] == [at(13, 0)]
    b = bars[0]
    assert (b["open"], b["close"]) == (10.0, 11.0)
    assert b["volume"] == pytest.approx(400.0)  # 40_000 股 — 一份 ≤1 个轮询间隔的量
    assert s.stats()["dropped_intervals"] == 0


def test_gap_across_unobserved_minutes_is_dropped():
    """断档: 区间跨 5 个未观测的连续竞价分钟 → 整段丢弃(不把多分钟量塞进一根)。"""
    s = ms.SnapshotMinuteSynthesizer()
    s.update([row()], at(10, 0, 20))
    assert s.update([row(price=10.5, shares=1_200_000, amount=12_000_000.0)], at(10, 5, 30)) == []
    assert s.stats()["dropped_intervals"] == 1

    bars = s.update([row(price=10.6, shares=1_201_000, amount=12_010_000.0)], at(10, 6, 35))
    assert [b["datetime"] for b in bars] == [at(10, 5)]  # 重置基线后按新基线计增量
    assert bars[0]["volume"] == pytest.approx(10.0)
    assert bars[0]["open"] == 10.5


def test_new_day_resets_baseline_and_rebuilds_open_minute():
    s = ms.SnapshotMinuteSynthesizer()
    s.update([row(shares=1_000_000)], at(14, 59, 0))
    s.update([row(price=10.5, shares=1_010_000, amount=10_100_000.0)], at(15, 0, 0))

    # 下一交易日 09:30: 当日累计量自 0 起算 → 种子样本即整根 09:30(含集合竞价)
    bars = s.update([row(price=10.5, shares=12_000, amount=126_000.0)], next_day_at(9, 30, 10))
    assert [b["datetime"] for b in bars] == [next_day_at(9, 30)]
    b = bars[0]
    assert b["volume"] == pytest.approx(120.0)  # 12_000 股, 不是与昨日的差
    assert b["datetime"].date() == next_day_at(9, 30).date()


def test_open_minute_seed_without_auction_volume_emits_nothing():
    s = ms.SnapshotMinuteSynthesizer()
    assert s.update([row(shares=0, amount=0.0)], at(9, 30, 5)) == []


def test_cumulative_volume_regression_only_reseeds():
    """上游累计量回退(重置/换日未识别) → 只重置基线, 不产出负量。"""
    s = ms.SnapshotMinuteSynthesizer()
    s.update([row(shares=1_000_000)], at(10, 0, 20))
    assert s.update([row(price=9.9, shares=100, amount=990.0)], at(10, 1, 25)) == []

    bars = s.update([row(price=9.95, shares=200, amount=1_990.0)], at(10, 2, 30))
    assert [b["datetime"] for b in bars] == [at(10, 1)]
    assert bars[0]["volume"] == pytest.approx(1.0)  # 100 股增量


def test_stale_server_timestamp_skips_whole_round():
    """服务端时间戳陈旧(如开盘瞬间拿到隔日快照) → 整轮不采信, 基线不动。"""
    s = ms.SnapshotMinuteSynthesizer()
    fresh = int(at(10, 0, 20).replace(tzinfo=ms.CN_TZ).timestamp() * 1000)
    s.update([row()], at(10, 0, 20), server_ts=fresh)

    stale = fresh - 3_600_000
    assert s.update([row(price=11.0, shares=2_000_000, amount=22_000_000.0)], at(10, 1, 25), server_ts=stale) == []
    assert s.stats()["stale_rounds"] == 1

    bars = s.update([row(price=10.1, shares=1_000_100, amount=10_001_000.0)], at(10, 1, 25), server_ts=fresh + 65_000)
    assert [b["datetime"] for b in bars] == [at(10, 0)]  # 基线未被陈旧轮推进
    assert bars[0]["volume"] == pytest.approx(1.0)


def test_keep_window_prunes_old_buckets():
    s = ms.SnapshotMinuteSynthesizer()
    shares = 1_000_000.0
    s.update([row(shares=shares)], at(10, 0, 20))
    bars: list[dict] = []
    for i in range(1, 6):
        shares += 100
        bars = s.update([row(price=10.0 + i * 0.01, shares=shares, amount=shares)], at(10, i, 25))
    assert [b["datetime"] for b in bars] == [at(10, 2), at(10, 3), at(10, 4)]
    assert len(bars) == ms.KEEP_BUCKETS


def test_max_buckets_caps_emitted_rows():
    s = ms.SnapshotMinuteSynthesizer()
    shares = 1_000_000.0
    s.update([row(shares=shares)], at(10, 0, 20))
    for i in range(1, 4):
        shares += 100
        bars = s.update(
            [row(shares=shares, amount=shares)], at(10, i, 25), max_buckets=1,
        )
    assert [b["datetime"] for b in bars] == [at(10, 2)]


def test_rows_without_price_or_volume_are_skipped():
    s = ms.SnapshotMinuteSynthesizer()
    s.update([row()], at(10, 0, 20))
    bars = s.update(
        [
            {"thscode": "600519.SH", "last_price": None, "volume": 1_000_100},
            {"thscode": "600519.SH", "last_price": 10.1, "volume": None},
            row(symbol="000001.SZ", price=5.0, shares=10, amount=50.0),
        ],
        at(10, 1, 25),
    )
    assert bars == []  # 前者被跳过, 后者是新标的(只建基线, 不在开盘分钟故无杆)
    assert s.stats()["skipped_rows"] == 2


def test_symbols_filter_limits_tracking():
    s = ms.SnapshotMinuteSynthesizer()
    s.update([row(), row("000001.SZ", price=5.0, shares=100, amount=500.0)], at(10, 0, 20), symbols=["600519.SH"])
    bars = s.update(
        [
            row(price=10.1, shares=1_000_100, amount=10_001_000.0),
            row("000001.SZ", price=5.1, shares=200, amount=1_020.0),
        ],
        at(10, 1, 25),
        symbols=["600519.SH"],
    )
    assert {b["symbol"] for b in bars} == {"600519.SH"}


# ── Provider 契约 ───────────────────────────────────────────────────


class _FakeSnapshotClient:
    """按用例推入的 (采样时刻, 快照行) 逐轮返回; server_ts 由采样时刻推导保证新鲜。"""

    def __init__(self) -> None:
        self.pages: list[tuple[datetime, list[dict]]] = []
        self.now: datetime | None = None
        self.error: Exception | None = None
        self.calls = 0

    def snapshot_all(self) -> tuple[list[dict], int]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        now, rows = self.pages.pop(0)
        return rows, int(now.replace(tzinfo=ms.CN_TZ).timestamp() * 1000)

    def close(self) -> None:
        pass


def _provider(monkeypatch) -> tuple[FuyaoProvider, _FakeSnapshotClient]:
    fake = _FakeSnapshotClient()
    monkeypatch.setattr(fp, "fuyao_client", type("M", (), {"FuyaoClient": lambda **kw: fake}))
    monkeypatch.setattr(fp, "get_api_key", lambda: "test-key")
    monkeypatch.setattr(ms, "now_wallclock", lambda: fake.now)
    return FuyaoProvider(), fake


def _poll(provider, fake, now: datetime, rows: list[dict]) -> pl.DataFrame:
    """一轮 = (采样时刻, 全市场快照行) → provider 合成帧。"""
    fake.now = now
    fake.pages.append((now, rows))
    return provider.get_intraday_batch()


def test_get_intraday_batch_frame_contract(monkeypatch):
    provider, fake = _provider(monkeypatch)
    assert _poll(provider, fake, at(10, 0, 20), [row()]).is_empty()  # 种子轮

    df = _poll(provider, fake, at(10, 1, 25), [row(price=10.1, shares=1_000_600, amount=10_006_100.0)])
    assert df.columns == ms.CANONICAL_COLUMNS
    assert df.schema["symbol"] == pl.Utf8
    assert df.schema["datetime"] == pl.Datetime("us")
    assert df.schema["datetime"].time_zone is None  # 北京墙钟 naive (时区契约)
    assert df.schema["volume"] == pl.Float64
    assert df["datetime"].to_list() == [at(10, 0)]
    assert df["volume"].to_list() == [6.0]
    assert df["amount"].to_list() == [6100.0]
    assert df["open"].to_list() == [10.0] and df["close"].to_list() == [10.1]


def test_get_intraday_batch_soft_fails_with_empty_canonical_frame(monkeypatch):
    provider, fake = _provider(monkeypatch)
    fake.error = fc.FuyaoError("扶摇接口错误 code=4001: 频率超限")
    out = provider.get_intraday_batch()
    assert out.is_empty() and out.columns == ms.CANONICAL_COLUMNS


def test_declares_full_minute_but_not_minute():
    """声明 full_minute(快照合成), 仍不声明 minute → 分时/分钟回测仍走 TickFlow。"""
    datasets = FuyaoProvider().config.datasets
    assert "full_minute" in datasets
    assert "minute" not in datasets


def test_provider_is_repair_only_via_missing_latest(monkeypatch):
    """不实现 get_intraday_latest → 服务按仅修复轮调度(60s 节奏下限, 上游友好)。"""
    provider, _ = _provider(monkeypatch)
    assert getattr(provider, "get_intraday_latest", None) is None
    assert MinuteRefreshService._custom_supports_increment(None, provider) is False


def test_test_dataset_full_minute_preview_and_note(monkeypatch):
    import json

    provider, fake = _provider(monkeypatch)
    _poll(provider, fake, at(10, 0, 20), [row()])
    fake.now = at(10, 1, 25)
    fake.pages.append((at(10, 1, 25), [row(price=10.1, shares=1_000_600, amount=10_006_100.0)]))

    out = provider.test_dataset("full_minute")
    assert fake.calls == 2
    assert out["dataset"] == "full_minute"
    assert out["rows"] >= 1
    assert "采样近似" in out["note"]
    assert out["preview"][0]["datetime"] == at(10, 0).isoformat()
    json.dumps(out)  # 必须可 JSON 序列化 (datetime 已转 ISO)


def test_test_dataset_minute_still_reports_fallback():
    out = FuyaoProvider().test_dataset("minute")
    assert out["rows"] == 0
    assert "未接入" in out["error"]


# ── 真实边界层 + 落盘 ───────────────────────────────────────────────


def test_boundary_layer_and_partition_write(tmp_path, monkeypatch):
    """过 kline_sync 边界包装(时区守卫) 后落盘, unique(symbol,datetime) 合并。"""
    from app.services import kline_sync

    provider, fake = _provider(monkeypatch)
    _poll(provider, fake, at(10, 0, 20), [row()])
    _poll(provider, fake, at(10, 1, 25), [row(price=10.1, shares=1_000_600, amount=10_006_100.0)])
    fake.now = at(10, 2, 30)
    fake.pages.append((at(10, 2, 30), [row(price=10.2, shares=1_000_700, amount=10_007_100.0)]))

    df, requests = kline_sync.fetch_intraday_custom_batch(provider, "fuyao", ["600519.SH"])
    assert requests == 1
    assert fake.calls == 3
    assert df.height == 2  # 保留窗口内的桶(10:00/10:01) 全部重发, 供幂等合并
    assert df["datetime"].to_list() == [at(10, 0), at(10, 1)]

    written = kline_sync._write_minute_partition(df, tmp_path / "kline_minute")
    assert written == 2
    part = tmp_path / "kline_minute" / f"date={at(10, 0).date().isoformat()}" / "part.parquet"
    assert part.exists()
    stored = pl.read_parquet(part)
    assert stored.columns == ms.CANONICAL_COLUMNS
    assert stored["datetime"].to_list() == [at(10, 0), at(10, 1)]
    assert stored["close"].to_list() == [10.1, 10.2]


# ── 服务端到端(修复轮) ──────────────────────────────────────────────


def test_minute_refresh_round_writes_partition(tmp_path, monkeypatch):
    from app.services import kline_sync, minute_refresh, preferences

    monkeypatch.setattr(preferences, "_path", lambda: tmp_path / "preferences.json")
    preferences._invalidate_cache()
    monkeypatch.setattr(preferences, "get_full_minute_data_provider", lambda: "fuyao")

    provider, fake = _provider(monkeypatch)
    monkeypatch.setattr(
        kline_sync, "_resolve_full_minute_provider", lambda name: (provider, False, None),
    )

    class _Repo:
        def __init__(self) -> None:
            self.store = type("S", (), {"data_dir": tmp_path})()

        def get_instruments(self) -> pl.DataFrame:
            return pl.DataFrame({"symbol": ["600519.SH"]})

    class _Caps:
        def has(self, cap) -> bool:
            from app.tickflow.capabilities import Cap

            return cap in (Cap.INTRADAY_BATCH, Cap.INTRADAY_UNIVERSE)

    svc = MinuteRefreshService(_Repo())
    svc.set_app_state(type("A", (), {"capabilities": _Caps()})())
    monkeypatch.setattr(minute_refresh, "_in_continuous_session", lambda now=None: True)
    monkeypatch.setattr(
        MinuteRefreshService, "_today_coverage_lag_minutes", lambda self: None,  # 冷启动 → 修复轮
    )

    fake.now = at(10, 0, 20)
    fake.pages.append((at(10, 0, 20), [row()]))  # 服务第 1 轮 = 种子轮(只建基线 → 0 行)
    svc._run_round()
    assert svc.status()["rounds"] == 0  # 种子轮无产出 → 按空轮处理
    assert "no data" in (svc.status()["last_error"] or "")

    fake.now = at(10, 1, 25)
    fake.pages.append((at(10, 1, 25), [row(price=10.1, shares=1_000_600, amount=10_006_100.0)]))
    svc._run_round()

    assert fake.calls == 2  # 两轮都真打了快照端点(不是异常被吞)

    st = svc.status()
    assert st["rounds"] == 1
    assert st["last_mode"] == "full"
    assert st["provider"] == "fuyao" and st["provider_effective"] == "fuyao"
    assert st["repair_only"] is True
    assert st["last_rows"] == 1 and st["last_symbols"] == 1
    part = tmp_path / "kline_minute" / f"date={at(10, 0).date().isoformat()}" / "part.parquet"
    assert pl.read_parquet(part).height == 1
