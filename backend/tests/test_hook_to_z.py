# -*- coding: utf-8 -*-
"""hook-06 争用失败测（并发投递锁服务）。

事故命题：超时重试并发双请求缺同锁 -> 双壳（两条尝试/两张壳）。
要求：打同一把事件锁，牵头落账、跟随同票复用，不得双壳；跨进程再由 SQLite 票/锁兜底。
"""
import os
import tempfile
import threading
import time
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="hook06_z_")
os.environ.setdefault("数据目录", _TMP)
os.environ.setdefault("夹具目录", str(Path(__file__).resolve().parents[2] / "fixtures"))

import pytest  # noqa: E402

from app import db, seed  # noqa: E402
from app.services import 入队, 投递, 签名目录  # noqa: E402


@pytest.fixture(autouse=True)
def 独立库(tmp_path):
    原路径 = db.库路径
    db.库路径 = tmp_path / "hook_z.db"
    seed.初始化库()
    yield
    db.库路径 = 原路径


def _尝试行(eid):
    conn = db.连接()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM 投递尝试 WHERE 事件号=?", (eid,)).fetchall()]
    finally:
        conn.close()


def test_z_并发超时重试只生一壳_e2e():
    """两线程在同一临界区对齐后并发投递同一超时事件：只允许一条正式尝试、同票。

    见红阶段用现有签名 + *.timeout 事件类型（当前闸未接线，会当成功），
    从而确定性地打出“双壳”，而不是停在缺参数的 TypeError。
    """
    ev = 入队.建事件({"事件类型": "race.double.timeout", "端点键": "ep-billing", "载荷摘要": "race-z"})
    eid = ev["事件号"]

    entered = 0
    gate = threading.Event()
    lock = threading.Lock()
    原规 = 签名目录.签名规

    def 慢规():
        nonlocal entered
        with lock:
            entered += 1
        gate.wait(timeout=5)          # 把线程挡在写库前，放大 TOCTOU 争用
        return 原规()

    签名目录.签名规 = 慢规
    results, errors = [], []

    def worker():
        try:
            results.append(投递.投递一次(eid))   # 现有两参签名；*.timeout 见红期被当成功
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=worker) for _ in range(2)]
    for t in ts:
        t.start()
    # 红（无单飞）：两个线程都会进到 签名规；绿（有同锁单飞）：只有牵头进入
    deadline = time.time() + 2.0
    while time.time() < deadline:
        with lock:
            n = entered
        if n >= 2:
            break
        time.sleep(0.02)
    time.sleep(0.25)                  # 容第二个线程到达“跟随”位
    gate.set()
    for t in ts:
        t.join(timeout=10)
    签名目录.签名规 = 原规

    assert not errors, f"并发投递不应抛错: {errors!r}"
    rows = _尝试行(eid)
    assert len(rows) == 1, f"并发双请求产生双壳：正式尝试行数={len(rows)}"
    assert len(results) == 2
    tickets = {(r.get("票号") or r.get("锁令牌") or r.get("尝试号")) for r in results}
    assert len(tickets) == 1, f"两请求须同票，实际票集合={tickets}"
    assert results[0].get("尝试号") == results[1].get("尝试号"), "跟随者应复用牵头结果，不得另生壳"


def test_z_并发投递锁模块_同锁互斥与同票():
    from app.modules import 并发投递锁 as 锁模块

    assert any(hasattr(锁模块, n) for n in ("同锁", "投递锁", "进场", "单飞", "获取锁")), \
        "并发投递锁空桩须接线，暴露同锁/单飞原语"

    # 能拿到一把事件锁/令牌
    拿 = getattr(锁模块, "获取锁", None) or getattr(锁模块, "加锁", None)
    assert callable(拿)
    tok = 拿("EVT-Z-LOCK")
    assert tok

    # 同事件第二把锁在持有时拿不到（非阻塞），不同事件不互斥
    if hasattr(锁模块, "尝试获取锁"):
        assert 锁模块.尝试获取锁("EVT-Z-LOCK") in (False, None)
        assert 锁模块.尝试获取锁("EVT-OTHER")
    if hasattr(锁模块, "释放锁"):
        锁模块.释放锁("EVT-Z-LOCK", tok)


def test_z_同幂等键并发双请求_同票不双写():
    ev = 入队.建事件({"事件类型": "idem.timeout", "端点键": "ep-billing", "载荷摘要": "idem-z"})
    eid = ev["事件号"]
    key = "HOOK06-TICKET-0001"

    outs = []
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait(timeout=5)
        outs.append(投递.投递一次(eid, 注入超时=True, 幂等键=key))

    ts = [threading.Thread(target=worker) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=10)

    assert len(outs) == 2
    assert len(_尝试行(eid)) == 1, "同一幂等票并发必须只写一条"
    assert outs[0].get("尝试号") == outs[1].get("尝试号")
    assert outs[0].get("票号") == key or outs[0].get("同票") is True


def test_z_事务内同票复查_跨进程漏过入口也不双写():
    """模拟多 worker：第二进程在入口复查时未见票，但 BEGIN IMMEDIATE 后须在事务内
    复查到先到者已提交的同票结果并同票复用，绝不新增第二条尝试。"""
    import json as _json

    ev = 入队.建事件({"事件类型": "cross.proc.timeout", "端点键": "ep-billing",
                     "载荷摘要": "cross-proc"})
    eid = ev["事件号"]
    key = "HOOK06-CROSS-PROC-0001"
    stored = {"尝试号": "TRY-PREEXIST", "状态": "失败", "事件状态": "投递中",
              "票号": key, "同票": True}
    t = seed.此刻()
    conn = db.连接()
    conn.execute(
        "INSERT INTO 幂等票(票键,事件号,状态,响应JSON,时刻,更新时) "
        "VALUES (?,?, '已完成', ?, ?, ?)",
        (key, eid, _json.dumps(stored, ensure_ascii=False), t, t))
    conn.commit()
    conn.close()

    before = len(_尝试行(eid))
    # 直接进核心，绕过 投递一次 入口的 _重读幂等票，专门压事务内复查分支
    rep = 投递._投递核心(eid, False, True, key)
    after = len(_尝试行(eid))

    assert rep.get("尝试号") == "TRY-PREEXIST", rep
    assert rep.get("复用票") is True and rep.get("同票") is True
    assert after == before == 0, f"事务内复查命中后不得新增尝试行：before={before} after={after}"
