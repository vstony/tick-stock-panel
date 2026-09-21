"""回归测试: job 记录跨进程死亡持久化(「数据在、记录丢」补丁)。

背景(用户反馈): 全市场同步 12:11~12:42 成功结束后 0.7s, uvicorn --reload
检测到代码变更杀死 worker, 恰好落在管道完成与 job_store.succeed() 落盘之间
—— 数据已写盘但同步历史无任何记录。旧实现 pending/running 仅存内存、终态才
落盘, 存在整段丢失窗口。

修复后契约:
  - create()/start() 即落盘 pending/running 快照;
  - 下次进程启动(= 新 JobStore 实例, 同目录)把遗留的 pending/running
    孤儿记录补标为 failed(中断), finished_at 取文件 mtime;
  - 终态记录不受补录影响; 终态写入覆盖 running 快照(同一文件)。
均为纯逻辑, 不触网。
"""
from __future__ import annotations

import json
import threading

from app.services import pipeline_jobs
from app.services.pipeline_jobs import JobStore


def _read_disk(d, jid: str) -> dict:
    return json.loads((d / f"{jid}.json").read_text("utf-8"))


# ── 创建/启动即落盘 ──────────────────────────────────────────────────────

def test_create_writes_pending_snapshot_to_disk(tmp_path):
    d = tmp_path / "jobs"
    store = JobStore(store_dir=d)
    jid, _ = store.create(timeout_s=60)

    disk = _read_disk(d, jid)
    assert disk["status"] == "pending"
    assert disk["stage"] == "init"


def test_start_updates_disk_snapshot_to_running(tmp_path):
    d = tmp_path / "jobs"
    store = JobStore(store_dir=d)
    jid, _ = store.create(timeout_s=60)
    store.start(jid)

    disk = _read_disk(d, jid)
    assert disk["status"] == "running"
    assert disk["started_at"] is not None


# ── 进程死亡 → 下次启动补录 ──────────────────────────────────────────────

def test_orphan_running_record_is_reaped_on_next_boot(tmp_path):
    """核心场景: 进程死在 running(甚至工作已做完但未终态), 记录必须可见。"""
    d = tmp_path / "jobs"
    dead = JobStore(store_dir=d)
    jid, _ = dead.create(timeout_s=60)
    dead.start(jid)
    dead.progress(jid, "sync", 50, "halfway")  # 进度只更新内存

    # 新进程 = 同目录新实例(内存为空, 只有磁盘)
    revived = JobStore(store_dir=d)
    j = revived.get(jid)
    assert j is not None
    assert j["status"] == "failed"
    assert "中断" in j["error"]
    assert j["finished_at"] is not None
    # finished_at 基于文件 mtime(≈ start 时刻), 时长不得虚增为负或巨大
    assert j["duration_s"] is not None
    assert 0 <= j["duration_s"] <= 60
    # 同步历史列表可见
    assert any(x["id"] == jid for x in revived.list_recent())


def test_orphan_pending_record_is_reaped(tmp_path):
    """进程死在 create() 与 start() 之间: 记录同样可见, 时长为 None。"""
    d = tmp_path / "jobs"
    dead = JobStore(store_dir=d)
    jid, _ = dead.create(timeout_s=60)
    # 未 start 即死亡

    revived = JobStore(store_dir=d)
    j = revived.get(jid)
    assert j["status"] == "failed"
    assert j["duration_s"] is None


def test_reap_does_not_touch_terminal_records(tmp_path):
    d = tmp_path / "jobs"
    store = JobStore(store_dir=d)
    jid, _ = store.create(timeout_s=60)
    store.start(jid)
    store.succeed(jid, {"daily_rows": 100})

    revived = JobStore(store_dir=d)
    j = revived.get(jid)
    assert j["status"] == "succeeded"
    assert j["result"] == {"daily_rows": 100}


def test_reap_allows_new_job_after_dead_orphan(tmp_path):
    """补录后旧 job 已 failed: 新进程 create() 不被死孤儿阻塞(单飞只看内存)。"""
    d = tmp_path / "jobs"
    dead = JobStore(store_dir=d)
    old_jid, _ = dead.create(timeout_s=60)
    dead.start(old_jid)

    revived = JobStore(store_dir=d)
    new_jid, is_new = revived.create(timeout_s=60)
    assert is_new is True
    assert new_jid != old_jid


# ── 终态覆盖快照 ─────────────────────────────────────────────────────────

def test_terminal_write_replaces_running_snapshot(tmp_path):
    d = tmp_path / "jobs"
    store = JobStore(store_dir=d)
    jid, _ = store.create(timeout_s=60)
    store.start(jid)
    store.fail(jid, "boom")

    files = list(d.glob("*.json"))
    assert len(files) == 1
    disk = _read_disk(d, jid)
    assert disk["status"] == "failed"
    assert disk["error"] == "boom"


# ── 并发读写的完整性(半截文件 = 任务记录凭空消失) ─────────────────────────

def test_write_file_leaves_no_temp_and_replaces_partial_content(tmp_path):
    """写盘走临时文件 + os.replace: 旧内容被完整替换, 不留 .tmp 残留。"""
    d = tmp_path / "jobs"
    store = JobStore(store_dir=d)
    jid, _ = store.create(timeout_s=60)
    (d / f"{jid}.json").write_text('{"id": "x", "status": "run', encoding="utf-8")

    store.start(jid)

    assert list(d.glob("*.tmp")) == []
    assert _read_disk(d, jid)["status"] == "running"


def test_write_file_retries_when_replace_hits_sharing_violation(tmp_path, monkeypatch):
    """原子替换撞 Windows 共享冲突(读者持有句柄)必须重试, 不能丢掉终态快照。

    实测来源: 前端 5ms 轮询读 job 文件时, os.replace 抛 PermissionError, 一次不重试
    就会把终态丢弃 → 任务永远停在 running(tests/test_pipeline_capacity.py 复现)。
    """
    d = tmp_path / "jobs"
    store = JobStore(store_dir=d)
    jid, _ = store.create(timeout_s=60)

    real_replace = pipeline_jobs.os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError(13, "sharing violation")
        return real_replace(src, dst)

    monkeypatch.setattr(pipeline_jobs.os, "replace", flaky_replace)
    monkeypatch.setattr(pipeline_jobs.time, "sleep", lambda _seconds: None)

    store.start(jid)

    assert calls["n"] == 3  # 前两次冲突 → 第三次成功
    assert _read_disk(d, jid)["status"] == "running"
    assert list(d.glob("*.tmp")) == []


def test_write_file_gives_up_after_all_retries_and_logs(tmp_path, monkeypatch, caplog):
    """全部重试都撞冲突时必须告警(而不是静默丢记录), 且不留下 .tmp 残留。"""
    d = tmp_path / "jobs"
    store = JobStore(store_dir=d)
    jid, _ = store.create(timeout_s=60)

    def always_blocked(_src, _dst):
        raise PermissionError(13, "sharing violation")

    monkeypatch.setattr(pipeline_jobs.os, "replace", always_blocked)
    monkeypatch.setattr(pipeline_jobs.time, "sleep", lambda _seconds: None)

    with caplog.at_level("WARNING"):
        store.start(jid)

    assert any("failed to write job file" in r.getMessage() for r in caplog.records)
    assert list(d.glob("*.tmp")) == []


def test_read_file_retries_transient_partial_content(tmp_path, monkeypatch):
    """读到半截 JSON(与写盘竞态)必须重试: 单次失败不得把运行中的任务报成不存在。

    实测来源: tests/test_pipeline_capacity.py 偶发 `store.get(jid)` → None → TypeError。
    """
    d = tmp_path / "jobs"
    store = JobStore(store_dir=d)
    jid, _ = store.create(timeout_s=60)
    store.start(jid)
    store.succeed(jid, {"rows": 1})  # 终态后记录已从内存释放 → get() 走磁盘

    real_loads = pipeline_jobs.json.loads
    calls = {"n": 0}

    def flaky_loads(text):
        calls["n"] += 1
        if calls["n"] == 1:
            raise json.JSONDecodeError("Expecting value", text, 0)
        return real_loads(text)

    monkeypatch.setattr(pipeline_jobs.json, "loads", flaky_loads)
    monkeypatch.setattr(pipeline_jobs.time, "sleep", lambda _seconds: None)

    record = store.get(jid)

    assert record is not None
    assert record["status"] == "succeeded"
    assert calls["n"] == 2  # 第一次失败 → 重试成功


def test_concurrent_read_and_write_never_loses_record(tmp_path):
    """并发读写下记录始终可读(修复前: 截断写盘 → 读者读到半截 JSON → None)。"""
    d = tmp_path / "jobs"
    store = JobStore(store_dir=d)
    jid, _ = store.create(timeout_s=60)

    stop = threading.Event()
    failures: list[str] = []

    def reader():
        while not stop.is_set():
            try:
                record = store.get(jid)
            except Exception as e:  # 任何异常都算记录不可读
                failures.append(repr(e))
                return
            if record is None:
                failures.append("None(记录读不到)")
                return

    readers = [threading.Thread(target=reader, daemon=True) for _ in range(3)]
    for t in readers:
        t.start()
    try:
        for _ in range(200):  # start() 每次都会重写 running 快照
            store.start(jid)
    finally:
        stop.set()
        for t in readers:
            t.join(timeout=2)

    assert failures == []
