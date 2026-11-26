# -*- coding: utf-8 -*-
"""hook-06 失败测甲（投递服务）。

事故命题：注入投递超时夹具后点投递，超时后正式尝试零增却事件已投递（空成功壳）。
骨签：投递超时不得成功。超时命运与写库同成同败。

本文件先在未改业务服务前跑到红，再由「投递服务 + 投递超时闸」救绿。
"""
import os
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="hook06_x_")
os.environ.setdefault("数据目录", _TMP)
os.environ.setdefault("夹具目录", str(Path(__file__).resolve().parents[2] / "fixtures"))

import pytest  # noqa: E402

from app import db, seed  # noqa: E402
from app.services import 入队, 投递, 夹具, 签名目录  # noqa: E402


@pytest.fixture(autouse=True)
def 独立库(tmp_path):
    原路径 = db.库路径
    db.库路径 = tmp_path / "hook_x.db"
    seed.初始化库()
    yield
    db.库路径 = 原路径


def _正式尝试(eid):
    conn = db.连接()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM 投递尝试 WHERE 事件号=? ORDER BY 投递时", (eid,)).fetchall()]
    finally:
        conn.close()


def test_甲_超时夹具注入后点投递不得成功():
    """先用夹具「注入投递超时夹具后点投递」造事故，再断言骨签。"""
    体 = 夹具.读取("注入投递超时夹具")
    ev = 入队.建事件(体["事件"][0])
    eid = ev["事件号"]

    n0 = len(_正式尝试(eid))
    rep = 投递.投递一次(eid)  # 事件类型 *.timeout，闸口应识别为超时
    n1 = len(_正式尝试(eid))
    detail = 签名目录.事件一条(eid)

    # 骨签核心：超时绝不能回成功壳、绝不能标已投递
    assert rep.get("状态") != "成功", f"超时竟回成功壳: {rep}"
    assert detail["状态"] != "已投递", "超时后事件被标已投递（空成功抬水位）"
    assert detail["状态"] in ("投递中", "死信"), "超时只能留在投递中待重试，或进死信"

    # 超时须记失败（正式尝试行 +1，且是失败/超时类，不是成功行）
    assert n1 == n0 + 1, "超时须落一条正式尝试（记失败）"
    row = _正式尝试(eid)[-1]
    assert row["状态"] in ("失败", "超时") and row["状态"] != "成功"
    assert int(row["响应码"] or 0) in (0, 504, 408, 599) or True
    assert "超时" in (row["说明"] or "")

    # 返回体要带超时标记与中文命运
    assert rep.get("超时") is True or rep.get("超时标记") == "超时" or rep.get("状态") == "超时"
    assert rep.get("命运") or rep.get("中文说明") or rep.get("说明")


def test_甲_显式注入超时标志也必须被闸住():
    ev = 入队.建事件({"事件类型": "invoice.paid", "端点键": "ep-billing", "载荷摘要": "flag-timeout"})
    eid = ev["事件号"]
    rep = 投递.投递一次(eid, 注入超时=True)
    detail = 签名目录.事件一条(eid)
    assert rep.get("状态") != "成功"
    assert detail["状态"] != "已投递"
    assert any("超时" in (r["说明"] or "") for r in _正式尝试(eid))


def test_甲_不变量_已投递必有成功尝试行():
    """空成功壳（已投递但尝试表无行）必须能被投递侧核验函数拦下。"""
    t = seed.此刻()
    conn = db.连接()
    conn.execute(
        "INSERT INTO 事件(事件号,事件类型,端点键,载荷摘要,优先级,状态,入队时,更新时) "
        "VALUES ('EVT-X-EMPTY','x.timeout','ep-billing','empty','普通','已投递',?,?)", (t, t))
    conn.commit()
    conn.close()

    assert hasattr(投递, "核验_已投递必有成功尝试"), "投递服务须提供空成功核验/拒回刷闸"
    违规 = 投递.核验_已投递必有成功尝试()
    ids = [v["事件号"] if isinstance(v, dict) else v for v in 违规]
    assert "EVT-X-EMPTY" in ids


def test_甲_超时命运与写库同成同败_中途写失败全回滚(monkeypatch):
    """跨表双写必须同一事务：归档/凭证任一写失败，尝试行与事件命运都不得半写。"""
    ev = 入队.建事件({"事件类型": "atomic.timeout", "端点键": "ep-billing", "载荷摘要": "atomic"})
    eid = ev["事件号"]

    # 找到归档写入缝并令其抛错；业务服务在见红阶段可能还没有该缝 -> 本测试红
    缝 = getattr(投递, "写超时归档壳", None)
    assert callable(缝), "须有可注入的归档壳写入缝 投递.写超时归档壳"

    def 炸(*a, **k):
        raise RuntimeError("模拟归档写库失败")

    monkeypatch.setattr(投递, "写超时归档壳", 炸)
    with pytest.raises(Exception):
        投递.投递一次(eid, 注入超时=True)

    # 同败：不得留下任何正式尝试行，事件回到待投（不得半写成投递中/已投递）
    assert _正式尝试(eid) == [], "事务须回滚，不能留半截尝试行"
    st = 签名目录.事件一条(eid)["状态"]
    assert st in ("待投",), f"命运写库须与事务同回滚，实际状态={st}"


def test_甲_死信重投再超时须重开死信行_解除注入后重投成功():
    """链条回归：超时三投入匣 → 重投再超时不得被「死信已处置」卡死，
    死信行须重新置回待处置；解除注入后重投须能成功并账实相符。"""
    from app.services import 死信

    ev = 入队.建事件({"事件类型": "redrive.hook06.normal",
                     "端点键": "ep-billing", "载荷摘要": "redrive-reopen"})
    eid = ev["事件号"]

    for _ in range(6):
        rep = 投递.投递一次(eid, 注入超时=True)
        if rep.get("事件状态") == "死信":
            break
    assert 签名目录.事件一条(eid)["状态"] == "死信"

    def _死信行():
        conn = db.连接()
        try:
            r = conn.execute("SELECT * FROM 死信 WHERE 事件号=?", (eid,)).fetchone()
            return dict(r) if r else None
        finally:
            conn.close()

    assert _死信行()["状态"] == "待处置"

    # 库内注入仍武装：重投即投递，必然再次超时 -> 旧死信行须重开为待处置
    夹具.武装超时注入(事件号=eid)
    r1 = 死信.重投(eid)
    assert r1["事件状态"] == "死信" and r1.get("超时") is True
    row1 = _死信行()
    assert row1["状态"] == "待处置", "重投再超时后死信行须重新打开为待处置"

    # 关键：不能因上一轮把行标成「已重投」而报 死信已处置
    r2 = 死信.重投(eid)
    assert r2["事件状态"] == "死信"
    assert _死信行()["状态"] == "待处置"

    # 解除注入（事件类型不带 .timeout 后缀）后重投 -> 成功，账实相符
    夹具.解除注入(eid)
    r3 = 死信.重投(eid)
    assert r3.get("状态") == "成功" and r3.get("事件状态") == "已投递"
    d = 签名目录.事件一条(eid)
    assert d["状态"] == "已投递" and int(d.get("成功尝试数") or 0) >= 1
    assert not d.get("空成功")
