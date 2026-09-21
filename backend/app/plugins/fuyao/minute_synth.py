"""扶摇全量分钟的合成内核: 把全市场快照的轮询序列还原成 1 分钟 K。

为什么需要合成 (2026-09 实测, 结论同步写在 docs/configuration.md 与
docs/plugin-development.md): 扶摇没有全市场分钟端点 —

- ``/api/a-share/prices/snapshot`` 是"最新行情快照": 每只一行、无时间序列
  (只有 last_price / open / high / low / prev_price / volume / turnover),
  传 ``interval`` 等未知参数会被服务端**静默忽略**(仍 HTTP 200 code=0);
- ``/api/a-share/prices/historical`` 的 ``interval`` 目前**仅支持 1d**
  (传 1m → code=1002 Invalid parameter format);
- 唯一带 1 分钟的是"高频动向" ``/api/a-share/high-frequency/{historical,intraday}``,
  但**单标的**(不接受逗号批量)、仅最近 30 交易日, 字段为 hf_direction /
  hf_participation(**不是 OHLCV**), 且实测 code=2004「该数据为同花顺AI客户端专用」
  (文档明确暂未开放外部接入);
- 全市场 dump 只有 daily-k / daily-k-10d / adjustment-factors, **无分钟 dump**。

于是"扶摇侧全量分钟"的唯一来源是: 反复拉全市场快照(1 请求 ≈ 5575 行, 当日累计
成交量额), 用相邻两轮的增量还原每根分钟 K。本模块只做这件合成, 不碰网络。

口径 (与 TickFlow/Tushare 分钟一致): volume 单位**手**, amount 单位**元**,
datetime 为北京墙钟 naive。

真实性边界(不伪造, 与 CONTRIBUTING §3.3「缺失即缺失」一致):
- ``close`` = 桶结束时最新采样价, ``open`` = 桶起点的采样价 → 都是**真实观测价**;
  ``high``/``low`` = 桶内采样价极值 → **采样近似**, 不声称覆盖桶内真实极值
  (60s 轮询时每桶 1 次采样, H/L 即 O/C 两点区间);
- 无成交的分钟**不产出**(不补零、不平推); 停牌标的整日无杆;
- 冷启动前的分钟、断档跨越的分钟**不产出也不插值** → 缺口交给盘后分钟同步
  (`minute` 数据集) 或 TickFlow 兜底;
- ``volume`` = 增量股 / 100, 保留 2 位小数(不 floor: 逐分钟 floor 会让全天累计
  系统性偏小, 而分钟量常被策略按比例使用)。

桶归属(时间轴对齐):
- 常规区间按**区间起点分钟**归档 — 60s 轮询下区间 ≈93% 的成交时间落在起点分钟内;
- 区间含**闭市**时间(午休/收盘后)时, 区间内成交全部落在恢复后的首分钟(闭市不成交),
  故归到当前分钟 — 代价是上午/午后交界会有一份 ≤ 一个轮询间隔的量被计到交界后那根。
  之所以不能归到起点分钟: 那样会把**午后开盘价写进上午最后一根**(时序倒错),
  而缺一根开盘杆比量轻微偏移更伤;
- 区间跨越了**未观测的连续竞价分钟**(网络失败 / 进程暂停 / 轮询间隔配置过大)
  则整段丢弃, 只重置基线 —— 把多分钟的量塞进一根会造出假量。
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from datetime import time as dt_time

from app.market_time import CN_TZ, cn_now, trading_minutes_elapsed_from_dt

logger = logging.getLogger(__name__)

CANONICAL_COLUMNS = ["symbol", "datetime", "open", "high", "low", "close", "volume", "amount"]

# 开盘首分钟 (09:30): 当日累计量自 0 起算 → 种子样本可整根还原(含 09:25 集合竞价)
SESSION_OPEN_MINUTE = dt_time(9, 30)
# 区间可跨越的连续竞价分钟数上限: 60s 轮询 ≈1.0, 90s ≈1.5; 超过即判定为断档
MAX_INTERVAL_TRADING_MINUTES = 1.6
# 区间内"含闭市时间"的判据: 连续竞价时段内 wall 间隙与已交易分钟数**恒定相等**
# (采样延迟同时抬高两者), 两者之差 > 此值 ⇔ 区间里含了午休/收盘后的空档
CLOSED_GAP_EPS_MINUTES = 0.5
# 每标的保留的桶数: 与 full_minute 契约的 count=3 对齐, 每轮重发供幂等合并
KEEP_BUCKETS = 3
SHARES_PER_HAND = 100.0
# 快照服务端时间戳与本机时间相差超过此值 → 整轮不采信
# (防"隔日/陈旧快照"被当成今日累计: 09:30 开盘瞬间尤其关键)
STALE_SNAPSHOT_MS = 5 * 60 * 1000


def now_wallclock() -> datetime:
    """当前北京墙钟 (naive) — 抽成函数便于测试注入时钟。"""
    return cn_now().replace(tzinfo=None)


def _to_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(slots=True)
class _Bucket:
    """一根仍在累计的分钟 K (volume 手 / amount 元)。"""

    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    amount: float = 0.0


@dataclass(slots=True)
class _SymbolState:
    """单标的的采样基线 + 当日分钟缓冲。"""

    day: date
    last_at: datetime      # 上一次采样的北京墙钟
    last_price: float
    cum_shares: float      # 当日累计成交量 (股)
    cum_amount: float      # 当日累计成交额 (元)
    buckets: dict[datetime, _Bucket] = field(default_factory=dict)


class SnapshotMinuteSynthesizer:
    """线程安全的快照→分钟K 合成器 (provider 单例持有一份状态)。"""

    def __init__(
        self,
        *,
        keep_buckets: int = KEEP_BUCKETS,
        max_interval_minutes: float = MAX_INTERVAL_TRADING_MINUTES,
        stale_snapshot_ms: int = STALE_SNAPSHOT_MS,
    ) -> None:
        self.keep_buckets = max(1, int(keep_buckets))
        self.max_interval_minutes = float(max_interval_minutes)
        self.stale_snapshot_ms = int(stale_snapshot_ms)
        self._lock = threading.Lock()
        self._state: dict[str, _SymbolState] = {}
        self.rounds = 0
        self.stale_rounds = 0
        self.dropped_intervals = 0
        self.skipped_rows = 0

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def update(
        self,
        rows: Iterable[Mapping],
        now: datetime | None = None,
        *,
        server_ts: int = 0,
        symbols: Sequence[str] | None = None,
        max_buckets: int | None = None,
    ) -> list[dict]:
        """喂入一轮全市场快照行, 返回保留窗口内的分钟K行 (canonical 列)。

        rows: 快照原始行 (thscode / last_price / volume 股 / turnover 元)。
        now: 采样时刻(北京墙钟, 带不带时区都可); 缺省取当前时间。
        server_ts: 快照信封里的服务端时间戳(毫秒), 0/缺失则不校验新鲜度。
        symbols: 只跟踪给定标的 (None = 全部行)。
        max_buckets: 每标的返回的桶数上限 (缺省用 keep_buckets)。
        """
        now = self._wallclock(now)
        minute = now.replace(second=0, microsecond=0)
        day = now.date()
        wanted = set(symbols) if symbols else None
        with self._lock:
            self.rounds += 1
            if self._is_stale(now, server_ts):
                self.stale_rounds += 1
                logger.warning(
                    "扶摇分钟合成: 本轮快照陈旧(服务端 %s, 本机 %s), 整轮不采信 — "
                    "不把隔日/陈旧累计量当成当日增量",
                    server_ts, int(now.replace(tzinfo=CN_TZ).timestamp() * 1000),
                )
                return []
            elapsed_now = trading_minutes_elapsed_from_dt(now)
            dropped_before = self.dropped_intervals
            for row in rows:
                symbol = row.get("thscode")
                if not symbol or (wanted is not None and symbol not in wanted):
                    continue
                price = _to_float(row.get("last_price"))
                cum_shares = _to_float(row.get("volume"))
                cum_amount = _to_float(row.get("turnover"))
                if price is None or price <= 0 or cum_shares is None or cum_shares < 0:
                    self.skipped_rows += 1
                    continue
                self._observe(
                    str(symbol), price, cum_shares,
                    0.0 if cum_amount is None else cum_amount,
                    now, minute, day, elapsed_now,
                )
            dropped_now = self.dropped_intervals - dropped_before
            if dropped_now:
                # 每轮汇总一条(断档时全市场同时命中, 逐标的告警会淹日志)
                logger.warning(
                    "扶摇分钟合成: 本轮丢弃 %d 个标的的断档区间(跨越未观测的连续竞价分钟) — "
                    "缺口不补, 由盘后分钟同步/TickFlow 兜底; 若持续出现请把「全量分钟刷新间隔」"
                    "调到 60s(本源单区间最多跨 %.1f 个竞价分钟, 约 %ds)",
                    dropped_now, self.max_interval_minutes, int(self.max_interval_minutes * 60),
                )
            return self._emit(max_buckets)

    def reset(self) -> None:
        with self._lock:
            self._state.clear()
            self.rounds = self.stale_rounds = 0
            self.dropped_intervals = self.skipped_rows = 0

    def stats(self) -> dict:
        """运行计数(供日志/试拉展示, 不参与业务逻辑)。"""
        with self._lock:
            return {
                "rounds": self.rounds,
                "tracked": len(self._state),
                "buckets": sum(len(s.buckets) for s in self._state.values()),
                "stale_rounds": self.stale_rounds,
                "dropped_intervals": self.dropped_intervals,
                "skipped_rows": self.skipped_rows,
            }

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    @staticmethod
    def _wallclock(now: datetime | None) -> datetime:
        """统一成北京墙钟 naive (带时区的输入按上海口径折平)。"""
        if now is None:
            return now_wallclock()
        if now.tzinfo is not None:
            return now.astimezone(CN_TZ).replace(tzinfo=None)
        return now

    def _is_stale(self, now: datetime, server_ts: int) -> bool:
        if not server_ts or self.stale_snapshot_ms <= 0:
            return False
        try:
            delta = abs(int(now.replace(tzinfo=CN_TZ).timestamp() * 1000) - int(server_ts))
        except (TypeError, ValueError, OSError):
            return False
        return delta > self.stale_snapshot_ms

    def _seed(
        self, now: datetime, minute: datetime, day: date,
        price: float, cum_shares: float, cum_amount: float,
    ) -> _SymbolState:
        """建立/重建基线。开盘首分钟可整根还原(当日累计量自 0 起算)。"""
        state = _SymbolState(
            day=day, last_at=now, last_price=price,
            cum_shares=cum_shares, cum_amount=cum_amount,
        )
        if minute.time() == SESSION_OPEN_MINUTE and cum_shares > 0:
            state.buckets[minute] = _Bucket(
                open=price, high=price, low=price, close=price,
                volume=cum_shares / SHARES_PER_HAND, amount=cum_amount,
            )
        return state

    def _observe(
        self,
        symbol: str,
        price: float,
        cum_shares: float,
        cum_amount: float,
        now: datetime,
        minute: datetime,
        day: date,
        elapsed_now: float,
    ) -> None:
        state = self._state.get(symbol)
        if state is None or state.day != day or cum_shares + 1e-9 < state.cum_shares:
            # 冷启动 / 换日 / 累计量回退(上游重置) → 只重置基线, 不产出
            self._state[symbol] = self._seed(now, minute, day, price, cum_shares, cum_amount)
            return

        gap_s = (now - state.last_at).total_seconds()
        elapsed = elapsed_now - trading_minutes_elapsed_from_dt(state.last_at)
        if elapsed > self.max_interval_minutes:
            # 断档: 区间跨越未观测的连续竞价分钟 (网络失败/进程暂停/轮询间隔配大了)
            self.dropped_intervals += 1
            self._state[symbol] = self._seed(now, minute, day, price, cum_shares, cum_amount)
            return

        # 区间内含闭市时间(午休/收盘后) ⇔ wall 间隙明显大于已交易分钟数
        closed = (gap_s / 60.0) - elapsed > CLOSED_GAP_EPS_MINUTES
        # 闭市不成交 → 区间内成交全落在恢复后的首分钟; 否则归区间起点分钟
        bucket = minute if closed else state.last_at.replace(second=0, microsecond=0)
        self._accumulate(
            state, bucket,
            prev_price=state.last_price, price=price,
            d_shares=cum_shares - state.cum_shares,
            d_amount=cum_amount - state.cum_amount,
        )
        state.last_at = now
        state.last_price = price
        state.cum_shares = cum_shares
        state.cum_amount = cum_amount
        self._prune(state)

    def _accumulate(
        self,
        state: _SymbolState,
        bucket: datetime,
        *,
        prev_price: float,
        price: float,
        d_shares: float,
        d_amount: float,
    ) -> None:
        bar = state.buckets.get(bucket)
        if bar is None and d_shares <= 0 and d_amount <= 0 and price == prev_price:
            return  # 该分钟无成交且价未变 → 不产出空杆
        if bar is None:
            state.buckets[bucket] = _Bucket(
                open=prev_price,
                high=max(prev_price, price),
                low=min(prev_price, price),
                close=price,
                volume=max(d_shares, 0.0) / SHARES_PER_HAND,
                amount=max(d_amount, 0.0),
            )
            return
        bar.high = max(bar.high, prev_price, price)
        bar.low = min(bar.low, prev_price, price)
        bar.close = price
        bar.volume += max(d_shares, 0.0) / SHARES_PER_HAND
        bar.amount += max(d_amount, 0.0)

    def _prune(self, state: _SymbolState) -> None:
        extra = len(state.buckets) - self.keep_buckets
        if extra > 0:
            for key in sorted(state.buckets)[:extra]:
                del state.buckets[key]

    def _emit(self, max_buckets: int | None) -> list[dict]:
        cap = self.keep_buckets if max_buckets is None else max(1, min(int(max_buckets), self.keep_buckets))
        out: list[dict] = []
        for symbol in sorted(self._state):
            buckets = self._state[symbol].buckets
            for minute in sorted(buckets)[-cap:]:
                bar = buckets[minute]
                out.append({
                    "symbol": symbol,
                    "datetime": minute,
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": round(bar.volume, 2),
                    "amount": round(bar.amount, 2),
                })
        return out
