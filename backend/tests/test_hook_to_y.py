# -*- coding: utf-8 -*-
"""hook-06 失败测乙（互证服务）。

事故命题：投递互证对账把空成功（事件已投递、尝试表无行）当成“已投对齐”。
要求：互证全扫须认出空成功/无成功尝试/影子与正式票不符，并判撕裂；流水与互证同票。
"""
import os
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="hook06_y_")
os.environ.setdefault("数据目录", _TMP)
os.environ.setdefault("夹具目录", str(Path(__file__).resolve().parents[2] / "fixtures"))

import pytest  # noqa: E402

from app import db, seed  # noqa: E402
from app.services import 入队, 投递, 互证, 签名目录  # noqa: E402


@pytest.fixture(autouse=True)
def 独立库(tmp_path):
    原路径 = db.库路径
    db.库路径 = tmp_path / "hook_y.db"
    seed.初始化库()
    yield
    db.库路径 = 原路径


def _种空成功壳(eid="EVT-Y-EMPTY"):
    t = seed.此刻()
    conn = db.连接()
    conn.execute(
        "INSERT INTO 事件(事件号,事件类型,端点键,载荷摘要,优先级,状态,入队时,更新时) "
        "VALUES (?,?,?,?,?, '已投递', ?, ?)",
        (eid, "x.timeout", "ep-billing", "empty", "普通", t, t))
    conn.commit()
    conn.close()
    return eid


def test_乙_互证不得把空成功当成已投对齐():
    eid = _种空成功壳()
    rep = 互证.跑一次()

    assert rep.get("空成功数", 0) >= 1, "互证须扫出无尝试行的已投递空成功"
    assert rep["是否对齐"] is False or rep["是否全部对齐"] is False, \
        "空成功抬水位，互证必须判撕裂，不得对齐"
    # 联查/明细里要能点到那张空壳
    blob = repr(rep.get("空成功明细") or rep.get("联查") or rep.get("明细") or "")
    assert eid in blob
    assert "空成功" in (rep.get("说明") or "") or "尝试" in (rep.get("说明") or "")


def test_乙_健康种子与正常成功投递仍对齐():
    # 种子自带一张有成功尝试的已投递单 EVT-demo-04，应判对齐
    rep0 = 互证.跑一次()
    assert rep0["是否对齐"] is True, f"健康种子应对齐：{rep0}"

    ev = 入队.建事件({"事件类型": "invoice.paid", "端点键": "ep-partner", "载荷摘要": "ok-y"})
    投递.投递一次(ev["事件号"])
    rep = 互证.跑一次()
    assert rep["是否对齐"] is True
    assert rep.get("空成功数", 0) == 0


def test_乙_超时双写后影子与正式对账同票():
    """超时走双写两轨：正式失败尝试 + 只读归档壳/旁路凭证，票号一致，互证同票。"""
    ev = 入队.建事件({"事件类型": "dual.timeout", "端点键": "ep-billing", "载荷摘要": "dual"})
    eid = ev["事件号"]
    rep = 投递.投递一次(eid, 注入超时=True)
    票 = rep.get("票号") or rep.get("锁令牌") or rep.get("尝试号")
    assert 票

    对账 = 互证.跑一次()
    # 超时是合法的“记失败”，不应被当成空成功；影子票与正式票须对得上
    assert 对账.get("空成功数", 0) == 0
    assert "同票" in 对账, "互证结果须给出 同票 字段"
    assert 对账["同票"] is True, f"影子与正式须同票：{对账}"
    assert 对账["是否对齐"] is True

    # 若强种一张没有正式票的影子归档壳，互证必须判不同票/不对齐
    conn = db.连接()
    t = seed.此刻()
    try:
        conn.execute(
            "INSERT INTO 归档壳(壳号,事件号,票号,锁令牌,壳JSON,拒因,轨,入档时) "
            "VALUES ('SHELL-ORPHAN','EVT-ORPHAN-SHADOW','TICKET-GHOST','','{}','人为影子孤壳','超时只读归档',?)",
            (t,))
        conn.commit()
    except Exception:
        pytest.skip("归档壳表尚未建立（见红阶段预期）")
    finally:
        conn.close()
    对账2 = 互证.跑一次()
    assert 对账2["同票"] is False or 对账2["是否对齐"] is False
